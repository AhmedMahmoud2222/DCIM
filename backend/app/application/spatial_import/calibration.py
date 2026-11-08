"""Calibration: source coordinates -> canonical room-local integer millimetres (X right, Y down, origin at the
room corner, rotation clockwise degrees).

    u = (x - origin_x) * mm_per_unit
    v = (y - origin_y) * mm_per_unit, negated when the source Y axis points up
    (u, v) is then rotated clockwise by `rotation_quadrants` * 90 degrees about the origin

The parameters are stored once (floor_plan_calibration, immutable) and applied on demand; they are never
recomputed on read. Every method reports a conservative error bound so the operator sees how far a canonical
coordinate can be from the truth, and so tests can assert it."""

import math
from dataclasses import dataclass, field
from typing import Any

from app.application.spatial_import.sir import UNIT_TO_MM, SirDocument

METHODS = ("declared_units", "two_point", "room_dimension", "manual_scale")
MIN_MM_PER_UNIT = 1e-6
MAX_MM_PER_UNIT = 1e6
ROUNDING_BOUND_MM = 0.5  # canonical storage is integer mm
MAX_ANISOTROPY = 0.02
MIN_BASELINE_FRACTION = 0.05


class CalibrationError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CalibrationParams:
    method: str
    source_units: str
    mm_per_unit: float
    origin_x: float
    origin_y: float
    y_axis: str
    rotation_quadrants: int
    error_bound_mm: float | None
    relative_error: float | None
    confidence: str
    reference: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_canonical(self, x: float, y: float) -> tuple[float, float]:
        u = (x - self.origin_x) * self.mm_per_unit
        v = (y - self.origin_y) * self.mm_per_unit
        if self.y_axis == "up":
            v = -v
        for _ in range(self.rotation_quadrants % 4):
            u, v = -v, u
        return u, v

    def length_to_mm(self, length: float) -> float:
        return length * self.mm_per_unit

    def rect_to_canonical(self, cx: float, cy: float, width: float, height: float, rotation_deg: float) -> dict[str, float]:
        """Centre-based source rectangle -> canonical top-left (unrotated) + size + clockwise rotation."""
        mx, my = self.to_canonical(cx, cy)
        w_mm, h_mm = self.length_to_mm(width), self.length_to_mm(height)
        rot = rotation_deg if self.y_axis == "down" else -rotation_deg
        rot = (rot + 90 * (self.rotation_quadrants % 4)) % 360
        return {"x_mm": mx - w_mm / 2, "y_mm": my - h_mm / 2, "width_mm": w_mm, "height_mm": h_mm, "rotation_deg": rot}


def default_origin(document: SirDocument) -> tuple[float, float]:
    """Top-left of the drawing extent in canonical orientation (min X; max Y for a Y-up source)."""
    box = document.bbox()
    if box is None:
        return 0.0, 0.0
    return box["min_x"], (box["max_y"] if document.y_axis == "up" else box["min_y"])


def extent_src(document: SirDocument) -> float:
    box = document.bbox()
    if box is None:
        return 0.0
    return max(box["max_x"] - box["min_x"], box["max_y"] - box["min_y"])


def _confidence(relative_error: float | None, *, trusted: bool = True) -> str:
    if relative_error is None or not trusted:
        return "low"
    if relative_error <= 0.005:
        return "high"
    if relative_error <= 0.02:
        return "medium"
    return "low"


def _finish(
    *, method: str, units: str, scale: float, origin: tuple[float, float], y_axis: str, quadrants: int,
    relative_error: float | None, extent_mm: float, reference: dict[str, Any], warnings: list[str], trusted: bool = True,
) -> CalibrationParams:
    if not (MIN_MM_PER_UNIT <= scale <= MAX_MM_PER_UNIT) or not math.isfinite(scale):
        raise CalibrationError("scale_out_of_range", f"scale {scale!r} mm per unit is outside the permitted range")
    if quadrants not in (0, 1, 2, 3):
        raise CalibrationError("bad_rotation", "rotation must be 0, 90, 180 or 270 degrees")
    bound = None if relative_error is None else relative_error * extent_mm + ROUNDING_BOUND_MM
    return CalibrationParams(
        method=method, source_units=units, mm_per_unit=scale, origin_x=origin[0], origin_y=origin[1], y_axis=y_axis,
        rotation_quadrants=quadrants, error_bound_mm=None if bound is None else round(bound, 3),
        relative_error=None if relative_error is None else round(relative_error, 6),
        confidence=_confidence(relative_error, trusted=trusted), reference=reference, warnings=tuple(warnings),
    )


def calibrate_declared_units(
    document: SirDocument, *, origin: tuple[float, float] | None = None, quadrants: int = 0
) -> CalibrationParams:
    """Uses the unit the file itself declares. Only valid when the SIR says the unit is declared."""
    if document.source_units not in UNIT_TO_MM:
        raise CalibrationError("no_declared_units", f"The file declares no usable length unit ({document.source_units}).")
    scale = UNIT_TO_MM[document.source_units]
    warnings = [] if document.units_trusted else ["The file's units are not authoritative; verify with a two-point check."]
    return _finish(
        method="declared_units", units=document.source_units, scale=scale, origin=origin or default_origin(document),
        y_axis=document.y_axis, quadrants=quadrants, relative_error=0.0, extent_mm=extent_src(document) * scale,
        reference={"declared_units": document.source_units}, warnings=warnings, trusted=document.units_trusted,
    )


def calibrate_two_point(
    document: SirDocument, *, p1: tuple[float, float], p2: tuple[float, float], distance_mm: float,
    tolerance_mm: float = 1.0, pick_tolerance_src: float = 0.0, origin: tuple[float, float] | None = None, quadrants: int = 0,
) -> CalibrationParams:
    baseline = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if baseline <= 0 or not math.isfinite(baseline):
        raise CalibrationError("degenerate_baseline", "The two calibration points must differ.")
    if not (distance_mm > 0) or not math.isfinite(distance_mm):
        raise CalibrationError("bad_distance", "The real-world distance must be positive.")
    if tolerance_mm < 0 or pick_tolerance_src < 0:
        raise CalibrationError("bad_tolerance", "Tolerances cannot be negative.")
    scale = distance_mm / baseline
    relative_error = tolerance_mm / distance_mm + 2.0 * pick_tolerance_src / baseline
    extent = extent_src(document)
    warnings: list[str] = []
    if extent > 0 and baseline < MIN_BASELINE_FRACTION * extent:
        warnings.append("The calibration baseline is under 5% of the drawing extent; error is amplified across the plan.")
        relative_error = max(relative_error, 0.021)  # forces low confidence without hiding the computed figure
    return _finish(
        method="two_point", units=document.source_units, scale=scale, origin=origin or default_origin(document),
        y_axis=document.y_axis, quadrants=quadrants, relative_error=relative_error, extent_mm=extent * scale,
        reference={
            "p1": list(p1), "p2": list(p2), "distance_mm": distance_mm, "tolerance_mm": tolerance_mm,
            "pick_tolerance_src": pick_tolerance_src, "baseline_src": baseline,
        },
        warnings=warnings,
    )


def calibrate_room_dimension(
    document: SirDocument, *, src_width: float, real_width_mm: float, src_height: float | None = None,
    real_height_mm: float | None = None, tolerance_mm: float = 1.0, origin: tuple[float, float] | None = None, quadrants: int = 0,
) -> CalibrationParams:
    if src_width <= 0 or real_width_mm <= 0:
        raise CalibrationError("bad_dimension", "Room dimensions must be positive.")
    sx = real_width_mm / src_width
    anisotropy = 0.0
    scale = sx
    reference: dict[str, Any] = {
        "src_width": src_width, "real_width_mm": real_width_mm, "tolerance_mm": tolerance_mm,
    }
    if src_height is not None or real_height_mm is not None:
        if not src_height or not real_height_mm or src_height <= 0 or real_height_mm <= 0:
            raise CalibrationError("bad_dimension", "Provide both the drawn and the real room height, or neither.")
        sy = real_height_mm / src_height
        anisotropy = abs(sx - sy) / max(sx, sy)
        if anisotropy > MAX_ANISOTROPY:
            raise CalibrationError(
                "anisotropic_scale",
                f"The width and height scales differ by {anisotropy:.1%}; the drawing is distorted or a dimension is wrong.",
            )
        scale = (sx + sy) / 2
        reference.update({"src_height": src_height, "real_height_mm": real_height_mm})
    relative_error = anisotropy / 2 + tolerance_mm / real_width_mm
    return _finish(
        method="room_dimension", units=document.source_units, scale=scale, origin=origin or default_origin(document),
        y_axis=document.y_axis, quadrants=quadrants, relative_error=relative_error, extent_mm=extent_src(document) * scale,
        reference=reference, warnings=[],
    )


def calibrate_manual_scale(
    document: SirDocument, *, mm_per_unit: float, origin: tuple[float, float] | None = None, quadrants: int = 0
) -> CalibrationParams:
    return _finish(
        method="manual_scale", units=document.source_units, scale=mm_per_unit, origin=origin or default_origin(document),
        y_axis=document.y_axis, quadrants=quadrants, relative_error=None, extent_mm=extent_src(document) * mm_per_unit,
        reference={"mm_per_unit": mm_per_unit}, warnings=["A manually entered scale has no verifiable error bound."],
    )
