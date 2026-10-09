# ruff: noqa: E501
"""Geometry validation for thermal zones and containment elements (Issue #105). Canonical room-local millimetres."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.spatial_import.boundary import load_boundary
from app.application.spatial_import.geometry import Point, point_in_polygon, polygon_area, polygon_contains_polygon, rect_corners
from app.application.spatial_validation import validate_coordinate
from app.core.errors import ApiError
from app.domain.spatial.models import FloorPlan

MAX_POLYGON_POINTS = 64
BOUNDARY_TOLERANCE_MM = 5.0
MIN_ZONE_AREA_MM2 = 100.0 * 100.0


@dataclass(frozen=True)
class ZoneShape:
    geometry_type: str | None
    x_mm: int | None = None
    y_mm: int | None = None
    width_mm: int | None = None
    height_mm: int | None = None
    points: list[list[int]] | None = None

    def polygon(self) -> list[Point] | None:
        if self.geometry_type == "rect":
            assert None not in (self.x_mm, self.y_mm, self.width_mm, self.height_mm)
            return rect_corners(self.x_mm, self.y_mm, self.width_mm, self.height_mm, 0.0)  # type: ignore[arg-type]
        if self.geometry_type == "polygon" and self.points:
            return [(float(p[0]), float(p[1])) for p in self.points]
        return None


def _ccw(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    d1, d2, d3, d4 = _ccw(p3, p4, p1), _ccw(p3, p4, p2), _ccw(p1, p2, p3), _ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4)


def polygon_self_intersects(points: list[Point]) -> bool:
    n = len(points)
    for i in range(n):
        a1, a2 = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_cross(a1, a2, points[j], points[(j + 1) % n]):
                return True
    return False


def validate_shape(shape: ZoneShape) -> None:
    if shape.geometry_type is None:
        return
    if shape.geometry_type == "rect":
        if None in (shape.x_mm, shape.y_mm, shape.width_mm, shape.height_mm) or shape.points is not None:
            raise ApiError(status_code=422, title="Validation Error", detail="A rect zone needs x_mm, y_mm, width_mm and height_mm and no points.")
        if (shape.width_mm or 0) <= 0 or (shape.height_mm or 0) <= 0:
            raise ApiError(status_code=422, title="Validation Error", detail="Zone width and height must be positive.")
        for name, value in (("x_mm", shape.x_mm), ("y_mm", shape.y_mm), ("width_mm", shape.width_mm), ("height_mm", shape.height_mm)):
            validate_coordinate(name, value)
        return
    if shape.geometry_type == "polygon":
        points = shape.points or []
        if shape.x_mm is not None or shape.y_mm is not None or shape.width_mm is not None or shape.height_mm is not None:
            raise ApiError(status_code=422, title="Validation Error", detail="A polygon zone takes points only.")
        if not (3 <= len(points) <= MAX_POLYGON_POINTS):
            raise ApiError(status_code=422, title="Validation Error", detail=f"A polygon needs 3 to {MAX_POLYGON_POINTS} points.")
        for p in points:
            if len(p) != 2:
                raise ApiError(status_code=422, title="Validation Error", detail="Each polygon point must be [x_mm, y_mm].")
            validate_coordinate("point x", p[0])
            validate_coordinate("point y", p[1])
        poly = [(float(p[0]), float(p[1])) for p in points]
        if abs(polygon_area(poly)) < MIN_ZONE_AREA_MM2:
            raise ApiError(status_code=422, title="Validation Error", detail="The polygon area is too small or degenerate.")
        if polygon_self_intersects(poly):
            raise ApiError(status_code=422, title="Validation Error", detail="The polygon must not intersect itself.")
        return
    raise ApiError(status_code=422, title="Validation Error", detail="geometry_type must be rect or polygon.")


async def active_floor_plan(db: AsyncSession, room_id: uuid.UUID) -> FloorPlan | None:
    return (await db.execute(select(FloorPlan).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))).scalar_one_or_none()


async def check_inside_room(db: AsyncSession, room_id: uuid.UUID, polygon: list[Point], label: str) -> str:
    """'within_boundary' when an approved room boundary exists and contains the shape, else a 422. Returns
    'unchecked_no_boundary' when the room has no approved boundary, so the response can say the check did not run."""
    plan = await active_floor_plan(db, room_id)
    boundary = await load_boundary(db, plan.id) if plan is not None else None
    if boundary is None:
        return "unchecked_no_boundary"
    if not polygon_contains_polygon(boundary, polygon, tolerance=BOUNDARY_TOLERANCE_MM):
        raise ApiError(status_code=422, title="Outside Room Boundary", detail=f"{label} lies outside the approved room boundary.")
    return "within_boundary"


async def check_point_inside_room(db: AsyncSession, room_id: uuid.UUID, point: Point, label: str) -> str:
    plan = await active_floor_plan(db, room_id)
    boundary = await load_boundary(db, plan.id) if plan is not None else None
    if boundary is None:
        return "unchecked_no_boundary"
    if not point_in_polygon(point, boundary, BOUNDARY_TOLERANCE_MM):
        raise ApiError(status_code=422, title="Outside Room Boundary", detail=f"{label} lies outside the approved room boundary.")
    return "within_boundary"
