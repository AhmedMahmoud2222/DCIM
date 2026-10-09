"""Issue #105: current-value contract, heat maps, airflow, capacity and exceptions."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import (
    C,
    TelemetrySeeder,
    activate,
    make_group,
    make_sensor,
    make_unit,
    make_world,
    make_zone,
    relate,
    seed_plan,
)

CORNERS = [(1000, 1000, 20.0), (5000, 1000, 30.0), (1000, 3000, 24.0), (5000, 3000, 26.0)]


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    return {"admin": admin, "site": site["site"], "room": site["room"], "org": site["org"], "auth_headers": auth_headers, "seed": TelemetrySeeder(db_session)}


async def four_sensors(client, world, metric="temperature_c", *, kind="temperature", ages=(5, 5, 5, 5), poll=60, values=None):
    sensors = []
    for i, (x, y, _value) in enumerate(CORNERS):
        sensor = await make_sensor(client, world["admin"], world["site"], world["room"], x, y, sensor_kind=kind, name=f"S{i}")
        if ages[i] is not None:
            await world["seed"].reading(sensor["id"], metric, (values or [v for _, _, v in CORNERS])[i], age_seconds=ages[i], poll=poll)
        sensors.append(sensor)
    return sensors


def heat(client, world, **params):
    return client.get(f"{C}/rooms/{world['room']}/heat-map", params=params, headers=world["admin"])


async def test_current_value_contract_has_measured_value_unit_age_and_state(client, world):
    sensors = await four_sensors(client, world, ages=(5, 500, None, 5))
    body = (await client.get(f"{C}/rooms/{world['room']}/environment", params={"metric": "temperature_c"}, headers=world["admin"])).json()
    by_name = {p["name"]: p for p in body["points"]}
    fresh, stale, missing = by_name["S0"], by_name["S1"], by_name["S2"]
    assert (fresh["state"], fresh["value"], fresh["unit"], fresh["value_provenance"]) == ("measured_fresh", 20.0, "degC", "measured")
    assert fresh["occurred_at"] and fresh["age_seconds"] <= 30 and fresh["expected_poll_interval_seconds"] == 60
    assert (stale["state"], stale["value"]) == ("measured_stale", 30.0)  # the old value is shown, but never as current
    assert (missing["state"], missing["value"], missing["occurred_at"]) == ("missing", None, None)
    assert body["counts"] == {"total": 4, "measured_fresh": 2, "measured_stale": 1, "missing": 1, "invalid": 0}
    assert body["points"][0]["x_mm"] is not None and body["points"][0]["sensor_id"] in {s["id"] for s in sensors}


async def test_freshness_boundary_is_three_poll_intervals():
    from app.application.thermal.environment import classify_reading

    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert classify_reading(now - timedelta(seconds=180), now, 60) == ("measured_fresh", 180)
    assert classify_reading(now - timedelta(seconds=181), now, 60)[0] == "measured_stale"
    assert classify_reading(now + timedelta(seconds=30), now, 60) == ("measured_fresh", 0)  # small clock skew tolerated
    assert classify_reading(now + timedelta(seconds=600), now, 60)[0] == "invalid"
    assert classify_reading(now, now, 0)[0] == "measured_fresh"


async def test_healthy_map_separates_measured_points_from_interpolated_cells(client, world):
    await four_sensors(client, world)
    body = (await heat(client, world)).json()
    assert body["state"] == "healthy" and body["state_reasons"] == [] and body["kind"] == "interpolated_operational_estimate"
    assert "not validated CFD" in body["disclaimer"]
    grid = body["grid"]
    assert grid["value_provenance"] == "interpolated" and grid["unit"] == "degC"
    assert len(grid["values"]) == grid["columns"] * grid["rows"] and 20.0 <= grid["min"] <= grid["max"] <= 30.0
    assert all(s["value_provenance"] == "measured" and s["used_in_field"] for s in body["sensors"])
    assert sorted(s["value"] for s in body["sensors"]) == [20.0, 24.0, 26.0, 30.0]  # measured values untouched by interpolation
    quality = body["quality"]
    assert (quality["sensor_count"], quality["fresh_count"], quality["stale_count"], quality["missing_count"]) == (4, 4, 0, 0)
    assert quality["coverage_percent"] > 0 and quality["coverage_class"] in ("good", "fair") and quality["max_age_skew_seconds"] <= 5
    assert body["method"]["name"] == "idw" and body["method"]["barrier_aware"] is False and body["method"]["stale_policy"] == "excluded"
    assert len(body["source_set"]["contributing_sensor_ids"]) == 4
    assert body["generated_at"] and body["as_of"] and body["quality"]["source_time_range"]["oldest"]


async def test_map_is_deterministic_for_a_fixed_snapshot(client, world):
    await four_sensors(client, world)
    as_of = datetime.now(UTC).isoformat()
    first = (await heat(client, world, as_of=as_of)).json()
    second = (await heat(client, world, as_of=as_of)).json()
    assert first["grid"] == second["grid"] and first["source_set"] == second["source_set"]


async def test_stale_sensor_is_excluded_and_the_map_is_never_called_healthy(client, world):
    await four_sensors(client, world, ages=(5, 900, 5, 5))
    body = (await heat(client, world)).json()
    assert body["state"] == "partial" and "some_expected_sensors_not_contributing" in body["state_reasons"]
    stale = next(s for s in body["sensors"] if s["state"] == "measured_stale")
    assert stale["used_in_field"] is False and stale["excluded_reason"] == "measured_stale"
    assert body["quality"]["stale_count"] == 1 and body["quality"]["fresh_count"] == 3
    assert body["grid"] is not None and body["grid"]["max"] < 30.0 + 1e-9  # the stale 30 degC sensor no longer pulls the field


async def test_missing_sensor_makes_the_map_partial(client, world):
    await four_sensors(client, world, ages=(5, 5, 5, None))
    body = (await heat(client, world)).json()
    assert body["state"] == "partial" and body["quality"]["missing_count"] == 1
    assert next(s for s in body["sensors"] if s["state"] == "missing")["value"] is None


async def test_too_few_fresh_sensors_gives_no_field_at_all(client, world):
    await four_sensors(client, world, ages=(5, 5, 900, None))
    body = (await heat(client, world)).json()
    assert body["state"] == "unavailable" and body["grid"] is None and "insufficient_fresh_sensors" in body["state_reasons"]
    assert body["quality"]["coverage_percent"] == 0.0 and body["quality"]["coverage_class"] == "none"
    only_stale = await four_sensors(client, world, ages=(900, 900, 900, 900))
    assert len(only_stale) == 4
    after = (await heat(client, world)).json()
    assert after["state"] == "unavailable" and after["grid"] is None  # an all-stale room never renders as healthy


async def test_one_sensor_never_becomes_a_map(client, world):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 3000, 2000)
    await world["seed"].reading(sensor["id"], "temperature_c", 22.0)
    body = (await heat(client, world)).json()
    assert body["state"] == "unavailable" and body["grid"] is None and body["quality"]["fresh_count"] == 1


async def test_readings_too_far_apart_in_time_degrade_the_map_instead_of_faking_coherence(client, world):
    await four_sensors(client, world, ages=(5, 5, 5, 700), poll=600)  # all fresh for a 10 min poll, but 695 s apart
    default = (await heat(client, world)).json()
    assert default["state"] == "healthy" and default["quality"]["max_age_skew_seconds"] >= 690
    strict = (await heat(client, world, max_skew_seconds=60)).json()
    assert strict["state"] == "degraded" and strict["grid"] is None and "age_skew_exceeded" in strict["state_reasons"]
    assert strict["quality"]["max_age_skew_allowed_seconds"] == 60
    assert {s["state"] for s in strict["sensors"]} == {"measured_fresh"}  # the points themselves are still shown


async def test_as_of_snapshot_ignores_newer_readings(client, world):
    sensors = await four_sensors(client, world)
    older = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    body = (await heat(client, world, as_of=older)).json()
    assert body["state"] == "unavailable" and body["quality"]["missing_count"] == 4  # nothing existed two hours ago
    old_at = datetime.now(UTC) - timedelta(hours=2, seconds=10)
    for s in sensors:
        await world["seed"].reading(s["id"], "temperature_c", 40.0, at=old_at)
    snapshot = (await heat(client, world, as_of=older)).json()
    assert snapshot["state"] == "healthy" and snapshot["grid"]["max"] == 40.0  # judged fresh relative to the snapshot instant
    now_map = (await heat(client, world)).json()
    assert now_map["grid"]["max"] <= 30.0


async def test_no_cross_room_interpolation(client, world, db_session):
    await four_sensors(client, world)
    floor_id = (await db_session.execute(text("SELECT floor_id FROM room WHERE id = :i"), {"i": world["room"]})).scalar_one()
    admin = world["admin"]
    other_room = (await client.post("/api/v1/rooms", json={"floor_id": str(floor_id), "code": "R2", "name": "Other"}, headers=admin)).json()["id"]
    await seed_plan(db_session, other_room)
    stray = await make_sensor(client, admin, world["site"], other_room, 3000, 2000, name="Stray")
    await world["seed"].reading(stray["id"], "temperature_c", 90.0)
    body = (await heat(client, world)).json()
    assert "Stray" not in {s["name"] for s in body["sensors"]} and body["grid"]["max"] <= 30.0 and len(body["sensors"]) == 4
    other = (await client.get(f"{C}/rooms/{other_room}/heat-map", headers=admin)).json()
    assert [s["name"] for s in other["sensors"]] == ["Stray"] and other["state"] == "unavailable"


async def test_unlocated_and_out_of_room_sensors_are_listed_but_never_interpolated(client, world, db_session):
    sensors = await four_sensors(client, world)
    floating = await make_sensor(client, world["admin"], world["site"], world["room"], name="Floating")  # associated with the room, no position
    await world["seed"].reading(floating["id"], "temperature_c", 99.0)
    await db_session.execute(text("UPDATE equipment_placement SET x_mm = 9500, y_mm = 100 WHERE equipment_id = :i AND effective_to IS NULL"), {"i": sensors[0]["id"]})
    await db_session.commit()
    body = (await heat(client, world)).json()
    by_name = {s["name"]: s for s in body["sensors"]}
    assert by_name["Floating"]["excluded_reason"] == "no_location" and by_name["Floating"]["x_mm"] is None and by_name["Floating"]["used_in_field"] is False
    assert by_name["S0"]["excluded_reason"] == "outside_room_boundary" and by_name["S0"]["used_in_field"] is False
    assert body["state"] == "partial" and body["quality"]["unlocated_count"] == 1
    assert body["grid"]["max"] < 99.0
    # only three usable sensors remain (S1..S3): still a field, but dropping one more would not be
    assert body["quality"]["contributing_count"] == 3


async def test_map_needs_an_active_calibrated_plan_but_still_lists_sensors(client, world, db_session):
    admin, other = await make_world(client, world["auth_headers"])
    sensor = await make_sensor(client, admin, other["site"], other["room"])
    await world["seed"].reading(sensor["id"], "temperature_c", 22.0)
    none = (await client.get(f"{C}/rooms/{other['room']}/heat-map", headers=admin)).json()
    assert none["state"] == "unavailable" and "no_active_floor_plan" in none["state_reasons"] and none["grid"] is None and len(none["sensors"]) == 1
    await seed_plan(db_session, other["room"], calibrated=False)
    uncalibrated = (await client.get(f"{C}/rooms/{other['room']}/heat-map", headers=admin)).json()
    assert "no_calibration" in uncalibrated["state_reasons"] and uncalibrated["grid"] is None


async def test_invalid_rows_are_flagged_not_reinterpreted(client, world):
    sensors = await four_sensors(client, world, ages=(5, 5, 5, 5))
    await world["seed"].reading(sensors[0]["id"], "temperature_c", 70.0, unit="degF")  # legacy non-canonical unit text on a stored row
    await world["seed"].reading(sensors[1]["id"], "temperature_c", 31.0, at=datetime.now(UTC) + timedelta(hours=1))
    body = (await heat(client, world)).json()
    by_name = {s["name"]: s for s in body["sensors"]}
    assert by_name["S0"]["state"] == "invalid" and by_name["S0"]["invalid_reason"] == "unit_mismatch" and by_name["S0"]["value"] is None
    assert by_name["S1"]["state"] == "invalid" and by_name["S1"]["invalid_reason"] == "future_timestamp"
    assert body["quality"]["invalid_count"] == 2 and body["state"] == "unavailable" and body["grid"] is None  # only two valid sensors remain


async def test_humidity_map_has_its_own_unit_assumptions_and_sensors(client, world):
    await four_sensors(client, world, metric="humidity_percent", kind="humidity", values=[40.0, 55.0, 45.0, 50.0])
    temp = (await heat(client, world, metric="temperature_c")).json()
    assert temp["sensors"] == [] and temp["state"] == "unavailable"  # humidity sensors never feed the temperature map
    body = (await heat(client, world, metric="humidity_percent")).json()
    assert body["unit"] == "%" and body["state"] == "healthy" and 40.0 <= body["grid"]["min"] <= body["grid"]["max"] <= 55.0
    assert any("Relative humidity depends on temperature" in a for a in body["method"]["assumptions"])
    assert not any("Relative humidity" in a for a in temp["method"]["assumptions"])
    assert (await heat(client, world, metric="power_kw")).status_code == 422


async def test_combined_sensor_feeds_both_maps(client, world):
    for i, (x, y, v) in enumerate(CORNERS):
        sensor = await make_sensor(client, world["admin"], world["site"], world["room"], x, y, sensor_kind="combined", name=f"C{i}")
        await world["seed"].reading(sensor["id"], "temperature_c", v)
        await world["seed"].reading(sensor["id"], "humidity_percent", 30 + v)
    assert (await heat(client, world, metric="temperature_c")).json()["state"] == "healthy"
    assert (await heat(client, world, metric="humidity_percent")).json()["state"] == "healthy"


async def test_sensor_roles_are_separated_by_default(client, world):
    await four_sensors(client, world)
    exhaust = await make_sensor(client, world["admin"], world["site"], world["room"], 3000, 2000, name="Exhaust", measurement_role="rack_exhaust")
    await world["seed"].reading(exhaust["id"], "temperature_c", 45.0)
    default = (await heat(client, world)).json()
    assert "Exhaust" not in {s["name"] for s in default["sensors"]} and default["grid"]["max"] < 45.0
    everything = (await heat(client, world, sensor_roles="ambient,rack_inlet,rack_exhaust,other")).json()
    assert "Exhaust" in {s["name"] for s in everything["sensors"]}
    assert (await heat(client, world, sensor_roles="bogus")).status_code == 422


async def test_resolution_and_radius_parameters_are_bounded(client, world):
    await four_sensors(client, world)
    assert (await heat(client, world, cell_mm=10)).status_code == 422
    assert (await heat(client, world, radius_mm=99999)).status_code == 422
    assert (await heat(client, world, max_skew_seconds=1)).status_code == 422
    fine = (await heat(client, world, cell_mm=100)).json()
    assert fine["grid"]["columns"] * fine["grid"]["rows"] <= 6400
    small = (await heat(client, world, radius_mm=500)).json()
    assert small["method"]["radius_mm"] == 500 and small["quality"]["coverage_percent"] < fine["quality"]["coverage_percent"]


async def test_threshold_bands_come_from_existing_alarm_rules(client, world, db_session):
    sensors = await four_sensors(client, world)
    integration_id = (await db_session.execute(text("SELECT id FROM integration LIMIT 1"))).scalar_one()
    await db_session.execute(
        text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) VALUES (gen_random_uuid(), :i, 'temperature_c', 'threshold_high', 27, 'degC', true, 'High inlet')"),
        {"i": integration_id},
    )
    await db_session.commit()
    body = (await heat(client, world)).json()
    assert body["thresholds"] == [{"rule_type": "threshold_high", "threshold": 27.0, "unit": "degC", "name": "High inlet"}]
    assert sensors


async def test_source_identity_requires_integration_read(client, world):
    await four_sensors(client, world)
    full = (await heat(client, world)).json()
    assert full["sensors"][0]["source"]["integration_id"] and full["sensors"][0]["source"]["integration_name"]
    admin = world["admin"]
    viewer = await world["auth_headers"]("Viewer")
    me = (await client.get("/api/v1/auth/me", headers=viewer)).json()
    group = (await client.post("/api/v1/groups", json={"name": f"deny-{uuid.uuid4().hex[:5]}"}, headers=admin)).json()["id"]
    assert (await client.put(f"/api/v1/groups/{group}/permissions", json={"allow": [], "deny": ["integration:read"]}, headers=admin)).status_code == 200
    assert (await client.put(f"/api/v1/groups/{group}/members", json={"user_ids": [me["id"]]}, headers=admin)).status_code == 200
    masked = (await client.get(f"{C}/rooms/{world['room']}/heat-map", headers=viewer)).json()
    assert masked["state"] == "healthy" and all(s["source"] is None for s in masked["sensors"])
    env = (await client.get(f"{C}/rooms/{world['room']}/environment", headers=viewer)).json()
    assert all(p["source"] is None for p in env["points"])


async def test_airflow_elements_carry_provenance_and_nothing_is_fabricated(client, world):
    admin, room = world["admin"], world["room"]
    configured = await make_unit(client, admin, world["site"], supply_direction_deg=90, airflow_capacity_m3_s=4.5)
    no_direction = await make_unit(client, admin, world["site"], airflow_capacity_m3_s=3.0, name="NoDir")
    chiller = await make_unit(client, admin, world["site"], unit_kind="chiller", name="Plant")
    for u, x in ((configured, 500), (no_direction, 900), (chiller, 1300)):
        assert (await client.put(f"{C}/units/{u['id']}/placement", json={"room_id": room, "x_mm": x, "y_mm": 500}, headers=admin)).status_code == 200
    flow = await make_sensor(client, admin, world["site"], room, 2000, 2000, sensor_kind="airflow", flow_direction_deg=180, name="Flow")
    await world["seed"].reading(flow["id"], "airflow_m3_s", 1.25)
    await world["seed"].reading(flow["id"], "airflow_velocity_m_s", 2.5)
    blind = await make_sensor(client, admin, world["site"], room, 2500, 2000, sensor_kind="airflow", name="Blind")
    await world["seed"].reading(blind["id"], "airflow_m3_s", 0.5)
    silent = await make_sensor(client, admin, world["site"], room, 3000, 2000, sensor_kind="airflow", flow_direction_deg=10, name="Silent")
    body = (await client.get(f"{C}/rooms/{room}/airflow", headers=admin)).json()
    by_name = {e["name"]: e for e in body["elements"]}
    cooling = by_name[configured["name"]]
    assert (cooling["kind"], cooling["direction_deg"], cooling["magnitude_m3_s"], cooling["magnitude_provenance"], cooling["direction_provenance"]) == ("cooling_supply", 90, 4.5, "configured_design", "configured")
    assert cooling["drawable"] is True
    nd = by_name["NoDir"]
    assert nd["drawable"] is False and nd["not_drawable_reasons"] == ["no_configured_direction"] and nd["direction_deg"] is None
    assert "Plant" not in by_name  # a chiller has no room supply vector
    measured = by_name["Flow"]
    assert (measured["magnitude_provenance"], measured["direction_provenance"], measured["state"], measured["drawable"]) == ("measured", "configured", "measured_fresh", True)
    assert measured["volume_flow"]["value"] == 1.25 and measured["velocity"]["value"] == 2.5
    assert by_name["Blind"]["drawable"] is False and "no_configured_direction" in by_name["Blind"]["not_drawable_reasons"]
    assert by_name["Silent"]["drawable"] is False and "no_measured_magnitude" in by_name["Silent"]["not_drawable_reasons"] and by_name["Silent"]["state"] == "missing"
    assert body["provenance_summary"] == {"measured_magnitude": 1, "configured_design": 1, "modelled": 0, "not_drawable": 3}  # drawn: Flow + the CRAH with a direction
    assert "CFD" in body["note"] and silent["id"]
    assert all(e["magnitude_provenance"] != "modelled" and e["direction_provenance"] != "modelled" for e in body["elements"])


async def test_airflow_stale_measurement_is_marked_stale(client, world):
    flow = await make_sensor(client, world["admin"], world["site"], world["room"], 2000, 2000, sensor_kind="airflow", flow_direction_deg=0, name="Old")
    await world["seed"].reading(flow["id"], "airflow_m3_s", 1.0, age_seconds=3000)
    body = (await client.get(f"{C}/rooms/{world['room']}/airflow", headers=world["admin"])).json()
    assert body["elements"][0]["state"] == "measured_stale" and body["elements"][0]["magnitude_provenance"] == "measured"


# --------------------------------------------------------------------------------------------------- capacity


def fake_rollup(monkeypatch, racks=None, rooms=None):
    async def _rollup(db, site_id, now, freshness_seconds=900):
        return SimpleNamespace(racks=racks or {}, rooms=rooms or {})

    monkeypatch.setattr("app.application.thermal.capacity.rollup_for_site", _rollup)


def scope_result(load_kw, quality="measured"):
    return SimpleNamespace(load_kw=load_kw, quality=quality)


async def active_unit(client, world, **extra):
    unit = await make_unit(client, world["admin"], world["site"], **extra)
    await activate(client, world["admin"], unit["id"])
    return unit


async def capacity(client, world):
    response = await client.get(f"{C}/rooms/{world['room']}/capacity", headers=world["admin"])
    assert response.status_code == 200, response.text
    return response.json()


async def test_capacity_without_a_served_zone_is_not_configured(client, world):
    body = await capacity(client, world)
    assert body["state"] == "not_configured" and body["zones"] == [] and body["reasons"] == ["no_served_zone"]


async def test_capacity_distinguishes_rated_available_load_and_headroom(client, world, monkeypatch):
    zone = await make_zone(client, world["admin"], world["room"])
    a = await active_unit(client, world, rated_cooling_capacity_kw=100, name="A")
    b = await active_unit(client, world, rated_cooling_capacity_kw=100, configured_cooling_capacity_kw=80, name="B")
    c = await active_unit(client, world, rated_cooling_capacity_kw=100, name="C")
    for u in (a, b, c):
        await relate(client, world["admin"], u["id"], zone["id"])
    fake_rollup(monkeypatch, rooms={uuid.UUID(world["room"]): scope_result(120.0, "mixed")})
    z = (await capacity(client, world))["zones"][0]
    assert (z["installed_rated_kw"], z["available_kw"], z["unit_count"], z["available_units"]) == (300.0, 280.0, 3, 3)
    assert z["thermal_load"]["thermal_kw"] == 120.0 and z["thermal_load"]["electrical_kw"] == 120.0 and z["thermal_load"]["quality"] == "mixed"
    assert "1.0" in z["thermal_load"]["assumption"] and z["thermal_load"]["electrical_to_thermal_factor"] == 1.0
    assert z["headroom_kw"] == 160.0 and z["utilization_pct"] == 42.9 and z["level"] == "ok"
    assert z["redundancy"]["state"] == "redundant"  # 120 kW <= 280 - 100 largest
    assert next(u for u in z["units"] if u["name"] == "B")["effective_kw"] == 80.0


async def test_maintenance_offline_and_standby_units_change_available_capacity_and_redundancy(client, world, monkeypatch):
    zone = await make_zone(client, world["admin"], world["room"])
    online = await active_unit(client, world, rated_cooling_capacity_kw=100, name="On")
    standby = await active_unit(client, world, rated_cooling_capacity_kw=100, operating_status="standby", name="Standby")
    offline = await active_unit(client, world, rated_cooling_capacity_kw=100, operating_status="offline", name="Off")
    maintenance = await active_unit(client, world, rated_cooling_capacity_kw=100, name="Maint")
    assert (await client.post(f"/api/v1/managed-assets/{maintenance['id']}/lifecycle-transition", json={"to_status": "maintenance"}, headers=world["admin"])).status_code == 200
    for u in (online, standby, offline, maintenance):
        await relate(client, world["admin"], u["id"], zone["id"])
    fake_rollup(monkeypatch, rooms={uuid.UUID(world["room"]): scope_result(50.0)})
    z = (await capacity(client, world))["zones"][0]
    assert z["installed_rated_kw"] == 400.0 and z["available_kw"] == 200.0 and z["available_units"] == 2  # standby counts, offline / maintenance do not
    reasons = {u["name"]: u["unavailable_reason"] for u in z["units"]}
    assert reasons == {"On": None, "Standby": None, "Off": "operating_offline", "Maint": "lifecycle_maintenance"}
    assert z["redundancy"]["state"] == "degraded" and z["redundancy"]["pools"][0]["reason"] == "unit_unavailable"
    assert z["level"] == "warning"


async def test_overload_and_single_unit_and_unavailable_levels(client, world, monkeypatch):
    zone = await make_zone(client, world["admin"], world["room"])
    only = await active_unit(client, world, rated_cooling_capacity_kw=100)
    await relate(client, world["admin"], only["id"], zone["id"])
    fake_rollup(monkeypatch, rooms={uuid.UUID(world["room"]): scope_result(130.0)})
    z = (await capacity(client, world))["zones"][0]
    assert z["headroom_kw"] == -30.0 and z["level"] == "critical" and z["redundancy"]["state"] == "single_unit"
    await client.patch(f"{C}/units/{only['id']}", json={"operating_status": "fault"}, headers={**world["admin"], "If-Match": "1"})
    z = (await capacity(client, world))["zones"][0]
    assert z["redundancy"]["state"] == "unavailable" and z["level"] == "critical" and z["available_kw"] == 0.0 and z["headroom_kw"] == -130.0


async def test_missing_capacity_or_load_is_never_filled_in(client, world, monkeypatch):
    zone = await make_zone(client, world["admin"], world["room"])
    known = await active_unit(client, world, rated_cooling_capacity_kw=100)
    unknown = await active_unit(client, world)
    await relate(client, world["admin"], known["id"], zone["id"])
    await relate(client, world["admin"], unknown["id"], zone["id"])
    fake_rollup(monkeypatch, rooms={uuid.UUID(world["room"]): scope_result(10.0)})
    z = (await capacity(client, world))["zones"][0]
    assert z["available_complete"] is False and z["headroom_kw"] is None and z["utilization_pct"] is None and z["level"] == "unknown"
    assert z["redundancy"]["state"] == "redundant_unverified" and z["redundancy"]["pools"][0]["n_plus_1_verified"] is None
    assert next(u for u in z["units"] if u["capacity_known"] is False)["effective_kw"] is None
    fake_rollup(monkeypatch, rooms={})
    await client.patch(f"{C}/units/{unknown['id']}", json={"rated_cooling_capacity_kw": 100}, headers={**world["admin"], "If-Match": "1"})
    z = (await capacity(client, world))["zones"][0]
    assert z["available_complete"] is True and z["thermal_load"]["state"] == "unknown" and z["headroom_kw"] is None  # no power modelled => no headroom


async def test_zone_with_geometry_counts_only_racks_inside_it(client, world, monkeypatch, auth_headers):
    admin, room = world["admin"], world["room"]
    zone = await make_zone(client, admin, room, name="Left", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=4000)
    inside = await create_rack(client, admin, auth_headers, room_id=room, x_mm=500, y_mm=500, rotation_deg=0, name="In")
    outside = await create_rack(client, admin, auth_headers, room_id=room, x_mm=4500, y_mm=500, rotation_deg=0, name="Out")
    unit = await active_unit(client, world, rated_cooling_capacity_kw=50)
    await relate(client, admin, unit["id"], zone["id"])
    fake_rollup(monkeypatch, racks={uuid.UUID(inside["id"]): scope_result(12.0, "estimated"), uuid.UUID(outside["id"]): scope_result(99.0)})
    z = (await capacity(client, world))["zones"][0]
    assert z["thermal_load"]["thermal_kw"] == 12.0 and z["thermal_load"]["rack_count"] == 1 and z["thermal_load"]["quality"] == "estimated"
    assert z["headroom_kw"] == 38.0
    unplaced = await create_rack(client, admin, auth_headers, room_id=room, name="NoXY")
    fake_rollup(monkeypatch, racks={uuid.UUID(inside["id"]): scope_result(12.0), uuid.UUID(unplaced["id"]): scope_result(5.0)})
    z = (await capacity(client, world))["zones"][0]
    assert z["thermal_load"]["state"] == "incomplete" and z["thermal_load"]["unassigned_rack_count"] == 1 and z["headroom_kw"] is None


async def test_cooling_group_pools_units_across_zones_for_n_plus_1(client, world, monkeypatch, auth_headers):
    admin, room = world["admin"], world["room"]
    left = await make_zone(client, admin, room, name="Left", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=4000)
    right = await make_zone(client, admin, room, name="Right", geometry_type="rect", x_mm=3000, y_mm=0, width_mm=3000, height_mm=4000)
    group = await make_group(client, admin, world["site"])
    u1 = await active_unit(client, world, rated_cooling_capacity_kw=100, cooling_group_id=group["id"], name="U1")
    u2 = await active_unit(client, world, rated_cooling_capacity_kw=100, cooling_group_id=group["id"], name="U2")
    await relate(client, admin, u1["id"], left["id"])
    await relate(client, admin, u2["id"], right["id"])
    r1 = await create_rack(client, admin, auth_headers, room_id=room, x_mm=500, y_mm=500, rotation_deg=0)
    r2 = await create_rack(client, admin, auth_headers, room_id=room, x_mm=3500, y_mm=500, rotation_deg=0)
    fake_rollup(monkeypatch, racks={uuid.UUID(r1["id"]): scope_result(40.0), uuid.UUID(r2["id"]): scope_result(40.0)})
    zones = {z["name"]: z for z in (await capacity(client, world))["zones"]}
    pool = zones["Left"]["redundancy"]["pools"][0]
    assert pool["scope"] == "cooling_group" and pool["pool_load_kw"] == 80.0 and pool["provisioned_units"] == 2  # sibling unit U2 backs up U1
    assert zones["Left"]["redundancy"]["state"] == "redundant" and pool["n_plus_1_verified"] is True  # 80 kW fits in one 100 kW unit
    fake_rollup(monkeypatch, racks={uuid.UUID(r1["id"]): scope_result(70.0), uuid.UUID(r2["id"]): scope_result(70.0)})
    zones = {z["name"]: z for z in (await capacity(client, world))["zones"]}
    assert zones["Left"]["redundancy"]["state"] == "degraded" and zones["Left"]["redundancy"]["pools"][0]["reason"] == "insufficient_n_plus_1_capacity"
    assert zones["Left"]["headroom_kw"] == 30.0  # this zone alone is fine; the shared pool is what lost its spare
