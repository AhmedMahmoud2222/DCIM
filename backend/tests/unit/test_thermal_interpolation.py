"""Issue #105: deterministic, bounded IDW interpolation (pure functions, no database)."""

import time

from app.application.thermal import interpolation as idw

ROOM = [(0.0, 0.0), (6000.0, 0.0), (6000.0, 4000.0), (0.0, 4000.0)]


def samples():
    return [idw.FieldSample("a", 1000, 1000, 20.0), idw.FieldSample("b", 5000, 1000, 30.0), idw.FieldSample("c", 3000, 3500, 25.0)]


def test_fewer_than_three_sensors_never_produce_a_field():
    assert idw.idw_field(samples()[:2], ROOM) is None
    assert idw.idw_field([], ROOM) is None
    assert idw.idw_field(samples(), ROOM[:2]) is None  # not a polygon


def test_output_is_deterministic_and_independent_of_input_order():
    first = idw.idw_field(samples(), ROOM)
    second = idw.idw_field(list(reversed(samples())), ROOM)
    assert first == second
    assert idw.idw_field(samples(), ROOM) == first


def test_interpolated_values_stay_inside_the_range_of_the_contributing_sensors():
    field = idw.idw_field(samples(), ROOM)
    assert field is not None and field.min_value is not None and field.max_value is not None
    assert 20.0 <= field.min_value <= field.max_value <= 30.0
    assert all(v is None or 20.0 <= v <= 30.0 for v in field.values)


def test_a_cell_centred_on_a_sensor_takes_that_sensor_value_exactly():
    one = [idw.FieldSample("a", 250, 250, 21.5), idw.FieldSample("b", 5000, 3000, 30.0), idw.FieldSample("c", 2500, 2500, 25.0)]
    field = idw.idw_field(one, ROOM, cell_mm=500)
    assert field is not None
    assert field.values[0] == 21.5  # cell (0,0) centre is (250, 250)


def test_cells_with_fewer_than_two_sensors_in_range_are_left_empty_not_extrapolated():
    far = [idw.FieldSample("a", 500, 500, 20), idw.FieldSample("b", 700, 700, 22), idw.FieldSample("c", 900, 500, 21)]
    field = idw.idw_field(far, ROOM, radius_mm=1000, cell_mm=500)
    assert field is not None
    assert field.values[-1] is None  # far corner: nothing within 1 m
    assert field.coverage_percent < 100.0
    assert all(s == 0 for v, s in zip(field.values, field.support, strict=True) if v is None)


def test_influence_radius_is_clamped_to_its_bounds():
    assert idw.clamp_radius(1) == idw.MIN_RADIUS_MM
    assert idw.clamp_radius(10**9) == idw.MAX_RADIUS_MM
    assert idw.clamp_radius(None) == idw.DEFAULT_RADIUS_MM


def test_grid_resolution_is_bounded_whatever_cell_size_is_requested():
    huge_room = [(0.0, 0.0), (200000.0, 0.0), (200000.0, 150000.0), (0.0, 150000.0)]
    pts = [idw.FieldSample(f"s{i}", 1000 * i, 1000 * i, 20 + i) for i in range(5)]
    field = idw.idw_field(pts, huge_room, cell_mm=100, radius_mm=20000)
    assert field is not None
    assert field.columns <= idw.MAX_GRID_DIM and field.rows <= idw.MAX_GRID_DIM
    assert idw.resolve_cell_size(200000, 150000, 100) >= 2500


def test_cells_outside_a_non_rectangular_room_are_not_computed():
    l_shape = [(0.0, 0.0), (6000.0, 0.0), (6000.0, 2000.0), (2000.0, 2000.0), (2000.0, 4000.0), (0.0, 4000.0)]
    pts = [idw.FieldSample("a", 500, 500, 20), idw.FieldSample("b", 5500, 500, 24), idw.FieldSample("c", 500, 3500, 22)]
    field = idw.idw_field(pts, l_shape, cell_mm=500)
    assert field is not None
    assert field.room_cells < field.columns * field.rows
    assert all(v is None for v, inside in zip(field.values, field.inside, strict=True) if not inside)
    assert field.coverage_percent <= 100.0


def test_worst_case_input_completes_within_a_small_time_budget():
    pts = [idw.FieldSample(f"s{i:03d}", (i * 173) % 24000, (i * 389) % 18000, 20 + i % 10) for i in range(idw.MAX_SENSORS_PER_MAP + 50)]
    big_room = [(0.0, 0.0), (24000.0, 0.0), (24000.0, 18000.0), (0.0, 18000.0)]
    started = time.perf_counter()
    field = idw.idw_field(pts, big_room, cell_mm=100, radius_mm=20000)
    elapsed = time.perf_counter() - started
    assert field is not None and field.columns * field.rows <= idw.MAX_GRID_DIM**2
    assert elapsed < 5.0  # 128 sensors x 6400 cells; measured around 0.2 s


def test_distance_to_polygon_is_zero_inside_and_positive_outside():
    assert idw.distance_to_polygon_mm(100, 100, ROOM) == 0.0
    assert idw.distance_to_polygon_mm(-300, 100, ROOM) == 300.0
