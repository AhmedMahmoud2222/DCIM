# ruff: noqa: E501
"""Cooling assets, sensors, groups, thermal zones and their relationships (Issue #105) -- configuration and
inventory. Read-only derived views (heat maps, airflow, capacity, exceptions) live in `thermal.py`.

Authorization: every route needs a literal `cooling:read` / `cooling:manage` code. Neither is in
SCOPE_AWARE_PERMISSIONS, so a site-restricted user holds no effective cooling permission and is refused outright
(fail closed, same posture as the #104 overlays). Resource lookups additionally check the caller's site scope and
answer 404 for a resource in a site the caller cannot see, so a known id never confirms existence.

Cooling units and sensors are ManagedAsset subtypes (shared primary key). Mutations use If-Match optimistic
concurrency on the subtype `version`; placements reuse the shared close-then-open `equipment_placement` history."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import lock_versioned_row, parse_if_match, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.placement_service import (
    PlacementConflict,
    get_current_equipment_placement,
    move_equipment,
    retire_equipment_placement,
)
from app.application.rbac import AuthContext, require_permission
from app.application.spatial_validation import (
    validate_coordinate,
    validate_rotation_degrees,
    validate_u_range_against_rack_capacity,
)
from app.application.thermal.capacity import room_site_id
from app.application.thermal.zone_geometry import (
    ZoneShape,
    active_floor_plan,
    check_inside_room,
    check_point_inside_room,
    validate_shape,
)
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.catalog.models import RackModelRevision
from app.domain.cooling.models import (
    COOLING_UNIT_KINDS,
    SENSOR_KIND_METRICS,
    ContainmentElement,
    CoolingGroup,
    CoolingUnit,
    CoolingUnitZone,
    EnvironmentalSensor,
    ThermalZone,
)
from app.domain.identity.models import ALLOWED_LIFECYCLE_TRANSITIONS, ManagedAsset
from app.domain.location.models import Site
from app.domain.physical.models import Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.telemetry.models import IntegrationMetricMapping

router = APIRouter(prefix="/cooling", tags=["cooling"])

UnitKind = Literal["crac", "crah", "chiller"]
OperatingStatus = Literal["online", "standby", "offline", "fault", "unknown"]
SensorKind = Literal["temperature", "humidity", "airflow", "differential_pressure", "combined"]
SensorRole = Literal["ambient", "rack_inlet", "rack_exhaust", "supply_air", "return_air", "other"]
ZoneKind = Literal["served_zone", "supply_region", "return_region", "hot_aisle", "cold_aisle"]
PlacementType = Literal["floor_standing", "wall_mounted", "ceiling_mounted", "rack_mounted", "other"]
RelationKind = Literal["serves", "supplies", "returns_from"]
Semantics = Literal["authoritative", "configured", "modelled"]

# which zone kinds each relationship may target
RELATION_TARGETS: dict[str, tuple[str, ...]] = {
    "serves": ("served_zone",),
    "supplies": ("supply_region", "cold_aisle"),
    "returns_from": ("return_region", "hot_aisle"),
}


def _ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


def _site_visible(ctx: AuthContext, site_id: uuid.UUID) -> bool:
    return ctx.scope.allows_site(site_id)


async def _require_site(db: AsyncSession, ctx: AuthContext, site_id: uuid.UUID) -> None:
    if not _site_visible(ctx, site_id) or await db.get(Site, site_id) is None:
        raise ApiError(status_code=422, title="Validation Error", detail=f"site_id {site_id} does not exist.")


async def _room_site(db: AsyncSession, ctx: AuthContext, room_id: uuid.UUID) -> uuid.UUID:
    site_id = await room_site_id(db, room_id)
    if site_id is None or not _site_visible(ctx, site_id):
        raise NotFoundError(f"Room {room_id} not found.")
    return site_id


async def _commit_or_conflict(db: AsyncSession, detail: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(detail=detail) from exc


# ----------------------------------------------------------------------------------------------- shared output


class PlacementOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    placement_type: str
    x_mm: int | None
    y_mm: int | None
    rotation_deg: int | None
    rack_id: uuid.UUID | None
    u_start: int | None = None
    u_end: int | None = None
    side: str | None = None
    position_calibration_id: uuid.UUID | None
    version: int
    effective_from: datetime


def _placement_out(p: EquipmentPlacement | None) -> PlacementOut | None:
    if p is None:
        return None
    return PlacementOut(
        id=p.id, room_id=p.room_id, placement_type=p.placement_type, x_mm=p.x_mm, y_mm=p.y_mm, rotation_deg=p.rotation_deg,
        rack_id=p.rack_id, u_start=p.u_range.lower if p.u_range is not None else None, u_end=p.u_range.upper if p.u_range is not None else None,
        side=p.side, position_calibration_id=p.position_calibration_id, version=p.version, effective_from=p.effective_from,
    )


class PlacementIn(BaseModel):
    room_id: uuid.UUID
    placement_type: PlacementType = "floor_standing"
    x_mm: int | None = None
    y_mm: int | None = None
    rotation_deg: int | None = None
    rack_id: uuid.UUID | None = None
    u_start: int | None = None
    u_end: int | None = None
    side: Literal["front", "rear", "both"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "PlacementIn":
        if (self.x_mm is None) != (self.y_mm is None):
            raise ValueError("x_mm and y_mm must be given together.")
        if self.placement_type == "rack_mounted":
            if self.x_mm is not None:
                raise ValueError("A rack-mounted placement takes its position from the rack; omit x_mm / y_mm.")
            if None in (self.rack_id, self.u_start, self.u_end, self.side):
                raise ValueError("A rack-mounted placement needs rack_id, u_start, u_end and side.")
        elif any(v is not None for v in (self.rack_id, self.u_start, self.u_end, self.side)):
            raise ValueError("rack_id / u_start / u_end / side apply to rack-mounted placements only.")
        return self


async def _place_asset(
    db: AsyncSession, request: Request, ctx: AuthContext, *, asset_id: uuid.UUID, site_id: uuid.UUID, label: str, body: PlacementIn,
    if_match_version: int | None, allow_rack_mounted: bool,
) -> EquipmentPlacement:
    if body.placement_type == "rack_mounted" and not allow_rack_mounted:
        raise ApiError(status_code=422, title="Validation Error", detail="This asset type cannot be rack-mounted.")
    room_site = await _room_site(db, ctx, body.room_id)
    if room_site != site_id:
        raise ApiError(status_code=422, title="Cross-Site Placement", detail="The room belongs to a different site than this asset.")
    validate_coordinate("x_mm", body.x_mm)
    validate_coordinate("y_mm", body.y_mm)
    validate_rotation_degrees(body.rotation_deg)
    calibration_id: uuid.UUID | None = None
    if body.x_mm is not None and body.y_mm is not None:
        plan = await active_floor_plan(db, body.room_id)
        if plan is None or plan.current_calibration_id is None:
            raise ApiError(
                status_code=422, title="Calibrated Floor Plan Required",
                detail="A position can only be recorded on a room with an active, calibrated floor plan.",
            )
        await check_point_inside_room(db, body.room_id, (float(body.x_mm), float(body.y_mm)), label)
        calibration_id = plan.current_calibration_id
    if body.placement_type == "rack_mounted":
        assert body.rack_id is not None and body.u_start is not None and body.u_end is not None
        rack_placement = (
            await db.execute(select(RackPlacement).where(RackPlacement.rack_id == body.rack_id, RackPlacement.effective_to.is_(None)))
        ).scalar_one_or_none()
        if rack_placement is None or rack_placement.room_id != body.room_id:
            raise ApiError(status_code=422, title="Validation Error", detail="The rack is not currently placed in this room.")
        height_u = (
            await db.execute(
                select(RackModelRevision.height_u).join(Rack, Rack.model_revision_id == RackModelRevision.id).where(Rack.id == body.rack_id)
            )
        ).scalar_one()
        validate_u_range_against_rack_capacity(u_start=body.u_start, u_end=body.u_end, rack_height_u=height_u)
    try:
        placement = await move_equipment(
            db, equipment_id=asset_id, placement_type=body.placement_type, room_id=body.room_id, rack_id=body.rack_id,
            u_start=body.u_start, u_end=body.u_end, side=body.side, rotation_deg=body.rotation_deg, x_mm=body.x_mm, y_mm=body.y_mm,
            position_calibration_id=calibration_id, if_match_version=if_match_version,
        )
    except PlacementConflict as exc:
        await db.rollback()
        raise ConflictError(detail="The placement changed while you were editing; reload and try again.") from exc
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(detail="That placement conflicts with existing placement constraints (for example overlapping rack units).") from exc
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="cooling.placement.set", entity_type="managed_asset", entity_id=asset_id,
        request_id=request_id, correlation_id=correlation_id,
        after={"room_id": str(body.room_id), "placement_type": body.placement_type, "x_mm": body.x_mm, "y_mm": body.y_mm,
               "rack_id": str(body.rack_id) if body.rack_id else None, "calibration_id": str(calibration_id) if calibration_id else None},
    )
    await write_outbox_event(
        db, event_type="CoolingAssetPlaced", aggregate_type="managed_asset", aggregate_id=asset_id,
        payload={"room_id": str(body.room_id), "placement_type": body.placement_type}, correlation_id=correlation_id,
    )
    return placement


# ----------------------------------------------------------------------------------------------- cooling groups


class CoolingGroupIn(BaseModel):
    site_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class CoolingGroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class CoolingGroupOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    notes: str | None
    retired: bool
    version: int
    member_ids: list[uuid.UUID]


async def _group_out(db: AsyncSession, group: CoolingGroup) -> CoolingGroupOut:
    members = list((await db.execute(select(CoolingUnit.id).where(CoolingUnit.cooling_group_id == group.id).order_by(CoolingUnit.id))).scalars())
    return CoolingGroupOut(id=group.id, site_id=group.site_id, name=group.name, notes=group.notes, retired=group.retired, version=group.version, member_ids=members)


async def _visible_group(db: AsyncSession, ctx: AuthContext, group_id: uuid.UUID) -> CoolingGroup:
    group = await db.get(CoolingGroup, group_id)
    if group is None or not _site_visible(ctx, group.site_id):
        raise NotFoundError(f"Cooling group {group_id} not found.")
    return group


@router.post("/groups", response_model=CoolingGroupOut, status_code=201)
async def create_group(
    body: CoolingGroupIn, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> CoolingGroupOut:
    await _require_site(db, ctx, body.site_id)
    group = CoolingGroup(site_id=body.site_id, name=body.name.strip(), notes=body.notes)
    db.add(group)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(detail="A cooling group with that name already exists on this site.") from exc
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.group.create", entity_type="cooling_group", entity_id=group.id,
                          request_id=request_id, correlation_id=correlation_id, after={"name": group.name, "site_id": str(group.site_id)})
    await write_outbox_event(db, event_type="CoolingGroupCreated", aggregate_type="cooling_group", aggregate_id=group.id,
                             payload={"site_id": str(group.site_id)}, correlation_id=correlation_id)
    await db.commit()
    return await _group_out(db, group)


@router.get("/groups", response_model=Page[CoolingGroupOut])
async def list_groups(
    site_id: uuid.UUID | None = None, include_retired: bool = False, page: Pagination = Depends(pagination_params),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read")),
) -> Page[CoolingGroupOut]:
    stmt = select(CoolingGroup)
    if site_id is not None:
        stmt = stmt.where(CoolingGroup.site_id == site_id)
    if not ctx.scope.unrestricted:
        stmt = stmt.where(CoolingGroup.site_id.in_(ctx.scope.site_ids))
    if not include_retired:
        stmt = stmt.where(CoolingGroup.retired.is_(False))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(CoolingGroup.name, CoolingGroup.id).limit(page.limit).offset(page.offset))).scalars().all()
    return Page(items=[await _group_out(db, g) for g in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/groups/{group_id}", response_model=CoolingGroupOut)
async def get_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read"))) -> CoolingGroupOut:
    return await _group_out(db, await _visible_group(db, ctx, group_id))


@router.patch("/groups/{group_id}", response_model=CoolingGroupOut)
async def update_group(
    group_id: uuid.UUID, body: CoolingGroupPatch, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> CoolingGroupOut:
    await _visible_group(db, ctx, group_id)
    group = await lock_versioned_row(db, CoolingGroup, group_id, expected_version=if_match, label="Cooling group")
    before = {"name": group.name, "notes": group.notes}
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        group.name = changes["name"].strip()
    if "notes" in changes:
        group.notes = changes["notes"]
    group.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.group.update", entity_type="cooling_group", entity_id=group.id,
                          request_id=request_id, correlation_id=correlation_id, before=before, after={"name": group.name, "notes": group.notes})
    await _commit_or_conflict(db, "A cooling group with that name already exists on this site.")
    return await _group_out(db, group)


@router.post("/groups/{group_id}/retire", response_model=CoolingGroupOut)
async def retire_group(
    group_id: uuid.UUID, request: Request, if_match: int = Depends(require_if_match), db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> CoolingGroupOut:
    await _visible_group(db, ctx, group_id)
    group = await lock_versioned_row(db, CoolingGroup, group_id, expected_version=if_match, label="Cooling group")
    members = (await db.execute(select(func.count()).select_from(CoolingUnit).where(CoolingUnit.cooling_group_id == group.id))).scalar_one()
    if members:
        raise ConflictError(detail=f"The group still has {members} member unit(s); remove them from the group first.")
    group.retired = True
    group.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.group.retire", entity_type="cooling_group", entity_id=group.id,
                          request_id=request_id, correlation_id=correlation_id)
    await db.commit()
    return await _group_out(db, group)


# ----------------------------------------------------------------------------------------------- cooling units


class _UnitFields(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    rated_cooling_capacity_kw: float | None = Field(default=None, gt=0, le=100000)
    configured_cooling_capacity_kw: float | None = Field(default=None, gt=0, le=100000)
    airflow_capacity_m3_s: float | None = Field(default=None, gt=0, le=1000)
    supply_air_target_c: float | None = Field(default=None, ge=-50, le=100)
    return_air_design_c: float | None = Field(default=None, ge=-50, le=100)
    humidity_min_percent: float | None = Field(default=None, ge=0, le=100)
    humidity_max_percent: float | None = Field(default=None, ge=0, le=100)
    supply_direction_deg: int | None = Field(default=None, ge=0, le=359)
    operating_status: OperatingStatus = "unknown"
    cooling_group_id: uuid.UUID | None = None


def _check_unit_consistency(rated, configured, hmin, hmax) -> None:
    if rated is not None and configured is not None and configured > rated:
        raise ApiError(status_code=422, title="Validation Error", detail="configured_cooling_capacity_kw cannot exceed rated_cooling_capacity_kw.")
    if hmin is not None and hmax is not None and hmin >= hmax:
        raise ApiError(status_code=422, title="Validation Error", detail="humidity_min_percent must be below humidity_max_percent.")


class CoolingUnitIn(_UnitFields):
    unit_kind: UnitKind
    asset_tag: str = Field(min_length=1, max_length=64)
    serial_number: str | None = Field(default=None, max_length=128)
    site_id: uuid.UUID


class CoolingUnitPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    rated_cooling_capacity_kw: float | None = Field(default=None, gt=0, le=100000)
    configured_cooling_capacity_kw: float | None = Field(default=None, gt=0, le=100000)
    airflow_capacity_m3_s: float | None = Field(default=None, gt=0, le=1000)
    supply_air_target_c: float | None = Field(default=None, ge=-50, le=100)
    return_air_design_c: float | None = Field(default=None, ge=-50, le=100)
    humidity_min_percent: float | None = Field(default=None, ge=0, le=100)
    humidity_max_percent: float | None = Field(default=None, ge=0, le=100)
    supply_direction_deg: int | None = Field(default=None, ge=0, le=359)
    operating_status: OperatingStatus | None = None
    cooling_group_id: uuid.UUID | None = None


class ZoneRelationOut(BaseModel):
    id: uuid.UUID
    cooling_unit_id: uuid.UUID
    thermal_zone_id: uuid.UUID
    relation_kind: str
    semantics: str


class CoolingUnitOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    serial_number: str | None
    unit_kind: str
    site_id: uuid.UUID
    name: str
    notes: str | None
    lifecycle_status: str
    operating_status: str
    rated_cooling_capacity_kw: float | None
    configured_cooling_capacity_kw: float | None
    airflow_capacity_m3_s: float | None
    supply_air_target_c: float | None
    return_air_design_c: float | None
    humidity_min_percent: float | None
    humidity_max_percent: float | None
    supply_direction_deg: int | None
    cooling_group_id: uuid.UUID | None
    version: int
    placement: PlacementOut | None
    relations: list[ZoneRelationOut]


def _f(value) -> float | None:
    return float(value) if value is not None else None


async def _unit_out(db: AsyncSession, unit: CoolingUnit, asset: ManagedAsset) -> CoolingUnitOut:
    relations = (
        await db.execute(select(CoolingUnitZone).where(CoolingUnitZone.cooling_unit_id == unit.id).order_by(CoolingUnitZone.created_at, CoolingUnitZone.id))
    ).scalars().all()
    return CoolingUnitOut(
        id=unit.id, asset_tag=asset.asset_tag, serial_number=asset.serial_number, unit_kind=unit.unit_kind, site_id=unit.site_id, name=unit.name,
        notes=unit.notes, lifecycle_status=asset.lifecycle_status, operating_status=unit.operating_status,
        rated_cooling_capacity_kw=_f(unit.rated_cooling_capacity_kw), configured_cooling_capacity_kw=_f(unit.configured_cooling_capacity_kw),
        airflow_capacity_m3_s=_f(unit.airflow_capacity_m3_s), supply_air_target_c=_f(unit.supply_air_target_c),
        return_air_design_c=_f(unit.return_air_design_c), humidity_min_percent=_f(unit.humidity_min_percent),
        humidity_max_percent=_f(unit.humidity_max_percent), supply_direction_deg=unit.supply_direction_deg,
        cooling_group_id=unit.cooling_group_id, version=unit.version,
        placement=_placement_out(await get_current_equipment_placement(db, unit.id)),
        relations=[ZoneRelationOut(id=r.id, cooling_unit_id=r.cooling_unit_id, thermal_zone_id=r.thermal_zone_id, relation_kind=r.relation_kind, semantics=r.semantics) for r in relations],
    )


async def _visible_unit(db: AsyncSession, ctx: AuthContext, unit_id: uuid.UUID) -> tuple[CoolingUnit, ManagedAsset]:
    row = (await db.execute(select(CoolingUnit, ManagedAsset).join(ManagedAsset, ManagedAsset.id == CoolingUnit.id).where(CoolingUnit.id == unit_id))).first()
    if row is None or not _site_visible(ctx, row[0].site_id):
        raise NotFoundError(f"Cooling unit {unit_id} not found.")
    return row[0], row[1]


async def _check_group(db: AsyncSession, group_id: uuid.UUID | None, site_id: uuid.UUID, ctx: AuthContext) -> None:
    if group_id is None:
        return
    group = await db.get(CoolingGroup, group_id)
    if group is None or not _site_visible(ctx, group.site_id):
        raise ApiError(status_code=422, title="Validation Error", detail=f"cooling_group_id {group_id} does not exist.")
    if group.site_id != site_id:
        raise ApiError(status_code=422, title="Cross-Site Group", detail="The cooling group belongs to a different site than this unit.")
    if group.retired:
        raise ApiError(status_code=422, title="Validation Error", detail="The cooling group is retired.")


@router.post("/units", response_model=CoolingUnitOut, status_code=201)
async def create_unit(
    body: CoolingUnitIn, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> CoolingUnitOut:
    _check_unit_consistency(body.rated_cooling_capacity_kw, body.configured_cooling_capacity_kw, body.humidity_min_percent, body.humidity_max_percent)
    await _require_site(db, ctx, body.site_id)
    await _check_group(db, body.cooling_group_id, body.site_id, ctx)
    asset = ManagedAsset(asset_type=body.unit_kind, asset_tag=body.asset_tag.strip(), serial_number=body.serial_number, lifecycle_status="planned")
    db.add(asset)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(detail="That asset_tag is already in use.") from exc
    fields = body.model_dump(exclude={"asset_tag", "serial_number", "unit_kind"})
    unit = CoolingUnit(id=asset.id, unit_kind=body.unit_kind, **fields)
    db.add(unit)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.unit.create", entity_type=body.unit_kind, entity_id=asset.id,
                          request_id=request_id, correlation_id=correlation_id, after={"asset_tag": asset.asset_tag, "name": unit.name, "site_id": str(unit.site_id)})
    await write_outbox_event(db, event_type="CoolingUnitCreated", aggregate_type="cooling_unit", aggregate_id=asset.id,
                             payload={"unit_kind": body.unit_kind, "site_id": str(unit.site_id)}, correlation_id=correlation_id)
    await db.commit()
    return await _unit_out(db, unit, asset)


@router.get("/units", response_model=Page[CoolingUnitOut])
async def list_units(
    site_id: uuid.UUID | None = None, unit_kind: UnitKind | None = None, room_id: uuid.UUID | None = None, operating_status: OperatingStatus | None = None,
    page: Pagination = Depends(pagination_params), db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read")),
) -> Page[CoolingUnitOut]:
    stmt = select(CoolingUnit, ManagedAsset).join(ManagedAsset, ManagedAsset.id == CoolingUnit.id)
    if site_id is not None:
        stmt = stmt.where(CoolingUnit.site_id == site_id)
    if unit_kind is not None:
        stmt = stmt.where(CoolingUnit.unit_kind == unit_kind)
    if operating_status is not None:
        stmt = stmt.where(CoolingUnit.operating_status == operating_status)
    if room_id is not None:
        stmt = stmt.join(EquipmentPlacement, EquipmentPlacement.equipment_id == CoolingUnit.id).where(
            EquipmentPlacement.room_id == room_id, EquipmentPlacement.effective_to.is_(None)
        )
    if not ctx.scope.unrestricted:
        stmt = stmt.where(CoolingUnit.site_id.in_(ctx.scope.site_ids))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(CoolingUnit.name, CoolingUnit.id).limit(page.limit).offset(page.offset))).all()
    return Page(items=[await _unit_out(db, u, a) for u, a in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/units/{unit_id}", response_model=CoolingUnitOut)
async def get_unit(unit_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read"))) -> CoolingUnitOut:
    unit, asset = await _visible_unit(db, ctx, unit_id)
    return await _unit_out(db, unit, asset)


@router.patch("/units/{unit_id}", response_model=CoolingUnitOut)
async def update_unit(
    unit_id: uuid.UUID, body: CoolingUnitPatch, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> CoolingUnitOut:
    _, asset = await _visible_unit(db, ctx, unit_id)
    if asset.lifecycle_status in ("decommissioned", "removed"):
        raise ConflictError(detail="A retired cooling unit cannot be edited.")
    unit = await lock_versioned_row(db, CoolingUnit, unit_id, expected_version=if_match, label="Cooling unit")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise ApiError(status_code=422, title="Validation Error", detail="name cannot be null.")
    if "operating_status" in changes and changes["operating_status"] is None:
        raise ApiError(status_code=422, title="Validation Error", detail="operating_status cannot be null; use 'unknown'.")
    before = {k: (str(v) if isinstance(v, uuid.UUID) else _f(v) if k.endswith(("_kw", "_c", "_m3_s", "_percent")) else v) for k, v in ((k, getattr(unit, k)) for k in changes)}
    merged = {k: changes.get(k, getattr(unit, k)) for k in ("rated_cooling_capacity_kw", "configured_cooling_capacity_kw", "humidity_min_percent", "humidity_max_percent")}
    _check_unit_consistency(*(_f(merged[k]) for k in ("rated_cooling_capacity_kw", "configured_cooling_capacity_kw", "humidity_min_percent", "humidity_max_percent")))
    if "cooling_group_id" in changes:
        await _check_group(db, changes["cooling_group_id"], unit.site_id, ctx)
    for key, value in changes.items():
        setattr(unit, key, value)
    unit.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.unit.update", entity_type=unit.unit_kind, entity_id=unit.id,
                          request_id=request_id, correlation_id=correlation_id, before=before,
                          after={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in changes.items()})
    await write_outbox_event(db, event_type="CoolingUnitUpdated", aggregate_type="cooling_unit", aggregate_id=unit.id,
                             payload={"fields": sorted(changes)}, correlation_id=correlation_id)
    await _commit_or_conflict(db, "The update conflicts with existing data.")
    return await _unit_out(db, unit, asset)


@router.put("/units/{unit_id}/placement", response_model=PlacementOut)
async def place_unit(
    unit_id: uuid.UUID, body: PlacementIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match: str | None = None, ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> PlacementOut:
    unit, asset = await _visible_unit(db, ctx, unit_id)
    if asset.lifecycle_status in ("decommissioned", "removed"):
        raise ConflictError(detail="A retired cooling unit cannot be placed.")
    placement = await _place_asset(
        db, request, ctx, asset_id=unit.id, site_id=unit.site_id, label=unit.name, body=body,
        if_match_version=parse_if_match(if_match), allow_rack_mounted=False,
    )
    await db.commit()
    out = _placement_out(placement)
    assert out is not None
    return out


@router.delete("/units/{unit_id}/placement", status_code=204)
async def unplace_unit(
    unit_id: uuid.UUID, request: Request, if_match: str | None = None, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> None:
    unit, _ = await _visible_unit(db, ctx, unit_id)
    await _unplace(db, request, ctx, unit.id, parse_if_match(if_match))


async def _unplace(db: AsyncSession, request: Request, ctx: AuthContext, asset_id: uuid.UUID, if_match_version: int | None) -> None:
    try:
        closed = await retire_equipment_placement(db, equipment_id=asset_id, if_match_version=if_match_version)
    except PlacementConflict as exc:
        await db.rollback()
        raise ConflictError(detail="The placement changed while you were editing; reload and try again.") from exc
    if closed is None:
        raise NotFoundError("This asset has no current placement.")
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.placement.clear", entity_type="managed_asset", entity_id=asset_id,
                          request_id=request_id, correlation_id=correlation_id, before={"room_id": str(closed.room_id)})
    await db.commit()


@router.post("/units/{unit_id}/retire", response_model=CoolingUnitOut)
async def retire_unit(
    unit_id: uuid.UUID, request: Request, if_match: int = Depends(require_if_match), db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> CoolingUnitOut:
    _, asset = await _visible_unit(db, ctx, unit_id)
    unit = await lock_versioned_row(db, CoolingUnit, unit_id, expected_version=if_match, label="Cooling unit")
    related = (await db.execute(select(func.count()).select_from(CoolingUnitZone).where(CoolingUnitZone.cooling_unit_id == unit.id))).scalar_one()
    if related:
        raise ConflictError(detail=f"The unit is still related to {related} thermal zone(s); remove the relationships before retiring it.")
    locked_asset = (
        await db.execute(select(ManagedAsset).where(ManagedAsset.id == unit.id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()
    target = "decommissioned" if "decommissioned" in ALLOWED_LIFECYCLE_TRANSITIONS.get(locked_asset.lifecycle_status, set()) else "removed"
    if target not in ALLOWED_LIFECYCLE_TRANSITIONS.get(locked_asset.lifecycle_status, set()):
        raise ConflictError(detail=f"A unit in state {locked_asset.lifecycle_status!r} cannot be retired.")
    previous = locked_asset.lifecycle_status
    locked_asset.lifecycle_status = target
    if target == "decommissioned":
        locked_asset.decommissioned_at = datetime.now(UTC).replace(tzinfo=None)
    unit.operating_status = "offline"
    unit.version += 1
    await retire_equipment_placement(db, equipment_id=unit.id)
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.unit.retire", entity_type=unit.unit_kind, entity_id=unit.id,
                          request_id=request_id, correlation_id=correlation_id, before={"lifecycle_status": previous}, after={"lifecycle_status": target})
    await write_outbox_event(db, event_type="CoolingUnitRetired", aggregate_type="cooling_unit", aggregate_id=unit.id,
                             payload={"from": previous, "to": target}, correlation_id=correlation_id)
    await _commit_or_conflict(db, "The unit could not be retired.")
    await db.refresh(locked_asset)
    return await _unit_out(db, unit, locked_asset)


# ----------------------------------------------------------------------------------------------- sensors


class SensorIn(BaseModel):
    asset_tag: str = Field(min_length=1, max_length=64)
    serial_number: str | None = Field(default=None, max_length=128)
    site_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    sensor_kind: SensorKind
    measurement_role: SensorRole = "ambient"
    elevation_mm: int | None = Field(default=None, ge=0, le=100000)
    flow_direction_deg: int | None = Field(default=None, ge=0, le=359)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _direction(self) -> "SensorIn":
        if self.flow_direction_deg is not None and self.sensor_kind not in ("airflow", "combined"):
            raise ValueError("flow_direction_deg applies to airflow or combined sensors only.")
        return self


class SensorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    measurement_role: SensorRole | None = None
    elevation_mm: int | None = Field(default=None, ge=0, le=100000)
    flow_direction_deg: int | None = Field(default=None, ge=0, le=359)
    notes: str | None = Field(default=None, max_length=2000)


class SensorMappingOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    canonical_metric: str
    source_identifier: str
    unit: str


class SensorOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    serial_number: str | None
    site_id: uuid.UUID
    name: str
    sensor_kind: str
    measurement_role: str
    elevation_mm: int | None
    flow_direction_deg: int | None
    notes: str | None
    lifecycle_status: str
    allowed_metrics: list[str]
    version: int
    placement: PlacementOut | None
    # Present only for callers holding telemetry:read.
    mappings: list[SensorMappingOut] | None = None


async def _sensor_out(db: AsyncSession, ctx: AuthContext, sensor: EnvironmentalSensor, asset: ManagedAsset) -> SensorOut:
    mappings: list[SensorMappingOut] | None = None
    if ctx.has_permission("telemetry:read"):
        mappings = [
            SensorMappingOut(id=m.id, integration_id=m.integration_id, canonical_metric=m.canonical_metric, source_identifier=m.source_identifier, unit=m.unit)
            for m in (
                await db.execute(select(IntegrationMetricMapping).where(IntegrationMetricMapping.managed_asset_id == sensor.id).order_by(IntegrationMetricMapping.created_at))
            ).scalars()
        ]
    return SensorOut(
        id=sensor.id, asset_tag=asset.asset_tag, serial_number=asset.serial_number, site_id=sensor.site_id, name=sensor.name,
        sensor_kind=sensor.sensor_kind, measurement_role=sensor.measurement_role, elevation_mm=sensor.elevation_mm,
        flow_direction_deg=sensor.flow_direction_deg, notes=sensor.notes, lifecycle_status=asset.lifecycle_status,
        allowed_metrics=list(SENSOR_KIND_METRICS[sensor.sensor_kind]), version=sensor.version,
        placement=_placement_out(await get_current_equipment_placement(db, sensor.id)), mappings=mappings,
    )


async def _visible_sensor(db: AsyncSession, ctx: AuthContext, sensor_id: uuid.UUID) -> tuple[EnvironmentalSensor, ManagedAsset]:
    row = (
        await db.execute(select(EnvironmentalSensor, ManagedAsset).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id).where(EnvironmentalSensor.id == sensor_id))
    ).first()
    if row is None or not _site_visible(ctx, row[0].site_id):
        raise NotFoundError(f"Sensor {sensor_id} not found.")
    return row[0], row[1]


@router.post("/sensors", response_model=SensorOut, status_code=201)
async def create_sensor(
    body: SensorIn, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> SensorOut:
    await _require_site(db, ctx, body.site_id)
    asset = ManagedAsset(asset_type="sensor", asset_tag=body.asset_tag.strip(), serial_number=body.serial_number, lifecycle_status="planned")
    db.add(asset)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(detail="That asset_tag is already in use.") from exc
    sensor = EnvironmentalSensor(
        id=asset.id, site_id=body.site_id, name=body.name.strip(), sensor_kind=body.sensor_kind, measurement_role=body.measurement_role,
        elevation_mm=body.elevation_mm, flow_direction_deg=body.flow_direction_deg, notes=body.notes,
    )
    db.add(sensor)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.sensor.create", entity_type="sensor", entity_id=asset.id,
                          request_id=request_id, correlation_id=correlation_id,
                          after={"asset_tag": asset.asset_tag, "sensor_kind": sensor.sensor_kind, "site_id": str(sensor.site_id)})
    await write_outbox_event(db, event_type="EnvironmentalSensorCreated", aggregate_type="environmental_sensor", aggregate_id=asset.id,
                             payload={"sensor_kind": sensor.sensor_kind, "site_id": str(sensor.site_id)}, correlation_id=correlation_id)
    await db.commit()
    await db.refresh(sensor)
    return await _sensor_out(db, ctx, sensor, asset)


@router.get("/sensors", response_model=Page[SensorOut])
async def list_sensors(
    site_id: uuid.UUID | None = None, room_id: uuid.UUID | None = None, sensor_kind: SensorKind | None = None,
    page: Pagination = Depends(pagination_params), db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read")),
) -> Page[SensorOut]:
    stmt = select(EnvironmentalSensor, ManagedAsset).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id)
    if site_id is not None:
        stmt = stmt.where(EnvironmentalSensor.site_id == site_id)
    if sensor_kind is not None:
        stmt = stmt.where(EnvironmentalSensor.sensor_kind == sensor_kind)
    if room_id is not None:
        stmt = stmt.join(EquipmentPlacement, EquipmentPlacement.equipment_id == EnvironmentalSensor.id).where(
            EquipmentPlacement.room_id == room_id, EquipmentPlacement.effective_to.is_(None)
        )
    if not ctx.scope.unrestricted:
        stmt = stmt.where(EnvironmentalSensor.site_id.in_(ctx.scope.site_ids))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(EnvironmentalSensor.name, EnvironmentalSensor.id).limit(page.limit).offset(page.offset))).all()
    return Page(items=[await _sensor_out(db, ctx, s, a) for s, a in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/sensors/{sensor_id}", response_model=SensorOut)
async def get_sensor(sensor_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read"))) -> SensorOut:
    sensor, asset = await _visible_sensor(db, ctx, sensor_id)
    return await _sensor_out(db, ctx, sensor, asset)


@router.patch("/sensors/{sensor_id}", response_model=SensorOut)
async def update_sensor(
    sensor_id: uuid.UUID, body: SensorPatch, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> SensorOut:
    _, asset = await _visible_sensor(db, ctx, sensor_id)
    sensor = await lock_versioned_row(db, EnvironmentalSensor, sensor_id, expected_version=if_match, label="Sensor")
    changes = body.model_dump(exclude_unset=True)
    for non_null in ("name", "measurement_role"):
        if non_null in changes and changes[non_null] is None:
            raise ApiError(status_code=422, title="Validation Error", detail=f"{non_null} cannot be null.")
    if changes.get("flow_direction_deg") is not None and sensor.sensor_kind not in ("airflow", "combined"):
        raise ApiError(status_code=422, title="Validation Error", detail="flow_direction_deg applies to airflow or combined sensors only.")
    before = {k: getattr(sensor, k) for k in changes}
    for key, value in changes.items():
        setattr(sensor, key, value.strip() if key == "name" and isinstance(value, str) else value)
    sensor.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.sensor.update", entity_type="sensor", entity_id=sensor.id,
                          request_id=request_id, correlation_id=correlation_id, before=before, after=changes)
    await db.commit()
    return await _sensor_out(db, ctx, sensor, asset)


@router.put("/sensors/{sensor_id}/placement", response_model=PlacementOut)
async def place_sensor(
    sensor_id: uuid.UUID, body: PlacementIn, request: Request, if_match: str | None = None, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> PlacementOut:
    sensor, asset = await _visible_sensor(db, ctx, sensor_id)
    if asset.lifecycle_status in ("decommissioned", "removed"):
        raise ConflictError(detail="A retired sensor cannot be placed.")
    placement = await _place_asset(
        db, request, ctx, asset_id=sensor.id, site_id=sensor.site_id, label=sensor.name, body=body,
        if_match_version=parse_if_match(if_match), allow_rack_mounted=True,
    )
    await db.commit()
    out = _placement_out(placement)
    assert out is not None
    return out


@router.delete("/sensors/{sensor_id}/placement", status_code=204)
async def unplace_sensor(
    sensor_id: uuid.UUID, request: Request, if_match: str | None = None, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> None:
    sensor, _ = await _visible_sensor(db, ctx, sensor_id)
    await _unplace(db, request, ctx, sensor.id, parse_if_match(if_match))


# ----------------------------------------------------------------------------------------------- thermal zones


class ZoneIn(BaseModel):
    room_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    zone_kind: ZoneKind
    containment: Literal["none", "contained"] = "none"
    geometry_type: Literal["rect", "polygon"] | None = None
    x_mm: int | None = None
    y_mm: int | None = None
    width_mm: int | None = None
    height_mm: int | None = None
    points: list[list[int]] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    containment: Literal["none", "contained"] | None = None
    geometry_type: Literal["rect", "polygon"] | None = None
    x_mm: int | None = None
    y_mm: int | None = None
    width_mm: int | None = None
    height_mm: int | None = None
    points: list[list[int]] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ContainmentElementIn(BaseModel):
    element_kind: Literal["boundary", "opening"]
    x1_mm: int
    y1_mm: int
    x2_mm: int
    y2_mm: int
    label: str | None = Field(default=None, max_length=128)


class ContainmentElementOut(ContainmentElementIn):
    id: uuid.UUID
    thermal_zone_id: uuid.UUID


class ZoneOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    name: str
    zone_kind: str
    containment: str
    geometry_type: str | None
    x_mm: int | None
    y_mm: int | None
    width_mm: int | None
    height_mm: int | None
    points: list[list[int]] | None
    notes: str | None
    retired: bool
    version: int
    geometry_validation: str
    authority: str = "operator_configured"
    elements: list[ContainmentElementOut] = []


async def _zone_out(db: AsyncSession, zone: ThermalZone) -> ZoneOut:
    elements = (await db.execute(select(ContainmentElement).where(ContainmentElement.thermal_zone_id == zone.id).order_by(ContainmentElement.created_at, ContainmentElement.id))).scalars().all()
    shape = ZoneShape(zone.geometry_type, zone.x_mm, zone.y_mm, zone.width_mm, zone.height_mm, zone.points)
    polygon = shape.polygon()
    validation = "no_geometry"
    if polygon is not None:
        try:
            validation = await check_inside_room(db, zone.room_id, polygon, zone.name)
        except ApiError:
            validation = "outside_room_boundary"
    return ZoneOut(
        id=zone.id, room_id=zone.room_id, name=zone.name, zone_kind=zone.zone_kind, containment=zone.containment, geometry_type=zone.geometry_type,
        x_mm=zone.x_mm, y_mm=zone.y_mm, width_mm=zone.width_mm, height_mm=zone.height_mm, points=zone.points, notes=zone.notes,
        retired=zone.retired, version=zone.version, geometry_validation=validation,
        elements=[ContainmentElementOut(id=e.id, thermal_zone_id=e.thermal_zone_id, element_kind=e.element_kind, x1_mm=e.x1_mm, y1_mm=e.y1_mm, x2_mm=e.x2_mm, y2_mm=e.y2_mm, label=e.label) for e in elements],
    )


async def _visible_zone(db: AsyncSession, ctx: AuthContext, zone_id: uuid.UUID) -> ThermalZone:
    zone = await db.get(ThermalZone, zone_id)
    if zone is None:
        raise NotFoundError(f"Thermal zone {zone_id} not found.")
    site_id = await room_site_id(db, zone.room_id)
    if site_id is None or not _site_visible(ctx, site_id):
        raise NotFoundError(f"Thermal zone {zone_id} not found.")
    return zone


@router.post("/zones", response_model=ZoneOut, status_code=201)
async def create_zone(
    body: ZoneIn, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> ZoneOut:
    await _room_site(db, ctx, body.room_id)
    if body.containment == "contained" and body.zone_kind not in ("hot_aisle", "cold_aisle"):
        raise ApiError(status_code=422, title="Validation Error", detail="Containment applies to hot or cold aisles only.")
    if body.geometry_type is None and body.zone_kind != "served_zone":
        raise ApiError(status_code=422, title="Validation Error", detail="Only a served zone may omit geometry (it then covers the whole room).")
    shape = ZoneShape(body.geometry_type, body.x_mm, body.y_mm, body.width_mm, body.height_mm, body.points)
    validate_shape(shape)
    polygon = shape.polygon()
    if polygon is not None:
        await check_inside_room(db, body.room_id, polygon, body.name)
    zone = ThermalZone(
        room_id=body.room_id, name=body.name.strip(), zone_kind=body.zone_kind, containment=body.containment, geometry_type=body.geometry_type,
        x_mm=body.x_mm, y_mm=body.y_mm, width_mm=body.width_mm, height_mm=body.height_mm, points=body.points, notes=body.notes,
    )
    db.add(zone)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_thermal_zone_room_name" not in str(exc.orig):
            raise
        raise ConflictError(detail="A zone with that name already exists in this room.") from exc
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.zone.create", entity_type="thermal_zone", entity_id=zone.id,
                          request_id=request_id, correlation_id=correlation_id,
                          after={"room_id": str(zone.room_id), "name": zone.name, "zone_kind": zone.zone_kind, "containment": zone.containment, "geometry_type": zone.geometry_type})
    await write_outbox_event(db, event_type="ThermalZoneCreated", aggregate_type="thermal_zone", aggregate_id=zone.id,
                             payload={"room_id": str(zone.room_id), "zone_kind": zone.zone_kind}, correlation_id=correlation_id)
    await db.commit()
    return await _zone_out(db, zone)


@router.get("/zones", response_model=Page[ZoneOut])
async def list_zones(
    room_id: uuid.UUID, zone_kind: ZoneKind | None = None, include_retired: bool = False, page: Pagination = Depends(pagination_params),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read")),
) -> Page[ZoneOut]:
    await _room_site(db, ctx, room_id)
    stmt = select(ThermalZone).where(ThermalZone.room_id == room_id)
    if zone_kind is not None:
        stmt = stmt.where(ThermalZone.zone_kind == zone_kind)
    if not include_retired:
        stmt = stmt.where(ThermalZone.retired.is_(False))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(ThermalZone.name, ThermalZone.id).limit(page.limit).offset(page.offset))).scalars().all()
    return Page(items=[await _zone_out(db, z) for z in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/zones/{zone_id}", response_model=ZoneOut)
async def get_zone(zone_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:read"))) -> ZoneOut:
    return await _zone_out(db, await _visible_zone(db, ctx, zone_id))


@router.patch("/zones/{zone_id}", response_model=ZoneOut)
async def update_zone(
    zone_id: uuid.UUID, body: ZonePatch, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> ZoneOut:
    visible = await _visible_zone(db, ctx, zone_id)
    if visible.retired:
        raise ConflictError(detail="A retired zone cannot be edited.")
    zone = await lock_versioned_row(db, ThermalZone, zone_id, expected_version=if_match, label="Thermal zone")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise ApiError(status_code=422, title="Validation Error", detail="name cannot be null.")
    geometry_keys = {"geometry_type", "x_mm", "y_mm", "width_mm", "height_mm", "points"}
    before = {k: getattr(zone, k) for k in changes}
    if geometry_keys & set(changes):
        merged = {k: changes.get(k, getattr(zone, k)) for k in geometry_keys}
        if "geometry_type" in changes and changes["geometry_type"] != zone.geometry_type:
            # switching shape: a field the caller did not restate belongs to the old shape and is cleared
            merged = {k: changes.get(k) for k in geometry_keys}
        if merged["geometry_type"] is None and zone.zone_kind != "served_zone":
            raise ApiError(status_code=422, title="Validation Error", detail="Only a served zone may omit geometry.")
        shape = ZoneShape(merged["geometry_type"], merged["x_mm"], merged["y_mm"], merged["width_mm"], merged["height_mm"], merged["points"])
        validate_shape(shape)
        polygon = shape.polygon()
        if polygon is not None:
            await check_inside_room(db, zone.room_id, polygon, zone.name)
        for key, value in merged.items():
            setattr(zone, key, value)
    if "containment" in changes:
        if changes["containment"] == "contained" and zone.zone_kind not in ("hot_aisle", "cold_aisle"):
            raise ApiError(status_code=422, title="Validation Error", detail="Containment applies to hot or cold aisles only.")
        zone.containment = changes["containment"]
        if zone.containment == "none":
            await db.execute(ContainmentElement.__table__.delete().where(ContainmentElement.thermal_zone_id == zone.id))
    if "name" in changes:
        zone.name = changes["name"].strip()
    if "notes" in changes:
        zone.notes = changes["notes"]
    zone.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.zone.update", entity_type="thermal_zone", entity_id=zone.id,
                          request_id=request_id, correlation_id=correlation_id, before=before, after=changes)
    await write_outbox_event(db, event_type="ThermalZoneUpdated", aggregate_type="thermal_zone", aggregate_id=zone.id,
                             payload={"fields": sorted(changes)}, correlation_id=correlation_id)
    await _commit_or_conflict(db, "A zone with that name already exists in this room.")
    return await _zone_out(db, zone)


@router.post("/zones/{zone_id}/retire", response_model=ZoneOut)
async def retire_zone(
    zone_id: uuid.UUID, request: Request, if_match: int = Depends(require_if_match), db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> ZoneOut:
    await _visible_zone(db, ctx, zone_id)
    zone = await lock_versioned_row(db, ThermalZone, zone_id, expected_version=if_match, label="Thermal zone")
    related = (await db.execute(select(func.count()).select_from(CoolingUnitZone).where(CoolingUnitZone.thermal_zone_id == zone.id))).scalar_one()
    if related:
        raise ConflictError(detail=f"{related} cooling unit relationship(s) still reference this zone; remove them first.")
    zone.retired = True
    zone.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.zone.retire", entity_type="thermal_zone", entity_id=zone.id,
                          request_id=request_id, correlation_id=correlation_id)
    await db.commit()
    return await _zone_out(db, zone)


@router.post("/zones/{zone_id}/containment-elements", response_model=ContainmentElementOut, status_code=201)
async def add_containment_element(
    zone_id: uuid.UUID, body: ContainmentElementIn, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> ContainmentElementOut:
    await _visible_zone(db, ctx, zone_id)
    zone = await lock_versioned_row(db, ThermalZone, zone_id, expected_version=if_match, label="Thermal zone")
    if zone.containment != "contained":
        raise ApiError(status_code=422, title="Validation Error", detail="Containment boundaries and openings belong to a contained aisle.")
    for name, value in (("x1_mm", body.x1_mm), ("y1_mm", body.y1_mm), ("x2_mm", body.x2_mm), ("y2_mm", body.y2_mm)):
        validate_coordinate(name, value)
    if (body.x1_mm, body.y1_mm) == (body.x2_mm, body.y2_mm):
        raise ApiError(status_code=422, title="Validation Error", detail="A containment element needs two distinct end points.")
    await check_point_inside_room(db, zone.room_id, (float(body.x1_mm), float(body.y1_mm)), "Containment element start")
    await check_point_inside_room(db, zone.room_id, (float(body.x2_mm), float(body.y2_mm)), "Containment element end")
    element = ContainmentElement(thermal_zone_id=zone.id, **body.model_dump())
    db.add(element)
    zone.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.containment_element.add", entity_type="thermal_zone", entity_id=zone.id,
                          request_id=request_id, correlation_id=correlation_id, after=body.model_dump())
    await db.commit()
    return ContainmentElementOut(id=element.id, thermal_zone_id=zone.id, **body.model_dump())


@router.delete("/zones/{zone_id}/containment-elements/{element_id}", status_code=204)
async def remove_containment_element(
    zone_id: uuid.UUID, element_id: uuid.UUID, request: Request, if_match: int = Depends(require_if_match),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage")),
) -> None:
    await _visible_zone(db, ctx, zone_id)
    zone = await lock_versioned_row(db, ThermalZone, zone_id, expected_version=if_match, label="Thermal zone")
    element = await db.get(ContainmentElement, element_id)
    if element is None or element.thermal_zone_id != zone.id:
        raise NotFoundError(f"Containment element {element_id} not found.")
    await db.delete(element)
    zone.version += 1
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.containment_element.remove", entity_type="thermal_zone", entity_id=zone.id,
                          request_id=request_id, correlation_id=correlation_id, before={"element_id": str(element_id)})
    await db.commit()


# ----------------------------------------------------------------------------------------------- unit <-> zone relations


class RelationIn(BaseModel):
    cooling_unit_id: uuid.UUID
    thermal_zone_id: uuid.UUID
    relation_kind: RelationKind
    semantics: Semantics = "configured"


@router.post("/relations", response_model=ZoneRelationOut, status_code=201)
async def create_relation(
    body: RelationIn, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> ZoneRelationOut:
    unit, asset = await _visible_unit(db, ctx, body.cooling_unit_id)
    zone = await _visible_zone(db, ctx, body.thermal_zone_id)
    if zone.retired:
        raise ApiError(status_code=422, title="Validation Error", detail="The zone is retired.")
    if asset.lifecycle_status in ("decommissioned", "removed"):
        raise ConflictError(detail="A retired cooling unit cannot gain relationships.")
    if zone.zone_kind not in RELATION_TARGETS[body.relation_kind]:
        raise ApiError(
            status_code=422, title="Validation Error",
            detail=f"A unit '{body.relation_kind}' zones of kind {list(RELATION_TARGETS[body.relation_kind])}, not {zone.zone_kind!r}.",
        )
    if body.relation_kind == "serves" and unit.unit_kind not in COOLING_UNIT_KINDS:
        raise ApiError(status_code=422, title="Validation Error", detail="Unsupported unit kind.")
    if await room_site_id(db, zone.room_id) != unit.site_id:
        raise ApiError(status_code=422, title="Cross-Site Relationship", detail="The unit and the zone belong to different sites.")
    relation = CoolingUnitZone(cooling_unit_id=unit.id, thermal_zone_id=zone.id, relation_kind=body.relation_kind, semantics=body.semantics)
    db.add(relation)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "retired" in str(exc.orig):
            raise ConflictError(detail="The unit or the zone was retired while you were editing; reload and try again.") from exc
        raise ConflictError(detail="That relationship already exists.") from exc
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.relation.create", entity_type="cooling_unit_zone", entity_id=relation.id,
                          request_id=request_id, correlation_id=correlation_id, after=body.model_dump(mode="json"))
    await write_outbox_event(db, event_type="CoolingRelationCreated", aggregate_type="cooling_unit_zone", aggregate_id=relation.id,
                             payload=body.model_dump(mode="json"), correlation_id=correlation_id)
    await db.commit()
    return ZoneRelationOut(id=relation.id, cooling_unit_id=unit.id, thermal_zone_id=zone.id, relation_kind=relation.relation_kind, semantics=relation.semantics)


@router.get("/relations", response_model=list[ZoneRelationOut])
async def list_relations(
    cooling_unit_id: uuid.UUID | None = None, thermal_zone_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cooling:read")),
) -> list[ZoneRelationOut]:
    if cooling_unit_id is None and thermal_zone_id is None:
        raise ApiError(status_code=422, title="Validation Error", detail="Filter by cooling_unit_id or thermal_zone_id.")
    if cooling_unit_id is not None:
        await _visible_unit(db, ctx, cooling_unit_id)
    if thermal_zone_id is not None:
        await _visible_zone(db, ctx, thermal_zone_id)
    stmt = select(CoolingUnitZone)
    if cooling_unit_id is not None:
        stmt = stmt.where(CoolingUnitZone.cooling_unit_id == cooling_unit_id)
    if thermal_zone_id is not None:
        stmt = stmt.where(CoolingUnitZone.thermal_zone_id == thermal_zone_id)
    rows = (await db.execute(stmt.order_by(CoolingUnitZone.created_at, CoolingUnitZone.id).limit(500))).scalars().all()
    return [ZoneRelationOut(id=r.id, cooling_unit_id=r.cooling_unit_id, thermal_zone_id=r.thermal_zone_id, relation_kind=r.relation_kind, semantics=r.semantics) for r in rows]


@router.delete("/relations/{relation_id}", status_code=204)
async def delete_relation(
    relation_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cooling:manage"))
) -> None:
    relation = await db.get(CoolingUnitZone, relation_id)
    if relation is None:
        raise NotFoundError(f"Relationship {relation_id} not found.")
    await _visible_unit(db, ctx, relation.cooling_unit_id)
    request_id, correlation_id = _ids(request)
    await write_audit_log(db, actor_user_id=ctx.user.id, action="cooling.relation.delete", entity_type="cooling_unit_zone", entity_id=relation.id,
                          request_id=request_id, correlation_id=correlation_id,
                          before={"cooling_unit_id": str(relation.cooling_unit_id), "thermal_zone_id": str(relation.thermal_zone_id), "relation_kind": relation.relation_kind})
    await db.delete(relation)
    await db.commit()


