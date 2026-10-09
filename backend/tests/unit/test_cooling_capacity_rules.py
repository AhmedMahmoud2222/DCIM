"""Issue #105: deterministic cooling redundancy / availability rules (pure)."""

import uuid

from app.application.thermal.capacity import UnitIn, evaluate_pool, sum_known, worst_pool_state


def unit(kw=100.0, *, lifecycle="active", status="online", configured=None, kind="crah"):
    return UnitIn(uuid.uuid4(), f"u{kw}", kind, lifecycle, status, kw, configured)


def test_availability_needs_active_lifecycle_and_online_or_standby_status():
    assert unit().available
    assert unit(status="standby").available
    for status in ("offline", "fault", "unknown"):
        assert not unit(status=status).available
    for lifecycle in ("planned", "maintenance", "decommissioned", "removed", "reserved"):
        assert not unit(lifecycle=lifecycle).available
    assert unit(status="fault").unavailable_reason == "operating_fault"
    assert unit(lifecycle="maintenance").unavailable_reason == "lifecycle_maintenance"


def test_effective_capacity_prefers_the_configured_figure_and_never_defaults_unknown_to_zero():
    assert unit(100, configured=80).effective_kw == 80
    assert unit(100).effective_kw == 100
    assert UnitIn(uuid.uuid4(), "x", "crah", "active", "online", None, None).effective_kw is None


def test_no_units_is_not_configured():
    assert evaluate_pool([], 10.0)["state"] == "not_configured"


def test_all_units_down_is_unavailable():
    assert evaluate_pool([unit(status="offline"), unit(status="fault")], 10.0)["state"] == "unavailable"


def test_one_unit_is_single_unit_even_when_healthy():
    assert evaluate_pool([unit()], 10.0)["state"] == "single_unit"


def test_n_plus_1_is_verified_only_when_load_fits_without_the_largest_unit():
    result = evaluate_pool([unit(100), unit(100), unit(100)], 190.0)
    assert result["state"] == "redundant" and result["n_plus_1_verified"] is True
    over = evaluate_pool([unit(100), unit(100), unit(100)], 210.0)
    assert over["state"] == "degraded" and over["reason"] == "insufficient_n_plus_1_capacity" and over["n_plus_1_verified"] is False


def test_two_units_each_needed_for_the_load_is_not_redundant():
    assert evaluate_pool([unit(100), unit(100)], 150.0)["state"] == "degraded"
    assert evaluate_pool([unit(100), unit(100)], 90.0)["state"] == "redundant"


def test_an_unavailable_member_degrades_the_pool():
    result = evaluate_pool([unit(100), unit(100), unit(status="offline")], 50.0)
    assert result["state"] == "degraded" and result["reason"] == "unit_unavailable"
    assert len(result["unavailable_unit_ids"]) == 1


def test_unknown_load_or_capacity_is_reported_unverified_not_assumed_redundant():
    assert evaluate_pool([unit(100), unit(100)], None)["state"] == "redundant_unverified"
    no_capacity = UnitIn(uuid.uuid4(), "x", "crah", "active", "online", None, None)
    assert evaluate_pool([unit(100), no_capacity], 10.0)["state"] == "redundant_unverified"


def test_worst_state_across_pools_and_known_sums():
    assert worst_pool_state(["redundant", "degraded", "single_unit"]) == "degraded"
    assert worst_pool_state([]) == "not_configured"
    assert sum_known([1.0, None, 2.0]) == (3.0, False)
    assert sum_known([1.0, 2.0]) == (3.0, True)
