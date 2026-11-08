"""Calibration math, canonical conversion and error bounds (Issue #104)."""

import math
import random

import pytest

from app.application.spatial_import import calibration as cal
from app.application.spatial_import.dxf_parser import parse_dxf
from app.application.spatial_import.sir import SirDocument, SirEntity
from tests import spatial_fixtures as fx


def _doc(units="mm", trusted=True, y_axis="up", extent=1000.0):
    return SirDocument(
        source_format="dxf", parser_name="t", parser_version="1", source_units=units, units_trusted=trusted, y_axis=y_axis,
        entities=[SirEntity(kind="rect", ref="a", cx=extent / 2, cy=extent / 2, width=extent, height=extent)],
    )


def test_declared_units_map_a_y_up_drawing_to_the_room_corner_origin():
    doc = parse_dxf(fx.rack_row_dxf(2))  # room 0..6000 x 0..4000, Y up
    params = cal.calibrate_declared_units(doc)
    assert params.mm_per_unit == 1.0 and params.confidence == "high" and params.relative_error == 0.0
    assert params.to_canonical(0, 4000) == (0, 0)  # top-left of the drawing is the room origin
    assert params.to_canonical(1000, 1000) == (1000, 3000)  # Y flips: 3000 mm down from the top edge
    assert params.error_bound_mm == pytest.approx(cal.ROUNDING_BOUND_MM)


@pytest.mark.parametrize(("units", "factor"), [("mm", 1), ("cm", 10), ("m", 1000), ("in", 25.4), ("ft", 304.8)])
def test_unit_factors(units, factor):
    assert cal.calibrate_declared_units(_doc(units=units)).mm_per_unit == factor


def test_declared_units_refuse_unitless_and_pixel_drawings():
    for units in ("unitless", "px"):
        with pytest.raises(cal.CalibrationError) as exc:
            cal.calibrate_declared_units(_doc(units=units, trusted=False))
        assert exc.value.code == "no_declared_units"


def test_untrusted_declared_units_are_low_confidence_with_a_warning():
    params = cal.calibrate_declared_units(_doc(units="m", trusted=False))
    assert params.confidence == "low" and params.warnings


def test_two_point_scale_and_error_bound():
    doc = _doc(units="px", trusted=False, y_axis="down", extent=1000)
    params = cal.calibrate_two_point(doc, p1=(0, 0), p2=(600, 800), distance_mm=5000, tolerance_mm=2.0, pick_tolerance_src=1.0)
    baseline = 1000.0
    assert params.mm_per_unit == pytest.approx(5.0)
    expected_rel = 2.0 / 5000 + 2 * 1.0 / baseline
    assert params.relative_error == pytest.approx(expected_rel, rel=1e-4)
    assert params.error_bound_mm == pytest.approx(expected_rel * 1000 * 5.0 + 0.5, abs=0.01)
    assert params.confidence == "high"  # 0.24% relative error is under the 0.5% band
    assert params.reference["distance_mm"] == 5000


def test_two_point_confidence_bands():
    doc = _doc(units="px", trusted=False, y_axis="down", extent=1000)
    high = cal.calibrate_two_point(doc, p1=(0, 0), p2=(1000, 0), distance_mm=10000, tolerance_mm=1.0)
    low = cal.calibrate_two_point(doc, p1=(0, 0), p2=(1000, 0), distance_mm=10000, tolerance_mm=500.0)
    assert high.confidence == "high" and low.confidence == "low"


def test_short_baseline_is_flagged_and_forced_to_low_confidence():
    doc = _doc(units="px", trusted=False, y_axis="down", extent=10_000)
    params = cal.calibrate_two_point(doc, p1=(0, 0), p2=(100, 0), distance_mm=1000, tolerance_mm=0)
    assert params.confidence == "low" and any("5%" in w for w in params.warnings)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"p1": (1, 1), "p2": (1, 1), "distance_mm": 10}, "degenerate_baseline"),
        ({"p1": (0, 0), "p2": (1, 0), "distance_mm": 0}, "bad_distance"),
        ({"p1": (0, 0), "p2": (1, 0), "distance_mm": -5}, "bad_distance"),
        ({"p1": (0, 0), "p2": (1, 0), "distance_mm": 10, "tolerance_mm": -1}, "bad_tolerance"),
        ({"p1": (0, 0), "p2": (1e-12, 0), "distance_mm": 1e9}, "scale_out_of_range"),
    ],
)
def test_two_point_rejects_bad_input(kwargs, code):
    with pytest.raises(cal.CalibrationError) as exc:
        cal.calibrate_two_point(_doc(units="px", trusted=False), **kwargs)
    assert exc.value.code == code


def test_room_dimension_calibration_and_anisotropy_rejection():
    doc = _doc(units="px", trusted=False, y_axis="down", extent=800)
    ok = cal.calibrate_room_dimension(doc, src_width=800, real_width_mm=8000, src_height=400, real_height_mm=4040, tolerance_mm=1)
    assert ok.mm_per_unit == pytest.approx(10.05, rel=1e-3) and ok.confidence in {"high", "medium"}
    assert ok.relative_error == pytest.approx(abs(10 - 10.1) / 10.1 / 2 + 1 / 8000, rel=0.05)
    with pytest.raises(cal.CalibrationError) as exc:
        cal.calibrate_room_dimension(doc, src_width=800, real_width_mm=8000, src_height=400, real_height_mm=4400)
    assert exc.value.code == "anisotropic_scale"
    with pytest.raises(cal.CalibrationError):
        cal.calibrate_room_dimension(doc, src_width=800, real_width_mm=8000, src_height=400)  # half a pair


def test_manual_scale_has_no_verifiable_bound():
    params = cal.calibrate_manual_scale(_doc(units="px", trusted=False), mm_per_unit=2.5)
    assert params.error_bound_mm is None and params.relative_error is None and params.confidence == "low" and params.warnings


@pytest.mark.parametrize("scale", [0, -1, 1e-9, 1e9, math.inf, math.nan])
def test_scale_must_be_within_range(scale):
    with pytest.raises(cal.CalibrationError):
        cal.calibrate_manual_scale(_doc(units="px", trusted=False), mm_per_unit=scale)


def test_rotation_quadrants_rotate_clockwise_about_the_origin():
    doc = _doc(y_axis="down")
    base = dict(origin=(0, 0))
    q0 = cal.calibrate_declared_units(doc, quadrants=0, **base).to_canonical(100, 0)
    q1 = cal.calibrate_declared_units(doc, quadrants=1, **base).to_canonical(100, 0)
    q2 = cal.calibrate_declared_units(doc, quadrants=2, **base).to_canonical(100, 0)
    q3 = cal.calibrate_declared_units(doc, quadrants=3, **base).to_canonical(100, 0)
    assert q0 == (100, 0) and q1 == (0, 100) and q2 == (-100, 0) and q3 == (0, -100)
    with pytest.raises(cal.CalibrationError):
        cal.calibrate_declared_units(doc, quadrants=4)


def test_rect_conversion_flips_rotation_for_y_up_sources_and_adds_quadrants():
    up = cal.calibrate_declared_units(_doc(y_axis="up"), origin=(0, 1000))
    r = up.rect_to_canonical(cx=300, cy=500, width=600, height=1000, rotation_deg=30)
    assert (r["x_mm"], r["y_mm"], r["width_mm"], r["height_mm"]) == (0, 0, 600, 1000)
    assert r["rotation_deg"] == pytest.approx(330)
    down = cal.calibrate_declared_units(_doc(y_axis="down"), origin=(0, 0), quadrants=1)
    assert down.rect_to_canonical(cx=300, cy=500, width=600, height=1000, rotation_deg=30)["rotation_deg"] == pytest.approx(120)


def test_conversion_is_deterministic_and_does_not_depend_on_the_drawing():
    """No silent recomputation: the same stored parameters always give the same millimetres."""
    params = cal.calibrate_two_point(_doc(units="px", trusted=False, y_axis="down"), p1=(10, 10), p2=(110, 10), distance_mm=2500, origin=(5, 5))
    again = cal.CalibrationParams(**{**params.__dict__})
    assert params.to_canonical(55, 105) == again.to_canonical(55, 105) == (pytest.approx(1250), pytest.approx(2500))


def test_stated_error_bound_covers_the_actual_error_across_random_measurements():
    """Monte-Carlo check of the bound: perturb the picked points and the measured distance within their stated
    tolerances and confirm the mapped position of a far-away point stays inside `error_bound_mm`."""
    rng = random.Random(1042)
    doc = _doc(units="px", trusted=False, y_axis="down", extent=2000)
    true_scale = 4.0
    for _ in range(300):
        baseline = rng.uniform(600, 1800)
        pick_tol = rng.uniform(0, 3)
        tol_mm = rng.uniform(0, 8)
        p1 = (rng.uniform(0, 100), rng.uniform(0, 100))
        angle = rng.uniform(0, math.tau)
        p2_true = (p1[0] + baseline * math.cos(angle), p1[1] + baseline * math.sin(angle))
        # the operator's actual clicks land within `pick_tol` of the true points, along the baseline
        e1, e2 = rng.uniform(-pick_tol, pick_tol), rng.uniform(-pick_tol, pick_tol)
        c1 = (p1[0] + e1 * math.cos(angle), p1[1] + e1 * math.sin(angle))
        c2 = (p2_true[0] + e2 * math.cos(angle), p2_true[1] + e2 * math.sin(angle))
        measured = baseline * true_scale + rng.uniform(-tol_mm, tol_mm)
        params = cal.calibrate_two_point(doc, p1=c1, p2=c2, distance_mm=measured, tolerance_mm=tol_mm, pick_tolerance_src=pick_tol, origin=(0, 0))
        far = (2000.0, 2000.0)
        mapped = params.to_canonical(*far)
        true = (far[0] * true_scale, far[1] * true_scale)
        err = max(abs(mapped[0] - true[0]), abs(mapped[1] - true[1]))
        assert params.error_bound_mm is not None and err <= params.error_bound_mm, (err, params.error_bound_mm)
