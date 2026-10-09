"""Room-boundary containment. The boundary is the floor plan's `room_outline` SpatialObject (a rectangle or a
polygon in canonical mm). A rack or floor-standing footprint must lie inside it unless the operator records an
explicit, audited exception reason. A floor plan with no boundary imposes no constraint (incomplete data is
reported by the spatial view, not silently assumed)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.spatial_import.geometry import Point, polygon_contains_polygon, rect_corners
from app.core.errors import ApiError
from app.domain.spatial.models import SpatialLayer, SpatialObject

BOUNDARY_TOLERANCE_MM = 5.0


def object_polygon(obj: SpatialObject) -> list[Point] | None:
    data = obj.geometry_data or {}
    points = data.get("points") if isinstance(data, dict) else None
    if points and len(points) >= 3:
        return [(float(p[0]), float(p[1])) for p in points]
    if obj.width_mm and obj.height_mm:
        return rect_corners(obj.x_mm, obj.y_mm, obj.width_mm, obj.height_mm, obj.rotation_deg or 0)
    return None


async def load_boundary_object(db: AsyncSession, floor_plan_id: uuid.UUID) -> SpatialObject | None:
    stmt = (
        select(SpatialObject)
        .join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
        .where(SpatialLayer.floor_plan_id == floor_plan_id, SpatialObject.object_type == "room_outline")
        .order_by(SpatialObject.created_at)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def load_boundary(db: AsyncSession, floor_plan_id: uuid.UUID) -> list[Point] | None:
    obj = await load_boundary_object(db, floor_plan_id)
    return None if obj is None else object_polygon(obj)


def footprint_inside(boundary: list[Point], x: float, y: float, width: float, height: float, rotation_deg: float) -> bool:
    return polygon_contains_polygon(boundary, rect_corners(x, y, width, height, rotation_deg), tolerance=BOUNDARY_TOLERANCE_MM)


async def assert_within_boundary(
    db: AsyncSession, *, floor_plan_id: uuid.UUID | None, x: float, y: float, width: float, height: float,
    rotation_deg: float, label: str, exception_reason: str | None,
) -> dict[str, str] | None:
    """Raises 422 when the footprint leaves the boundary and no exception reason is given. Returns the exception
    record (for audit) when an override was used, else None."""
    if floor_plan_id is None:
        return None
    boundary = await load_boundary(db, floor_plan_id)
    if boundary is None or footprint_inside(boundary, x, y, width, height, rotation_deg):
        return None
    if exception_reason and exception_reason.strip():
        return {"code": "outside_room_boundary", "label": label, "reason": exception_reason.strip()[:500]}
    raise ApiError(
        status_code=422, title="Outside Room Boundary",
        detail=f"{label} lies outside the approved room boundary. Move it inside, or record a boundary exception reason.",
    )


async def assert_rack_within_room_boundary(
    db: AsyncSession, *, model_revision_id: uuid.UUID, room_id: uuid.UUID | None, x_mm: int | None, y_mm: int | None,
    rotation_deg: int | None, label: str, exception_reason: str | None,
) -> dict[str, str] | None:
    """Placement-time guard: a rack placed with explicit coordinates must sit inside the room's approved boundary
    (the active floor plan's `room_outline`). Rooms without an active plan or boundary are unconstrained."""
    if room_id is None or x_mm is None or y_mm is None:
        return None
    from app.domain.catalog.models import RackModelRevision
    from app.domain.spatial.models import FloorPlan

    revision = await db.get(RackModelRevision, model_revision_id)
    if revision is None:
        return None
    floor_plan_id = (
        await db.execute(select(FloorPlan.id).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))
    ).scalar_one_or_none()
    return await assert_within_boundary(
        db, floor_plan_id=floor_plan_id, x=x_mm, y=y_mm, width=revision.width_mm, height=revision.depth_mm,
        rotation_deg=rotation_deg or 0, label=label, exception_reason=exception_reason,
    )
