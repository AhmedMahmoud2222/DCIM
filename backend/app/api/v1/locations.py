"""Location hierarchy CRUD (Organization -> Country -> City -> Site -> Building -> Floor
-> Room). Create/get/list for every level validates "location hierarchy" per the Phase 1
scope (§25 of the Phase 1 prompt); Room additionally demonstrates the optimistic-
concurrency foundation (§17) since it's a real Phase 1 entity, not a placeholder."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.access_control import (
    city_visible_clause,
    country_visible_clause,
    ensure_room_access,
    ensure_site_access,
    floor_visible_clause,
    organization_visible_clause,
    room_visible_clause,
    site_visible_clause,
)
from app.application.audit_service import write_audit_log
from app.application.concurrency import lock_versioned_row, require_if_match
from app.application.rbac import require_permission
from app.core.errors import ConflictError, NotFoundError
from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site

router = APIRouter(tags=["locations"])

_DEPENDENCY_LABELS = {
    "rack_placement": "rack placements", "equipment_placement": "equipment placements",
    "user_group_site_access": "site access grants", "floor_plan": "floor plans",
}
_LOCATION_AUDIT_FIELDS = {
    "organization": ("id", "name"),
    "country": ("id", "name", "organization_id", "iso_code"),
    "city": ("id", "name", "country_id"),
    "site": ("id", "code", "name", "city_id"),
    "building": ("id", "code", "name", "site_id"),
    "floor": ("id", "name", "building_id", "level_number"),
    "room": ("id", "code", "name", "floor_id", "version"),
}


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


def _delete_conflict_message(
    entity_name: str, dependencies: list[tuple[str, int | None]], *, include_counts: bool,
) -> str:
    if not include_counts:
        return f"Cannot delete this {entity_name}: dependent records still reference it. " \
            "Contact an administrator to remove the blockers."
    detail = ", ".join(
        f"{_DEPENDENCY_LABELS.get(table, table.replace('_', ' '))} ({count})" for table, count in dependencies
    )
    dependency_detail = f": {detail}" if detail else ""
    return f"Cannot delete this {entity_name}: dependent records still reference it{dependency_detail}. " \
        "Remove or move those records first."


async def _audit_delete_conflict(
    db: AsyncSession, *, actor_id: uuid.UUID, record_id: uuid.UUID, entity_name: str,
    request_id: str | None, correlation_id: str | None, message: str,
) -> None:
    await write_audit_log(
        db, actor_user_id=actor_id, action=f"location.{entity_name}.delete", entity_type=entity_name,
        entity_id=record_id, request_id=request_id, correlation_id=correlation_id,
        result="failure", reason=message,
    )
    await db.commit()


async def _foreign_key_dependents(
    db: AsyncSession, table_name: str, record_id: uuid.UUID, *, include_counts: bool,
) -> list[tuple[str, int | None]]:
    """Find actual database FK references so safe deletes account for every domain table.

    The catalog lookup avoids relying on the ORM import order and includes inventory,
    placements, plans, and operational records added by later modules.
    """
    refs = await db.execute(text("""
        SELECT child_ns.nspname AS schema_name, child.relname AS table_name, child_att.attname AS column_name
        FROM pg_constraint c
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN unnest(c.conkey) WITH ORDINALITY child_key(attnum, ord) ON TRUE
        JOIN unnest(c.confkey) WITH ORDINALITY parent_key(attnum, ord) USING (ord)
        JOIN pg_attribute parent_att ON parent_att.attrelid = parent.oid AND parent_att.attnum = parent_key.attnum
        JOIN pg_attribute child_att ON child_att.attrelid = child.oid AND child_att.attnum = child_key.attnum
        -- Location tables are owned by the application schema and all target the id primary key.
        WHERE c.contype = 'f' AND parent_ns.nspname = current_schema()
          AND parent.relname = :table_name AND parent_att.attname = 'id' AND child.oid <> parent.oid
        ORDER BY child_ns.nspname, child.relname, child_att.attname
    """), {"table_name": table_name})
    dependencies: list[tuple[str, int | None]] = []
    for schema_name, child_table, child_column in refs:
        def quote(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        relation = f"{quote(schema_name)}.{quote(child_table)}"
        condition = f"{quote(child_column)} = :record_id"
        if include_counts:
            count = await db.scalar(text(f"SELECT count(*) FROM {relation} WHERE {condition}"), {"record_id": record_id})
            if count:
                dependencies.append((child_table, int(count)))
        elif await db.scalar(text(f"SELECT 1 FROM {relation} WHERE {condition} LIMIT 1"), {"record_id": record_id}):
            dependencies.append((child_table, None))
    return dependencies


async def _delete_location(
    db: AsyncSession, request: Request, ctx, model, record_id: uuid.UUID, entity_name: str, visible_clause,
) -> Response:
    actor_id = ctx.user.id
    result = await db.execute(
        select(model).where(model.id == record_id, visible_clause).with_for_update()
    )
    entity = result.scalar_one_or_none()
    if entity is None:
        raise NotFoundError(f"{entity_name.title()} {record_id} not found.")

    request_id, correlation_id = _request_ids(request)
    include_counts = ctx.scope.unrestricted
    dependencies = await _foreign_key_dependents(
        db, model.__tablename__, record_id, include_counts=include_counts,
    )
    if dependencies:
        # Restricted users may learn that the in-scope parent has dependents, but
        # never receive hidden table names or counts.
        message = _delete_conflict_message(entity_name, dependencies, include_counts=include_counts)
        await _audit_delete_conflict(
            db, actor_id=actor_id, record_id=record_id, entity_name=entity_name,
            request_id=request_id, correlation_id=correlation_id, message=message,
        )
        raise ConflictError(message)

    before = {}
    for key in _LOCATION_AUDIT_FIELDS[entity_name]:
        value = getattr(entity, key)
        before[key] = str(value) if isinstance(value, uuid.UUID) else value
    try:
        await db.delete(entity)
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        message = _delete_conflict_message(entity_name, [], include_counts=include_counts)
        await _audit_delete_conflict(
            db, actor_id=actor_id, record_id=record_id, entity_name=entity_name,
            request_id=request_id, correlation_id=correlation_id, message=message,
        )
        raise ConflictError(message) from error
    await write_audit_log(
        db, actor_user_id=actor_id, action=f"location.{entity_name}.delete", entity_type=entity_name,
        entity_id=record_id, request_id=request_id, correlation_id=correlation_id, before=before,
    )
    await db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- Organization
class OrganizationIn(BaseModel):
    name: str


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/organizations", response_model=OrganizationOut, status_code=201)
async def create_organization(
    body: OrganizationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Organization:
    org = Organization(name=body.name)
    db.add(org)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="organization.create",
        entity_type="organization",
        entity_id=org.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"name": org.name},
    )
    await db.commit()
    await db.refresh(org)
    return org


@router.get("/organizations", response_model=Page[OrganizationOut])
async def list_organizations(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("organization:read")),
) -> Page:
    visible = organization_visible_clause(ctx.scope, Organization.id)
    total = (await db.execute(select(func.count()).select_from(Organization).where(visible))).scalar_one()
    rows = (
        await db.execute(
            select(Organization).where(visible).order_by(Organization.name).offset(pagination.offset).limit(pagination.limit)
        )
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/organizations/{organization_id}", response_model=OrganizationOut)
async def get_organization(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("organization:read")),
) -> Organization:
    org = await db.get(Organization, organization_id)
    if org is None or not (
        await db.execute(select(func.count()).select_from(Organization).where(
            Organization.id == organization_id, organization_visible_clause(ctx.scope, Organization.id)))
    ).scalar_one():
        raise NotFoundError(f"Organization {organization_id} not found.")
    return org


# ---------------------------------------------------------------- Country
class CountryIn(BaseModel):
    organization_id: uuid.UUID
    name: str
    iso_code: str | None = None


class CountryOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    iso_code: str | None

    model_config = {"from_attributes": True}


@router.post("/countries", response_model=CountryOut, status_code=201)
async def create_country(
    body: CountryIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:manage"))
) -> Country:
    if await db.get(Organization, body.organization_id) is None:
        raise NotFoundError(f"Organization {body.organization_id} not found.")
    country = Country(**body.model_dump())
    db.add(country)
    await db.commit()
    await db.refresh(country)
    return country


@router.get("/countries", response_model=Page[CountryOut])
async def list_countries(
    organization_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = country_visible_clause(ctx.scope, Country.id)
    stmt = select(Country).where(visible)
    count_stmt = select(func.count()).select_from(Country).where(visible)
    if organization_id is not None:
        stmt = stmt.where(Country.organization_id == organization_id)
        count_stmt = count_stmt.where(Country.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(Country.name).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


# ---------------------------------------------------------------- City
class CityIn(BaseModel):
    country_id: uuid.UUID
    name: str


class CityOut(BaseModel):
    id: uuid.UUID
    country_id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


@router.post("/cities", response_model=CityOut, status_code=201)
async def create_city(
    body: CityIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:manage"))
) -> City:
    if await db.get(Country, body.country_id) is None:
        raise NotFoundError(f"Country {body.country_id} not found.")
    city = City(**body.model_dump())
    db.add(city)
    await db.commit()
    await db.refresh(city)
    return city


@router.get("/cities", response_model=Page[CityOut])
async def list_cities(
    country_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = city_visible_clause(ctx.scope, City.id)
    stmt = select(City).where(visible)
    count_stmt = select(func.count()).select_from(City).where(visible)
    if country_id is not None:
        stmt = stmt.where(City.country_id == country_id)
        count_stmt = count_stmt.where(City.country_id == country_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(City.name).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


# ---------------------------------------------------------------- Site
class SiteIn(BaseModel):
    city_id: uuid.UUID
    code: str
    name: str
    address: str | None = None
    timezone: str = "UTC"


class SiteOut(BaseModel):
    id: uuid.UUID
    city_id: uuid.UUID
    code: str
    name: str
    timezone: str

    model_config = {"from_attributes": True}


@router.post("/sites", response_model=SiteOut, status_code=201)
async def create_site(
    body: SiteIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Site:
    if await db.get(City, body.city_id) is None:
        raise NotFoundError(f"City {body.city_id} not found.")
    site = Site(**body.model_dump())
    db.add(site)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="site.create",
        entity_type="site",
        entity_id=site.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"code": site.code, "name": site.name},
    )
    await db.commit()
    await db.refresh(site)
    return site


@router.get("/sites", response_model=Page[SiteOut])
async def list_sites(
    city_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = site_visible_clause(ctx.scope, Site.id)
    stmt = select(Site).where(visible)
    count_stmt = select(func.count()).select_from(Site).where(visible)
    if city_id is not None:
        stmt = stmt.where(Site.city_id == city_id)
        count_stmt = count_stmt.where(Site.city_id == city_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(Site.code).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/sites/{site_id}", response_model=SiteOut)
async def get_site(
    site_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:read"))
) -> Site:
    await ensure_site_access(ctx.scope, site_id)
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError(f"Site {site_id} not found.")
    return site


# ---------------------------------------------------------------- Building
class BuildingIn(BaseModel):
    site_id: uuid.UUID
    code: str
    name: str


class BuildingOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    code: str
    name: str

    model_config = {"from_attributes": True}


@router.post("/buildings", response_model=BuildingOut, status_code=201)
async def create_building(
    body: BuildingIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:manage"))
) -> Building:
    if await db.get(Site, body.site_id) is None:
        raise NotFoundError(f"Site {body.site_id} not found.")
    building = Building(**body.model_dump())
    db.add(building)
    await db.commit()
    await db.refresh(building)
    return building


@router.get("/buildings", response_model=Page[BuildingOut])
async def list_buildings(
    site_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = site_visible_clause(ctx.scope, Building.site_id)
    stmt = select(Building).where(visible)
    count_stmt = select(func.count()).select_from(Building).where(visible)
    if site_id is not None:
        stmt = stmt.where(Building.site_id == site_id)
        count_stmt = count_stmt.where(Building.site_id == site_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(Building.code).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


# ---------------------------------------------------------------- Floor
class FloorIn(BaseModel):
    building_id: uuid.UUID
    name: str
    level_number: int


class FloorOut(BaseModel):
    id: uuid.UUID
    building_id: uuid.UUID
    name: str
    level_number: int

    model_config = {"from_attributes": True}


@router.post("/floors", response_model=FloorOut, status_code=201)
async def create_floor(
    body: FloorIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:manage"))
) -> Floor:
    if await db.get(Building, body.building_id) is None:
        raise NotFoundError(f"Building {body.building_id} not found.")
    floor = Floor(**body.model_dump())
    db.add(floor)
    await db.commit()
    await db.refresh(floor)
    return floor


@router.get("/floors", response_model=Page[FloorOut])
async def list_floors(
    building_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = floor_visible_clause(ctx.scope, Floor.id)
    stmt = select(Floor).where(visible)
    count_stmt = select(func.count()).select_from(Floor).where(visible)
    if building_id is not None:
        stmt = stmt.where(Floor.building_id == building_id)
        count_stmt = count_stmt.where(Floor.building_id == building_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Floor.level_number).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


# ---------------------------------------------------------------- Room
class RoomIn(BaseModel):
    floor_id: uuid.UUID
    code: str
    name: str
    room_type: str = "data_hall"


class RoomOut(BaseModel):
    id: uuid.UUID
    floor_id: uuid.UUID
    code: str
    name: str
    room_type: str
    version: int

    model_config = {"from_attributes": True}


class RoomUpdate(BaseModel):
    name: str | None = None
    room_type: str | None = None


@router.post("/rooms", response_model=RoomOut, status_code=201)
async def create_room(
    body: RoomIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Room:
    if await db.get(Floor, body.floor_id) is None:
        raise NotFoundError(f"Floor {body.floor_id} not found.")
    room = Room(**body.model_dump())
    db.add(room)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="room.create",
        entity_type="room",
        entity_id=room.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"code": room.code, "name": room.name},
    )
    await db.commit()
    await db.refresh(room)
    return room


@router.get("/rooms", response_model=Page[RoomOut])
async def list_rooms(
    floor_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("location:read")),
) -> Page:
    visible = room_visible_clause(ctx.scope, Room.id)
    stmt = select(Room).where(visible)
    count_stmt = select(func.count()).select_from(Room).where(visible)
    if floor_id is not None:
        stmt = stmt.where(Room.floor_id == floor_id)
        count_stmt = count_stmt.where(Room.floor_id == floor_id)
    if site_id is not None:
        site_rooms = (
            Floor.id == Room.floor_id,
            Building.id == Floor.building_id,
            Building.site_id == site_id,
        )
        stmt = stmt.join(Floor, site_rooms[0]).join(Building, site_rooms[1]).where(site_rooms[2])
        count_stmt = count_stmt.join(Floor, site_rooms[0]).join(Building, site_rooms[1]).where(site_rooms[2])
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(Room.code).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/rooms/{room_id}", response_model=RoomOut)
async def get_room(
    room_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("location:read"))
) -> Room:
    await ensure_room_access(db, ctx.scope, room_id)
    room = await db.get(Room, room_id)
    if room is None:
        raise NotFoundError(f"Room {room_id} not found.")
    return room


@router.patch("/rooms/{room_id}", response_model=RoomOut)
async def update_room(
    room_id: uuid.UUID,
    body: RoomUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("location:update")),
) -> Room:
    """Optimistic-concurrency demonstration entity (§17): a stale If-Match is rejected
    with 409 before any write happens, never a silent overwrite."""
    room = await lock_versioned_row(db, Room, room_id, expected_version=if_match_version)

    before = {"name": room.name, "room_type": room.room_type, "version": room.version}
    if body.name is not None:
        room.name = body.name
    if body.room_type is not None:
        room.room_type = body.room_type
    room.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="room.update",
        entity_type="room",
        entity_id=room.id,
        request_id=request_id,
        correlation_id=correlation_id,
        before=before,
        after={"name": room.name, "room_type": room.room_type, "version": room.version},
    )
    await db.commit()
    await db.refresh(room)
    return room


# --------------------------------------------------------------- safe deletion


@router.delete("/organizations/{organization_id}", status_code=204)
async def delete_organization(
    organization_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Organization, organization_id, "organization",
                                  organization_visible_clause(ctx.scope, Organization.id))


@router.delete("/countries/{country_id}", status_code=204)
async def delete_country(
    country_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Country, country_id, "country",
                                  country_visible_clause(ctx.scope, Country.id))


@router.delete("/cities/{city_id}", status_code=204)
async def delete_city(
    city_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, City, city_id, "city",
                                  city_visible_clause(ctx.scope, City.id))


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(
    site_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Site, site_id, "site", site_visible_clause(ctx.scope, Site.id))


@router.delete("/buildings/{building_id}", status_code=204)
async def delete_building(
    building_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Building, building_id, "building",
                                  site_visible_clause(ctx.scope, Building.site_id))


@router.delete("/floors/{floor_id}", status_code=204)
async def delete_floor(
    floor_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Floor, floor_id, "floor", floor_visible_clause(ctx.scope, Floor.id))


@router.delete("/rooms/{room_id}", status_code=204)
async def delete_room(
    room_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("location:manage")),
) -> Response:
    return await _delete_location(db, request, ctx, Room, room_id, "room", room_visible_clause(ctx.scope, Room.id))
