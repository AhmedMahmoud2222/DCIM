"""Reusable spatial validation (Phase 2 prompt §25). Two distinct categories, never
confused (§25's explicit instruction): a rule the database enforces absolutely
(U-space overlap, one-current-placement — see migration 0004's exclusion constraints,
app/domain/placement/models.py) is never re-implemented here as a "check" that could
silently diverge from the real constraint; this module covers everything else —
pre-DB API-level rejection of malformed input (so a client gets a clean 422 instead of a
raw constraint-violation 500/409) and advisory geometric warnings (overlap-in-room,
out-of-bounds) that the database has no constraint for at all, because rack/equipment
footprints are rectangles the DB does not model geometrically (RackPlacement/
EquipmentPlacement store one x/y point + rotation, not a polygon)."""

from dataclasses import dataclass

from app.core.errors import ApiError

MAX_COORDINATE_MM = 1_000_000  # 1 km — generously bounds a room; rejects garbage/overflow input
MIN_COORDINATE_MM = -MAX_COORDINATE_MM


@dataclass(frozen=True)
class ValidationWarning:
    code: str
    detail: str


def validate_rotation_degrees(rotation_deg: int | None) -> None:
    if rotation_deg is None:
        return
    if not (0 <= rotation_deg < 360):
        raise ApiError(
            status_code=422,
            title="Validation Error",
            detail=f"rotation_deg must be in [0, 360), got {rotation_deg}.",
        )


def validate_coordinate(name: str, value: int | None) -> None:
    if value is None:
        return
    if not (MIN_COORDINATE_MM <= value <= MAX_COORDINATE_MM):
        raise ApiError(
            status_code=422,
            title="Validation Error",
            detail=f"{name} must be within [{MIN_COORDINATE_MM}, {MAX_COORDINATE_MM}] mm, got {value}.",
        )


def validate_u_range_against_rack_capacity(*, u_start: int, u_end: int, rack_height_u: int) -> None:
    """Hard rejection at the API boundary — computable purely from the rack's own
    catalog spec (RackModelRevision.height_u), so there is no reason to let an
    impossible U range reach the database at all. Distinct from the front/rear overlap
    invariant, which genuinely needs the database's exclusion constraints (it depends on
    concurrent state, not just this one request's input)."""
    if u_start < 1:
        raise ApiError(status_code=422, title="Validation Error", detail=f"u_start must be >= 1, got {u_start}.")
    if u_end <= u_start:
        raise ApiError(
            status_code=422, title="Validation Error", detail=f"u_end ({u_end}) must be greater than u_start ({u_start})."
        )
    if u_end - u_start > 100:
        # No real rack is 100U+; catches accidental overflow-scale input before it even
        # reaches the rack-capacity check below.
        raise ApiError(status_code=422, title="Validation Error", detail="U-range height is implausibly large.")
    if u_end - 1 > rack_height_u:
        raise ApiError(
            status_code=422,
            title="Validation Error",
            detail=f"U-range [{u_start}, {u_end}) exceeds the rack's capacity of {rack_height_u}U.",
        )


def check_rack_footprint_within_room(
    *, x_mm: int | None, y_mm: int | None, width_mm: int, depth_mm: int, room_width_mm: int | None, room_height_mm: int | None
) -> list[ValidationWarning]:
    """Advisory only (§25: "where a rule is advisory, expose it as validation/diagnostic
    output") — the database has no geometric constraint tying RackPlacement.x_mm/y_mm to
    Room dimensions (Room itself doesn't require a floor plan to exist), so a rack
    without full spatial placement yet (x_mm/y_mm NULL — a valid, expected state per §8)
    or a room without recorded dimensions yet produces no warning, not an error."""
    warnings: list[ValidationWarning] = []
    if x_mm is None or y_mm is None or room_width_mm is None or room_height_mm is None:
        return warnings
    if x_mm < 0 or y_mm < 0 or x_mm + width_mm > room_width_mm or y_mm + depth_mm > room_height_mm:
        warnings.append(
            ValidationWarning(
                code="rack_outside_room_bounds",
                detail=f"Rack footprint at ({x_mm}, {y_mm}) size {width_mm}x{depth_mm}mm extends "
                f"outside the room's recorded {room_width_mm}x{room_height_mm}mm bounds.",
            )
        )
    return warnings


def check_rack_overlap_in_room(
    *, candidate: tuple[int, int, int, int], others: list[tuple[int, int, int, int]]
) -> list[ValidationWarning]:
    """Advisory axis-aligned-rectangle overlap check across racks placed in the same
    room. `candidate`/`others` are (x_mm, y_mm, width_mm, depth_mm) tuples of racks that
    already have a drawn (x_mm/y_mm not NULL) position — the caller filters those first.
    Rotation is intentionally not modeled here (would require full polygon intersection
    for a 90/180/270-rotated footprint); flagged as a documented limitation, not silently
    assumed correct — see PHASE2_IMPLEMENTATION_REPORT.md Known Issues."""
    warnings: list[ValidationWarning] = []
    cx, cy, cw, cd = candidate
    for ox, oy, ow, od in others:
        overlaps = cx < ox + ow and ox < cx + cw and cy < oy + od and oy < cy + cd
        if overlaps:
            warnings.append(
                ValidationWarning(
                    code="rack_overlaps_another_rack",
                    detail=f"Footprint overlaps another rack at ({ox}, {oy}) size {ow}x{od}mm.",
                )
            )
    return warnings
