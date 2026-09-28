"""Spatial query endpoints (Phase 2 prompt §2 item 17 "Spatial APIs"; ARCHITECTURE_REVIEW.md
§40: "every renderer consumes the same query result — the 2D floor-plan view, the rack
elevation view, and any future 3D view are all projections of the same authoritative
data, never independent stores"). This module is read-only: it composes data already
owned by `racks`/`equipment`/`floor_plans` into the single combined shape the 2D
floor-plan viewer needs, and creates nothing of its own."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Room
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.spatial.models import FloorPlan, SpatialLayer, SpatialObject

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

    model_config = {"from_attributes": True}


class RoomEquipmentOut(BaseModel):
    """Only equipment placed directly in the room (not rack-mounted) — rack-mounted
    equipment is part of its rack's elevation, not the 2D room view (§8)."""

    id: uuid.UUID
    asset_tag: str
    hostname: str | None
    placement_type: str
    spatial_object_id: uuid.UUID | None

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
            height_u=model_revision.height_u,
        )
        for placement, rack, asset, model_revision in rack_rows
    ]
    room_rack_ids = {rack.id for rack in racks}

    equipment_rows = (
        await db.execute(
            select(EquipmentPlacement, Equipment, ManagedAsset)
            .join(Equipment, Equipment.id == EquipmentPlacement.equipment_id)
            .join(ManagedAsset, ManagedAsset.id == EquipmentPlacement.equipment_id)
            .where(
                EquipmentPlacement.room_id == room_id,
                EquipmentPlacement.effective_to.is_(None),
                EquipmentPlacement.placement_type != "rack_mounted",
            )
        )
    ).all()
    equipment = [
        RoomEquipmentOut(
            id=equipment.id, asset_tag=asset.asset_tag, hostname=equipment.hostname,
            placement_type=placement.placement_type, spatial_object_id=placement.spatial_object_id,
        )
        for placement, equipment, asset in equipment_rows
    ]

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
    if active_floor_plan is not None:
        object_rows = (
            await db.execute(
                select(SpatialObject)
                .join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
                .where(SpatialLayer.floor_plan_id == active_floor_plan.id)
            )
        ).scalars().all()
        objects = [SpatialObjectOut.model_validate(obj) for obj in object_rows]

    from datetime import UTC

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
    )
