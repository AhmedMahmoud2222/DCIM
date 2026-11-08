"""Small planar-geometry helpers (canonical or source space; unit-agnostic). Footprints are oriented
rectangles: (x, y) is the top-left corner of the *unrotated* rectangle and rotation is clockwise degrees about
its centre in a y-down frame, the convention SpatialObject/RackPlacement already use."""

import math

Point = tuple[float, float]


def rect_corners(x: float, y: float, width: float, height: float, rotation_deg: float = 0.0) -> list[Point]:
    cx, cy = x + width / 2, y + height / 2
    rad = math.radians(rotation_deg)
    cos, sin = math.cos(rad), math.sin(rad)
    out: list[Point] = []
    for dx, dy in ((-width / 2, -height / 2), (width / 2, -height / 2), (width / 2, height / 2), (-width / 2, height / 2)):
        out.append((cx + dx * cos - dy * sin, cy + dx * sin + dy * cos))
    return out


def polygon_area(points: list[Point]) -> float:
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _ensure_ccw(points: list[Point]) -> list[Point]:
    return points if polygon_area(points) >= 0 else list(reversed(points))


def clip_polygon(subject: list[Point], clip: list[Point]) -> list[Point]:
    """Sutherland-Hodgman; `clip` must be convex."""
    output = _ensure_ccw(subject)
    clip = _ensure_ccw(clip)
    for i, (ax, ay) in enumerate(clip):
        bx, by = clip[(i + 1) % len(clip)]
        input_list, output = output, []
        if not input_list:
            break
        prev = input_list[-1]
        for cur in input_list:
            prev_in = (bx - ax) * (prev[1] - ay) - (by - ay) * (prev[0] - ax) >= 0
            cur_in = (bx - ax) * (cur[1] - ay) - (by - ay) * (cur[0] - ax) >= 0
            if cur_in != prev_in:
                dx1, dy1 = cur[0] - prev[0], cur[1] - prev[1]
                dx2, dy2 = bx - ax, by - ay
                denom = dx1 * dy2 - dy1 * dx2
                if abs(denom) > 1e-12:
                    t = ((ax - prev[0]) * dy2 - (ay - prev[1]) * dx2) / denom
                    output.append((prev[0] + t * dx1, prev[1] + t * dy1))
            if cur_in:
                output.append(cur)
            prev = cur
    return output


def rect_iou(a: list[Point], b: list[Point]) -> float:
    inter = abs(polygon_area(clip_polygon(a, b)))
    union = abs(polygon_area(a)) + abs(polygon_area(b)) - inter
    return 0.0 if union <= 0 else inter / union


def point_in_polygon(point: Point, polygon: list[Point], tolerance: float = 0.0) -> bool:
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        # on-edge (within tolerance) counts as inside
        seg_len = math.hypot(x2 - x1, y2 - y1)
        if seg_len > 0:
            t = max(0.0, min(1.0, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / (seg_len * seg_len)))
            if math.hypot(x - (x1 + t * (x2 - x1)), y - (y1 + t * (y2 - y1))) <= tolerance:
                return True
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def polygon_contains_polygon(outer: list[Point], inner: list[Point], tolerance: float = 0.0) -> bool:
    """True when every vertex of `inner` lies inside `outer` (exact for convex outers; a documented
    approximation for concave ones, where a concave notch could cut across an edge)."""
    return all(point_in_polygon(p, outer, tolerance) for p in inner)


def center_distance(a: list[Point], b: list[Point]) -> float:
    ax = sum(p[0] for p in a) / len(a)
    ay = sum(p[1] for p in a) / len(a)
    bx = sum(p[0] for p in b) / len(b)
    by = sum(p[1] for p in b) / len(b)
    return math.hypot(ax - bx, ay - by)
