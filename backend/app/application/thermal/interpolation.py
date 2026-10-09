# ruff: noqa: E501
"""Deterministic inverse-distance-weighted (IDW) interpolation for operational heat maps (Issue #105).

This is a visualisation aid, not a physical simulation. A generated cell value is a weighted average of nearby
*fresh measured* sensor values: it can never lie outside the range of the sensors that contributed to it, and it
is never written back to telemetry or inventory.

Method (Shepard IDW, power 2):
    value(c) = sum(v_i / d_i^2) / sum(1 / d_i^2)   over fresh sensors i with d_i <= radius
A cell is produced only when at least `MIN_NEIGHBOURS_PER_CELL` sensors lie within the influence radius; otherwise
the cell is left empty ("no coverage") instead of being filled by extrapolating from one sensor.

Assumptions and known limits (also documented in docs/implementation/ISSUE_105_COOLING_THERMAL.md):
  * 2D plane. Sensor mounting height, stratification and 3D air movement are not modelled.
  * No barrier awareness. Walls, containment and rack bodies do not block influence; the map must not be read as
    showing that air or heat crosses or does not cross them.
  * Euclidean distance in canonical room-local millimetres, inside one room only.
  * Stale, missing and untimestamped sensors contribute nothing (they are excluded, not down-weighted).
  * Deterministic: sensors are sorted by id, cells are scanned row-major, arithmetic order is fixed, and values
    are rounded to 2 decimals, so identical inputs give byte-identical output.

All resource limits are hard caps applied here, regardless of what the API caller requested.
"""

import math
from dataclasses import dataclass

IDW_POWER = 2.0
MIN_SENSORS_FOR_FIELD = 3
MIN_NEIGHBOURS_PER_CELL = 2
MAX_SENSORS_PER_MAP = 128
MAX_GRID_DIM = 80  # cells along the longer axis => at most 80 x 80 = 6400 cells
MIN_CELL_MM = 100
DEFAULT_CELL_MM = 500
MIN_RADIUS_MM = 500
DEFAULT_RADIUS_MM = 6000
MAX_RADIUS_MM = 20000
ROUND_DECIMALS = 2

Point = tuple[float, float]


@dataclass(frozen=True)
class FieldSample:
    sensor_id: str
    x_mm: float
    y_mm: float
    value: float


@dataclass(frozen=True)
class GridField:
    origin_x_mm: int
    origin_y_mm: int
    cell_mm: int
    columns: int
    rows: int
    values: list[float | None]  # row-major, None = no coverage or outside the room
    support: list[int]  # row-major, contributing sensors per cell (0 outside / no coverage)
    inside: list[bool]  # row-major, cell centre lies inside the room
    min_value: float | None
    max_value: float | None
    covered_cells: int
    room_cells: int

    @property
    def coverage_percent(self) -> float:
        return round(100.0 * self.covered_cells / self.room_cells, 1) if self.room_cells else 0.0


def clamp_radius(radius_mm: int | None) -> int:
    return max(MIN_RADIUS_MM, min(MAX_RADIUS_MM, radius_mm if radius_mm is not None else DEFAULT_RADIUS_MM))


def resolve_cell_size(extent_x_mm: float, extent_y_mm: float, requested_cell_mm: int | None) -> int:
    """The requested cell size, raised as far as needed so the grid never exceeds MAX_GRID_DIM per axis."""
    requested = max(MIN_CELL_MM, requested_cell_mm if requested_cell_mm is not None else DEFAULT_CELL_MM)
    needed = math.ceil(max(extent_x_mm, extent_y_mm, 1.0) / MAX_GRID_DIM)
    return int(max(requested, needed))


def point_in_polygon(x: float, y: float, polygon: list[Point]) -> bool:
    """Even-odd rule. Points exactly on an edge are treated as inside by the caller's tolerance, not here."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def distance_to_polygon_mm(x: float, y: float, polygon: list[Point]) -> float:
    """0 when inside, else the distance to the nearest edge."""
    if point_in_polygon(x, y, polygon):
        return 0.0
    best = math.inf
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def idw_field(
    samples: list[FieldSample], polygon: list[Point], *, cell_mm: int | None = None, radius_mm: int | None = None
) -> GridField | None:
    """Interpolate `samples` over the room `polygon`. Returns None when there are fewer than MIN_SENSORS_FOR_FIELD
    samples, so a caller can never mistake a one- or two-point "field" for a map."""
    if len(samples) < MIN_SENSORS_FOR_FIELD or len(polygon) < 3:
        return None
    ordered = sorted(samples, key=lambda s: s.sensor_id)[:MAX_SENSORS_PER_MAP]
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    size = resolve_cell_size(max_x - min_x, max_y - min_y, cell_mm)
    radius = clamp_radius(radius_mm)
    radius_sq = float(radius) * float(radius)
    columns = max(1, math.ceil((max_x - min_x) / size))
    rows = max(1, math.ceil((max_y - min_y) / size))
    values: list[float | None] = []
    support: list[int] = []
    inside: list[bool] = []
    lo: float | None = None
    hi: float | None = None
    covered = 0
    room_cells = 0
    for row in range(rows):
        cy = min_y + (row + 0.5) * size
        for col in range(columns):
            cx = min_x + (col + 0.5) * size
            if not point_in_polygon(cx, cy, polygon):
                values.append(None)
                support.append(0)
                inside.append(False)
                continue
            inside.append(True)
            room_cells += 1
            num = 0.0
            den = 0.0
            used = 0
            exact: float | None = None
            for s in ordered:
                dx = s.x_mm - cx
                dy = s.y_mm - cy
                d_sq = dx * dx + dy * dy
                if d_sq > radius_sq:
                    continue
                used += 1
                if d_sq < 1e-6:
                    exact = s.value
                    break
                w = 1.0 / (d_sq ** (IDW_POWER / 2.0))
                num += w * s.value
                den += w
            if exact is not None:
                value: float | None = round(exact, ROUND_DECIMALS)
            elif used >= MIN_NEIGHBOURS_PER_CELL and den > 0:
                value = round(num / den, ROUND_DECIMALS)
            else:
                value = None
            values.append(value)
            support.append(used if value is not None else 0)
            if value is not None:
                covered += 1
                lo = value if lo is None else min(lo, value)
                hi = value if hi is None else max(hi, value)
    return GridField(
        origin_x_mm=int(math.floor(min_x)), origin_y_mm=int(math.floor(min_y)), cell_mm=size, columns=columns, rows=rows,
        values=values, support=support, inside=inside, min_value=lo, max_value=hi, covered_cells=covered, room_cells=room_cells,
    )
