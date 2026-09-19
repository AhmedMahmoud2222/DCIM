"""Location hierarchy CRUD (Organization -> Country -> City -> Site -> Building -> Floor
-> Room). Create/get/list for every level validates "location hierarchy" per the Phase 1
scope (§25 of the Phase 1 prompt); Room additionally demonstrates the optimistic-
concurrency foundation (§17) since it's a real Phase 1 entity, not a placeholder."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match, require_if_match
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site

router = APIRouter(tags=["locations"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


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
    total = (await db.execute(select(func.count()).select_from(Organization))).scalar_one()
    rows = (
        await db.execute(select(Organization).order_by(Organization.name).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/organizations/{organization_id}", response_model=OrganizationOut)
async def get_organization(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("organization:read")),
) -> Organization:
    org = await db.get(Organization, organization_id)
    if org is None:
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
    stmt = select(Country)
    count_stmt = select(func.count()).select_from(Country)
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
    stmt = select(City)
    count_stmt = select(func.count()).select_from(City)
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
    stmt = select(Site)
    count_stmt = select(func.count()).select_from(Site)
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
    stmt = select(Building)
    count_stmt = select(func.count()).select_from(Building)
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
    stmt = select(Floor)
    count_stmt = select(func.count()).select_from(Floor)
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
    stmt = select(Room)
    count_stmt = select(func.count()).select_from(Room)
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
    room = await db.get(Room, room_id)
    if room is None:
        raise NotFoundError(f"Room {room_id} not found.")

    check_version_match(expected=if_match_version, actual=room.version)

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
