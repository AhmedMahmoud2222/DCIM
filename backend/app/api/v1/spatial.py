"""Spatial query endpoints (Phase 2 prompt §2 item 17 "Spatial APIs"; ARCHITECTURE_REVIEW.md
§40: "every renderer consumes the same query result — the 2D floor-plan view, the rack
elevation view, and any future 3D view are all projections of the same authoritative
data, never independent stores"). This module is read-only: it composes data already
owned by `racks`/`equipment`/`floor_plans` into the single combined shape the 2D
floor-plan viewer needs, and creates nothing of its own."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application import spatial_overlays
from app.application.rbac import AuthContext, get_auth_context, require_permission
from app.application.spatial_import.boundary import load_boundary_object
from app.core.errors import ApiError, ForbiddenError, NotFoundError
from app.domain.catalog.models import EquipmentModelRevision, RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Room
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.spatial.models import FloorPlan, FloorPlanCalibration, SpatialLayer, SpatialObject

router = APIRouter(prefix="/spatial", tags=["spatial"])


class RoomRackOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    name: str
    x_mm: int | None
    y_mm: int | None
    rotation_deg: int | None
    spatial_object_id: uuid.UUID | None
    # Additive (3D layout increment): the rack's own U capacity, needed to scale a
    # rack's mounted equipment within its face — every other field on this model
    # already existed, this is read from the same RackModelRevision the 2D rack
    # elevation view (`GET /racks/{id}/elevation`) already uses.
    height_u: int
    # Issue #104: authoritative physical dimensions from the rack's catalog revision (never a CSS constant).
    # `height_mm` is height_u x the EIA-310 rack unit (44.45 mm); frame/plinth overhead is not invented.
    width_mm: int | None = None
    depth_mm: int | None = None
    height_mm: int | None = None
    position_state: str = "placed"  # placed | missing (no authoritative x/y)
    placement_version: int = 1

    model_config = {"from_attributes": True}


class RoomEquipmentOut(BaseModel):
    """Only equipment placed directly in the room (not rack-mounted) — rack-mounted
    equipment is part of its rack's elevation, not the 2D room view (§8)."""

    id: uuid.UUID
    asset_tag: str
    hostname: str | None
    placement_type: str
    spatial_object_id: uuid.UUID | None
    # Issue #104: floor/wall/ceiling-mounted equipment position comes from its linked spatial object and
    # footprint from its catalog revision; any missing piece is reported, never defaulted.
    x_mm: int | None = None
    y_mm: int | None = None
    rotation_deg: int | None = None
    width_mm: int | None = None
    depth_mm: int | None = None
    height_mm: int | None = None
    position_state: str = "missing"  # placed | missing
    dimensions_state: str = "missing"  # complete | partial | missing

    model_config = {"from_attributes": True}


class RoomRackEquipmentOut(BaseModel):
    """Additive (3D layout increment): rack-mounted equipment placed in this room,
    with the same authoritative U-range/side data `GET /racks/{id}/elevation`
    returns — kept as a separate list from `equipment` above rather than folded into
    it, so the existing "equipment excludes rack-mounted" contract (§8) is untouched
    and every renderer still reads from the same query result (§40)."""

    id: uuid.UUID
    rack_id: uuid.UUID
    asset_tag: str
    hostname: str | None
    u_start: int
    u_end: int
    side: str

    model_config = {"from_attributes": True}


class SpatialObjectOut(BaseModel):
    id: uuid.UUID
    object_type: str
    geometry_type: str
    x_mm: int
    y_mm: int
    width_mm: int | None
    height_mm: int | None
    rotation_deg: int
    geometry_data: dict | None
    label: str | None
    source: str
    provenance: dict | None = None

    model_config = {"from_attributes": True}


RACK_UNIT_MM = 44.45


class SpatialCalibrationOut(BaseModel):
    id: uuid.UUID
    method: str
    source_units: str
    mm_per_unit: float
    error_bound_mm: float | None
    relative_error: float | None
    confidence: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RoomSpatialViewOut(BaseModel):
    room_id: uuid.UUID
    room_name: str
    active_floor_plan_id: uuid.UUID | None
    active_floor_plan_revision: int | None
    room_width_mm: int | None
    room_height_mm: int | None
    generated_at: datetime
    racks: list[RoomRackOut]
    equipment: list[RoomEquipmentOut]
    objects: list[SpatialObjectOut]
    # Additive (3D layout increment): rack-mounted equipment for the racks above,
    # omitted from `equipment` by design (§8) — see RoomRackEquipmentOut.
    rack_equipment: list[RoomRackEquipmentOut]
    # Issue #104
    calibration: SpatialCalibrationOut | None = None
    boundary: SpatialObjectOut | None = None
    rack_unit_mm: float = RACK_UNIT_MM
    layout_state: str = "incomplete"  # validated | incomplete
    incomplete_reasons: list[str] = []


@router.get("/rooms/{room_id}/view", response_model=RoomSpatialViewOut)
async def get_room_spatial_view(
    room_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("spatial:read"))
) -> RoomSpatialViewOut:
    """Data source for the 2D floor-plan viewer: the room's active FloorPlan (if any),
    every rack and non-rack-mounted equipment currently placed in the room (read straight
    from RackPlacement/EquipmentPlacement — always the authoritative position, never a
    cached copy), and the active FloorPlan's own SpatialObjects (background/annotation
    layers, plus any accepted import candidates)."""
    room = await db.get(Room, room_id)
    if room is None:
        raise NotFoundError(f"Room {room_id} not found.")

    active_floor_plan = (
        await db.execute(select(FloorPlan).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))
    ).scalar_one_or_none()

    rack_rows = (
        await db.execute(
            select(RackPlacement, Rack, ManagedAsset, RackModelRevision)
            .join(Rack, Rack.id == RackPlacement.rack_id)
            .join(ManagedAsset, ManagedAsset.id == RackPlacement.rack_id)
            .join(RackModelRevision, RackModelRevision.id == Rack.model_revision_id)
            .where(RackPlacement.room_id == room_id, RackPlacement.effective_to.is_(None))
        )
    ).all()
    racks = [
        RoomRackOut(
            id=rack.id, asset_tag=asset.asset_tag, name=rack.name, x_mm=placement.x_mm, y_mm=placement.y_mm,
            rotation_deg=placement.rotation_deg, spatial_object_id=placement.spatial_object_id,
            height_u=model_revision.height_u, width_mm=model_revision.width_mm, depth_mm=model_revision.depth_mm,
            height_mm=round(model_revision.height_u * RACK_UNIT_MM), placement_version=placement.version,
            position_state="placed" if placement.x_mm is not None and placement.y_mm is not None else "missing",
        )
        for placement, rack, asset, model_revision in rack_rows
    ]
    room_rack_ids = {rack.id for rack in racks}

    equipment_rows = (
        await db.execute(
            select(EquipmentPlacement, Equipment, ManagedAsset, EquipmentModelRevision, SpatialObject)
            .join(Equipment, Equipment.id == EquipmentPlacement.equipment_id)
            .join(ManagedAsset, ManagedAsset.id == EquipmentPlacement.equipment_id)
            .join(EquipmentModelRevision, EquipmentModelRevision.id == Equipment.model_revision_id)
            .outerjoin(SpatialObject, SpatialObject.id == EquipmentPlacement.spatial_object_id)
            .where(
                EquipmentPlacement.room_id == room_id,
                EquipmentPlacement.effective_to.is_(None),
                EquipmentPlacement.placement_type != "rack_mounted",
            )
        )
    ).all()
    equipment: list[RoomEquipmentOut] = []
    for placement, equipment_row, asset, revision, spatial_object in equipment_rows:
        width, depth = revision.width_mm, revision.depth_mm
        height = round(revision.height_u * RACK_UNIT_MM) if revision.height_u else None
        known = [v is not None for v in (width, depth, height)]
        rotation = placement.rotation_deg
        if rotation is None and spatial_object is not None:
            rotation = spatial_object.rotation_deg
        equipment.append(
            RoomEquipmentOut(
                id=equipment_row.id, asset_tag=asset.asset_tag, hostname=equipment_row.hostname,
                placement_type=placement.placement_type, spatial_object_id=placement.spatial_object_id,
                x_mm=spatial_object.x_mm if spatial_object is not None else None,
                y_mm=spatial_object.y_mm if spatial_object is not None else None,
                rotation_deg=rotation,
                width_mm=width, depth_mm=depth, height_mm=height,
                position_state="placed" if spatial_object is not None else "missing",
                dimensions_state="complete" if all(known) else ("partial" if any(known) else "missing"),
            )
        )

    rack_equipment: list[RoomRackEquipmentOut] = []
    if room_rack_ids:
        rack_equipment_rows = (
            await db.execute(
                select(EquipmentPlacement, Equipment, ManagedAsset)
                .join(Equipment, Equipment.id == EquipmentPlacement.equipment_id)
                .join(ManagedAsset, ManagedAsset.id == EquipmentPlacement.equipment_id)
                .where(
                    EquipmentPlacement.rack_id.in_(room_rack_ids),
                    EquipmentPlacement.placement_type == "rack_mounted",
                    EquipmentPlacement.effective_to.is_(None),
                )
            )
        ).all()
        for placement, rack_mounted_equipment, asset in rack_equipment_rows:
            u_range = placement.u_range
            # Guaranteed by rack_mounted_requires_rack_u_range_and_side (models.py),
            # same invariant `GET /racks/{id}/elevation` already relies on.
            assert u_range is not None and u_range.lower is not None and u_range.upper is not None
            assert placement.side is not None
            assert placement.rack_id is not None
            rack_equipment.append(
                RoomRackEquipmentOut(
                    id=rack_mounted_equipment.id, rack_id=placement.rack_id, asset_tag=asset.asset_tag,
                    hostname=rack_mounted_equipment.hostname, u_start=u_range.lower, u_end=u_range.upper,
                    side=placement.side,
                )
            )

    objects: list[SpatialObjectOut] = []
    calibration_out: SpatialCalibrationOut | None = None
    boundary_out: SpatialObjectOut | None = None
    if active_floor_plan is not None:
        if active_floor_plan.current_calibration_id is not None:
            calibration_row = await db.get(FloorPlanCalibration, active_floor_plan.current_calibration_id)
            calibration_out = SpatialCalibrationOut.model_validate(calibration_row) if calibration_row is not None else None
        boundary_row = await load_boundary_object(db, active_floor_plan.id)
        boundary_out = SpatialObjectOut.model_validate(boundary_row) if boundary_row is not None else None
        object_rows = (
            await db.execute(
                select(SpatialObject)
                .join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
                .where(SpatialLayer.floor_plan_id == active_floor_plan.id)
            )
        ).scalars().all()
        objects = [SpatialObjectOut.model_validate(obj) for obj in object_rows]

    reasons: list[str] = []
    if active_floor_plan is None:
        reasons.append("no_active_floor_plan")
    else:
        if calibration_out is None:
            reasons.append("no_calibration")
        if boundary_out is None:
            reasons.append("no_room_boundary")
    missing_rack_position = [r for r in racks if r.position_state == "missing"]
    if missing_rack_position:
        reasons.append(f"rack_position_missing:{len(missing_rack_position)}")
    floor_unplaced = [e for e in equipment if e.position_state == "missing"]
    if floor_unplaced:
        reasons.append(f"equipment_position_missing:{len(floor_unplaced)}")
    floor_no_dims = [e for e in equipment if e.dimensions_state != "complete"]
    if floor_no_dims:
        reasons.append(f"equipment_dimensions_missing:{len(floor_no_dims)}")

    return RoomSpatialViewOut(
        room_id=room.id,
        room_name=room.name,
        active_floor_plan_id=active_floor_plan.id if active_floor_plan is not None else None,
        active_floor_plan_revision=active_floor_plan.revision_number if active_floor_plan is not None else None,
        room_width_mm=active_floor_plan.room_width_mm if active_floor_plan is not None else None,
        room_height_mm=active_floor_plan.room_height_mm if active_floor_plan is not None else None,
        generated_at=datetime.now(UTC),
        racks=racks,
        equipment=equipment,
        objects=objects,
        rack_equipment=rack_equipment,
        calibration=calibration_out,
        boundary=boundary_out,
        layout_state="validated" if not reasons else "incomplete",
        incomplete_reasons=reasons,
    )


OVERLAY_KINDS = {
    "power": ("power:read", spatial_overlays.power_overlay),
    "network": ("cable:read", spatial_overlays.network_overlay),
    "environment": ("telemetry:read", spatial_overlays.environment_overlay),
}


def _overlay_permitted(ctx: AuthContext, kind: str) -> bool:
    # Literal codes keep the static permission allow-list check able to see them.
    if kind == "power":
        return ctx.has_permission("power:read")
    if kind == "network":
        return ctx.has_permission("cable:read")
    return ctx.has_permission("telemetry:read")


class RoomOverlaysOut(BaseModel):
    room_id: uuid.UUID
    generated_at: datetime
    overlays: dict[str, Any]


@router.get("/rooms/{room_id}/overlays", response_model=RoomOverlaysOut)
async def get_room_overlays(
    room_id: uuid.UUID,
    kinds: str = Query(default="power,network,environment", description="comma-separated: power, network, environment"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> RoomOverlaysOut:
    """Operational state for the 2D/3D twin, keyed by the same asset ids as the view. Needs spatial:read plus the
    permission of each requested overlay (power:read / cable:read / telemetry:read); a kind the caller may not read
    is a 403, not a silent omission. spatial:read is not site-scope-aware, so a site-restricted user holds no
    effective spatial:read and cannot reach this endpoint at all (fail closed)."""
    if not ctx.has_permission("spatial:read"):
        raise ForbiddenError("Missing required permission: spatial:read")
    requested = [k.strip() for k in kinds.split(",") if k.strip()]
    unknown = [k for k in requested if k not in OVERLAY_KINDS]
    if unknown or not requested:
        raise ApiError(status_code=422, title="Validation Error", detail=f"kinds must be a subset of {sorted(OVERLAY_KINDS)}.")
    for kind in requested:
        if not _overlay_permitted(ctx, kind):
            raise ForbiddenError(f"Missing required permission for the {kind} overlay: {OVERLAY_KINDS[kind][0]}")
    if await db.get(Room, room_id) is None:
        raise NotFoundError(f"Room {room_id} not found.")
    assets = await spatial_overlays.load_room_assets(db, room_id)
    overlays = {kind: await OVERLAY_KINDS[kind][1](db, assets) for kind in dict.fromkeys(requested)}
    return RoomOverlaysOut(room_id=room_id, generated_at=datetime.now(UTC), overlays=overlays)
