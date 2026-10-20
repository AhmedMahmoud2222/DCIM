"""Issue #105: one freshness policy for every environmental view. The #104 `environment_overlay` and the #105 current-value
service must classify the same reading the same way, including at the exact 3 x poll-interval boundary, for different
integration poll intervals, for missing data and for rows that cannot be trusted."""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.application import spatial_overlays
from app.application.thermal import environment
from app.application.thermal.environment import FRESH_POLL_MULTIPLE, classify_reading
from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import C, TelemetrySeeder, make_sensor, make_world, seed_plan

# (age seconds, poll interval seconds, stored unit, expected classification)
CASES = [
    (5, 60, "degC", "measured_fresh"),
    (170, 60, "degC", "measured_fresh"),
    (190, 60, "degC", "measured_stale"),  # just past 3 x 60
    (800, 300, "degC", "measured_fresh"),  # the same age is fresh at a slower poll interval
    (1000, 300, "degC", "measured_stale"),
    (10, 10, "degC", "measured_fresh"),  # 3 x 10 s = 30 s
    (30, 1, "degC", "measured_stale"),  # a 1 s poll interval makes 30 s old data stale
    (5, 60, "degF", "invalid"),  # stored unit is not canonical: never reinterpreted
    (-900, 60, "degC", "invalid"),  # stamped 15 minutes in the future
]
OVERLAY_LABEL = {"measured_fresh": "measured", "measured_stale": "stale", "invalid": "invalid"}


def test_there_is_one_freshness_rule_in_the_code_base():
    assert "classify_reading" in inspect.getsource(spatial_overlays.environment_overlay)
    assert not hasattr(spatial_overlays, "FRESH_POLL_MULTIPLE")
    assert FRESH_POLL_MULTIPLE == 3 and environment.FRESH_POLL_MULTIPLE == 3


def test_boundary_semantics_of_the_shared_function():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert classify_reading(now - timedelta(seconds=180), now, 60)[0] == "measured_fresh"  # exactly 3 x poll is still fresh
    assert classify_reading(now - timedelta(seconds=180.001), now, 60)[0] == "measured_stale"
    assert classify_reading(now - timedelta(seconds=900), now, 300)[0] == "measured_fresh"
    assert classify_reading(now - timedelta(seconds=901), now, 300)[0] == "measured_stale"
    assert classify_reading(now + timedelta(seconds=120), now, 60)[0] == "measured_fresh"  # tolerated clock skew
    assert classify_reading(now + timedelta(seconds=121), now, 60)[0] == "invalid"
    assert classify_reading(now, now, 0)[0] == "measured_fresh" and classify_reading(now - timedelta(seconds=4), now, 0)[0] == "measured_stale"


@pytest.mark.parametrize(("age", "poll", "unit", "expected"), CASES)
async def test_overlay_and_current_value_service_agree(client, auth_headers, db_session, age, poll, unit, expected):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    seed = TelemetrySeeder(db_session)
    rack = await create_rack(client, admin, auth_headers, room_id=site["room"], x_mm=500, y_mm=500, rotation_deg=0)
    sensor = await make_sensor(client, admin, site["site"], site["room"], 2000, 2000)
    for asset in (rack["id"], sensor["id"]):
        await seed.reading(asset, "temperature_c", 21.0, age_seconds=age, poll=poll, unit=unit)

    overlay = (await client.get(f"/api/v1/spatial/rooms/{site['room']}/overlays", params={"kinds": "environment"}, headers=admin)).json()
    item = next(i for i in overlay["overlays"]["environment"]["items"] if i["asset_id"] == rack["id"])
    assert item["data_quality"] == OVERLAY_LABEL[expected]

    current = (await client.get(f"{C}/rooms/{site['room']}/environment", params={"metric": "temperature_c"}, headers=admin)).json()
    assert current["points"][0]["state"] == (expected if expected != "invalid" else "invalid")
    # the overlay's state follows: only a fresh measured value is "normal"
    assert (item["state"] == "normal") == (expected == "measured_fresh")


async def test_missing_data_is_missing_in_both_views(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    rack = await create_rack(client, admin, auth_headers, room_id=site["room"], x_mm=500, y_mm=500, rotation_deg=0)
    sensor = await make_sensor(client, admin, site["site"], site["room"], 2000, 2000)
    overlay = (await client.get(f"/api/v1/spatial/rooms/{site['room']}/overlays", params={"kinds": "environment"}, headers=admin)).json()
    item = next(i for i in overlay["overlays"]["environment"]["items"] if i["asset_id"] == rack["id"])
    assert item["data_quality"] == "missing" and item["state"] == "unavailable" and item["value"] is None
    current = (await client.get(f"{C}/rooms/{site['room']}/environment", params={"metric": "temperature_c"}, headers=admin)).json()
    assert [p["state"] for p in current["points"]] == ["missing"] and sensor["id"] == current["points"][0]["sensor_id"]


async def test_overlay_response_contract_is_unchanged(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    rack = await create_rack(client, admin, auth_headers, room_id=site["room"], x_mm=500, y_mm=500, rotation_deg=0)
    await TelemetrySeeder(db_session).reading(rack["id"], "humidity_percent", 45.0, age_seconds=10)
    body = (await client.get(f"/api/v1/spatial/rooms/{site['room']}/overlays", params={"kinds": "environment"}, headers=admin)).json()
    env = body["overlays"]["environment"]
    assert env["source"] == "telemetry_latest" and env["estimated_values"] is False and env["truncated"] is False
    item = next(i for i in env["items"] if i["asset_id"] == rack["id"])
    assert set(item) >= {"asset_id", "asset_kind", "metric", "value", "unit", "occurred_at", "age_seconds", "expected_poll_interval_seconds", "data_quality", "state", "reason"}
    assert (item["metric"], item["value"], item["unit"], item["asset_kind"], item["data_quality"], item["state"]) == ("humidity_percent", 45.0, "%", "rack", "measured", "normal")
