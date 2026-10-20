"""Issue #102 area D: every forecast state, with its explicit reason."""

from datetime import UTC, datetime, timedelta

import pytest

from app.application.power_forecast import SnapshotPoint, compute_forecast

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def series(days, *, start_load=50.0, per_day=1.0, capacity=100.0, basis="measured", coverage=1.0, end_gap_hours=0):
    pts = []
    last_hour = NOW.replace(minute=0) - timedelta(hours=1 + end_gap_hours)
    for i in range(days * 24):
        ts = last_hour - timedelta(hours=days * 24 - 1 - i)
        load = start_load + per_day * (ts - (last_hour - timedelta(days=days))).total_seconds() / 86400.0
        pts.append(SnapshotPoint(ts, load, basis, capacity, coverage, 60))
    return pts


def test_good_growth_projects_exhaustion_with_inputs_named():
    f = compute_forecast(series(20), now=NOW)
    assert f.status == "good" and f.method == "linear_ols_v1" and f.metric == "power_kw" and f.unit == "kW"
    assert f.slope_kw_per_day == pytest.approx(1.0, abs=0.05)
    # about 30 kW of headroom at 1 kW/day
    assert 25 < f.days_to_exhaustion < 35
    assert f.exhaustion_date > NOW and f.confidence == "high"
    assert f.day_count >= 19 and f.sample_count == 20 * 24 * 60 and f.no_forecast_reason is None
    assert f.headroom_kw == pytest.approx(f.capacity_kw - f.current_load_kw, abs=1e-3)


def test_same_input_same_output():
    assert compute_forecast(series(20), now=NOW) == compute_forecast(series(20), now=NOW)


def test_flat_load():
    f = compute_forecast(series(20, per_day=0.0), now=NOW)
    assert f.status == "flat" and f.exhaustion_date is None and f.no_forecast_reason


def test_falling_load_is_flat_not_negative_days():
    f = compute_forecast(series(20, per_day=-0.5), now=NOW)
    assert f.status == "flat" and f.days_to_exhaustion is None


def test_beyond_horizon_is_reported_as_such():
    f = compute_forecast(series(20, per_day=0.1, capacity=100.0), now=NOW)
    assert f.status == "flat" and "horizon" in f.no_forecast_reason


def test_already_over_capacity():
    f = compute_forecast(series(10, start_load=95.0, per_day=1.0), now=NOW)
    assert f.status == "already_over_capacity" and f.exhaustion_date == NOW and f.days_to_exhaustion == 0
    assert f.headroom_kw < 0


def test_sparse_needs_enough_days():
    f = compute_forecast(series(3), now=NOW)
    assert f.status == "sparse" and f.exhaustion_date is None and f.day_count < 7


def test_low_coverage_is_sparse():
    f = compute_forecast(series(20, coverage=0.2), now=NOW)
    assert f.status == "sparse"


def test_stale_when_newest_snapshot_is_old():
    f = compute_forecast(series(20, end_gap_hours=12), now=NOW)
    assert f.status == "stale" and f.exhaustion_date is None


def test_missing_without_snapshots_or_capacity():
    assert compute_forecast([], now=NOW).status == "missing"
    f = compute_forecast(series(20, capacity=None), now=NOW)
    assert f.status == "missing" and "capacity" in f.no_forecast_reason


def test_estimated_mixed_when_window_rests_on_estimates():
    pts = series(20)
    pts = [SnapshotPoint(p.bucket_start, p.load_kw, "estimated" if i % 2 else "measured", p.capacity_kw, p.coverage_ratio, p.sample_count) for i, p in enumerate(pts)]
    f = compute_forecast(pts, now=NOW)
    assert f.status == "estimated_mixed" and f.exhaustion_date is None


def test_noisy_series_gets_lower_confidence():
    pts = series(20)
    noisy = [SnapshotPoint(p.bucket_start, p.load_kw + (15 if (p.bucket_start.day % 2) else -15), p.load_basis, p.capacity_kw, 1.0, 60) for p in pts]
    f = compute_forecast(noisy, now=NOW)
    assert f.confidence in ("low", "medium") and f.r_squared < 0.7
