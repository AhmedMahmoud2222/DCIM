import pytest

from app.application.spatial_validation import (
    check_rack_footprint_within_room,
    check_rack_overlap_in_room,
    validate_coordinate,
    validate_rotation_degrees,
    validate_u_range_against_rack_capacity,
)
from app.core.errors import ApiError


def test_validate_coordinate_accepts_none():
    validate_coordinate("x_mm", None)  # must not raise


def test_validate_coordinate_accepts_in_range_value():
    validate_coordinate("x_mm", 500_000)


@pytest.mark.parametrize("value", [1_000_001, -1_000_001, 10**9])
def test_validate_coordinate_rejects_out_of_range(value):
    with pytest.raises(ApiError) as exc:
        validate_coordinate("x_mm", value)
    assert exc.value.status_code == 422


def test_validate_rotation_accepts_none_and_boundaries():
    validate_rotation_degrees(None)
    validate_rotation_degrees(0)
    validate_rotation_degrees(359)


@pytest.mark.parametrize("value", [360, -1, 720])
def test_validate_rotation_rejects_out_of_range(value):
    with pytest.raises(ApiError):
        validate_rotation_degrees(value)


def test_u_range_rejects_u_start_below_one():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=0, u_end=2, rack_height_u=42)


def test_u_range_rejects_negative_u_start():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=-5, u_end=2, rack_height_u=42)


def test_u_range_rejects_u_end_not_greater_than_u_start():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=5, u_end=5, rack_height_u=42)
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=5, u_end=3, rack_height_u=42)


def test_u_range_rejects_implausibly_large_height():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=1, u_end=200, rack_height_u=1000)


def test_u_range_rejects_exceeding_rack_capacity():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=40, u_end=45, rack_height_u=42)


def test_u_range_accepts_exactly_filling_the_rack():
    validate_u_range_against_rack_capacity(u_start=1, u_end=43, rack_height_u=42)  # must not raise


def test_u_range_rejects_by_one_over_capacity():
    with pytest.raises(ApiError):
        validate_u_range_against_rack_capacity(u_start=1, u_end=44, rack_height_u=42)


def test_rack_footprint_check_produces_no_warning_when_coordinates_are_null():
    """A rack without a drawn position yet (x_mm/y_mm NULL) is a valid, expected state
    (§8) — the advisory check must not fabricate a warning for it."""
    warnings = check_rack_footprint_within_room(
        x_mm=None, y_mm=None, width_mm=600, depth_mm=1000, room_width_mm=5000, room_height_mm=5000
    )
    assert warnings == []


def test_rack_footprint_check_produces_no_warning_when_room_dimensions_are_null():
    warnings = check_rack_footprint_within_room(
        x_mm=0, y_mm=0, width_mm=600, depth_mm=1000, room_width_mm=None, room_height_mm=None
    )
    assert warnings == []


def test_rack_footprint_check_flags_out_of_bounds_footprint():
    warnings = check_rack_footprint_within_room(
        x_mm=4800, y_mm=0, width_mm=600, depth_mm=1000, room_width_mm=5000, room_height_mm=5000
    )
    assert len(warnings) == 1
    assert warnings[0].code == "rack_outside_room_bounds"


def test_rack_footprint_check_accepts_footprint_within_bounds():
    warnings = check_rack_footprint_within_room(
        x_mm=0, y_mm=0, width_mm=600, depth_mm=1000, room_width_mm=5000, room_height_mm=5000
    )
    assert warnings == []


def test_rack_overlap_check_flags_overlapping_footprints():
    warnings = check_rack_overlap_in_room(candidate=(0, 0, 600, 1000), others=[(300, 300, 600, 1000)])
    assert len(warnings) == 1
    assert warnings[0].code == "rack_overlaps_another_rack"


def test_rack_overlap_check_allows_adjacent_non_overlapping_footprints():
    warnings = check_rack_overlap_in_room(candidate=(0, 0, 600, 1000), others=[(600, 0, 600, 1000)])
    assert warnings == []
