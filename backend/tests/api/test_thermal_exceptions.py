"""Issue #105: environmental exception view (persistent alarms are read, derived conditions are computed)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.api._thermal_helpers import (
    C,
    TelemetrySeeder,
    activate,
    make_sensor,
    make_unit,
    make_world,
    make_zone,
    relate,
    seed_plan,
)


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    return {"admin": admin, "site": site["site"], "room": site["room"], "auth_headers": auth_headers, "seed": TelemetrySeeder(db_session)}


async def exceptions(client, world, headers=None):
    response = await client.get(f"{C}/rooms/{world['room']}/exceptions", headers=headers or world["admin"])
    assert response.status_code == 200, response.text
    return response.json()


def kinds(body):
    return {(i["type"], i["subject_id"]) for i in body["items"]}


async def test_stale_missing_and_unlocated_sensors_are_reported_as_derived(client, world):
    stale = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000, name="Stale")
    missing = await make_sensor(client, world["admin"], world["site"], world["room"], 2000, 1000, name="Missing")
    floating = await make_sensor(client, world["admin"], world["site"], world["room"], name="Floating")
    fine = await make_sensor(client, world["admin"], world["site"], world["room"], 3000, 1000, name="Fine")
    await world["seed"].reading(stale["id"], "temperature_c", 22.0, age_seconds=2000)
    await world["seed"].reading(floating["id"], "temperature_c", 22.0)
    await world["seed"].reading(fine["id"], "temperature_c", 22.0)
    body = await exceptions(client, world)
    found = kinds(body)
    assert ("stale_sensor", stale["id"]) in found and ("missing_sensor_data", missing["id"]) in found and ("sensor_not_located", floating["id"]) in found
    assert not any(sid == fine["id"] for _, sid in found)
    assert all(i["source"] == "derived" for i in body["items"]) and body["sources"]["derived"].endswith("not persisted.")
    assert next(i for i in body["items"] if i["type"] == "stale_sensor")["age_seconds"] >= 2000


async def test_planned_sensors_are_not_expected_to_report(client, world):
    planned = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000, activate_it=False)
    body = await exceptions(client, world)
    assert not any(i["subject_id"] == planned["id"] for i in body["items"])
    await activate(client, world["admin"], planned["id"])
    assert ("missing_sensor_data", planned["id"]) in kinds(await exceptions(client, world))


async def test_persistent_alarms_are_read_from_the_existing_alarm_store(client, world, db_session):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000, name="Hot")
    reading = await world["seed"].reading(sensor["id"], "temperature_c", 41.0)
    rule_id = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) VALUES (:r, :i, 'temperature_c', 'threshold_high', 35, 'degC', true, 'Inlet too hot')"),
        {"r": rule_id, "i": reading.integration_id},
    )
    await db_session.execute(
        text("INSERT INTO alarm (id, rule_id, integration_id, telemetry_reading_id, managed_asset_id, subject_key, status, opened_at, last_value, details) VALUES (gen_random_uuid(), :r, :i, :t, :a, 'k', 'ACTIVE', now(), 41, '{}'::jsonb)"),
        {"r": rule_id, "i": reading.integration_id, "t": reading.id, "a": sensor["id"]},
    )
    await db_session.commit()
    before = (await db_session.execute(text("SELECT count(*) FROM alarm"))).scalar_one()
    body = await exceptions(client, world)
    alarm = next(i for i in body["items"] if i["type"] == "high_temperature")
    assert alarm["source"] == "alarm" and alarm["severity"] == "critical" and alarm["subject_id"] == sensor["id"] and alarm["value"] == 41.0
    assert (await db_session.execute(text("SELECT count(*) FROM alarm"))).scalar_one() == before  # reading the view creates no alarm
    assert body["counts"]["critical"] >= 1


async def test_alarm_items_require_alarm_read(client, world, db_session):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000)
    reading = await world["seed"].reading(sensor["id"], "temperature_c", 41.0)
    rule_id = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) VALUES (:r, :i, 'temperature_c', 'threshold_high', 35, 'degC', true, 'r')"),
        {"r": rule_id, "i": reading.integration_id},
    )
    await db_session.execute(
        text("INSERT INTO alarm (id, rule_id, integration_id, managed_asset_id, subject_key, status, opened_at, last_value, details) VALUES (gen_random_uuid(), :r, :i, :a, 'k', 'ACTIVE', now(), 41, '{}'::jsonb)"),
        {"r": rule_id, "i": reading.integration_id, "a": sensor["id"]},
    )
    await db_session.commit()
    viewer = await world["auth_headers"]("Viewer")
    me = (await client.get("/api/v1/auth/me", headers=viewer)).json()
    group = (await client.post("/api/v1/groups", json={"name": f"g-{uuid.uuid4().hex[:5]}"}, headers=world["admin"])).json()["id"]
    await client.put(f"/api/v1/groups/{group}/permissions", json={"allow": [], "deny": ["alarm:read"]}, headers=world["admin"])
    await client.put(f"/api/v1/groups/{group}/members", json={"user_ids": [me["id"]]}, headers=world["admin"])
    body = await exceptions(client, world, viewer)
    assert not any(i["source"] == "alarm" for i in body["items"]) and "alarm:read" in body["alarms_omitted"]


async def test_unavailable_cooling_unit_and_capacity_conditions(client, world, monkeypatch):
    admin, room = world["admin"], world["room"]
    zone = await make_zone(client, admin, room, name="Hall")
    unit = await make_unit(client, admin, world["site"], rated_cooling_capacity_kw=50, operating_status="fault", name="CRAH-1")
    await activate(client, admin, unit["id"])
    await client.put(f"{C}/units/{unit['id']}/placement", json={"room_id": room, "x_mm": 100, "y_mm": 100}, headers=admin)
    other = await make_unit(client, admin, world["site"], name="Unplaced")
    await relate(client, admin, unit["id"], zone["id"])

    async def _rollup(db, site_id, now, freshness_seconds=900):
        return SimpleNamespace(racks={}, rooms={uuid.UUID(room): SimpleNamespace(load_kw=30.0, quality="measured")})

    monkeypatch.setattr("app.application.thermal.capacity.rollup_for_site", _rollup)
    body = await exceptions(client, world)
    found = kinds(body)
    assert ("cooling_unit_unavailable", unit["id"]) in found
    assert ("cooling_redundancy_lost", zone["id"]) in found  # nothing available => unavailable pool
    assert other["id"]
    await client.patch(f"{C}/units/{unit['id']}", json={"operating_status": "online"}, headers={**admin, "If-Match": "1"})
    body = await exceptions(client, world)
    assert ("cooling_unit_unavailable", unit["id"]) not in kinds(body)
    assert ("cooling_single_unit", zone["id"]) in kinds(body)
    unit2 = await make_unit(client, admin, world["site"], rated_cooling_capacity_kw=20, name="Small")
    await activate(client, admin, unit2["id"])
    await client.patch(f"{C}/units/{unit2['id']}", json={"operating_status": "online"}, headers={**admin, "If-Match": "1"})
    await relate(client, admin, unit2["id"], zone["id"])
    body = await exceptions(client, world)
    assert not any(i["type"] == "capacity_headroom_breach" for i in body["items"])  # 30 of 70 kW is within limits
    assert ("cooling_redundancy_lost", zone["id"]) in kinds(body)  # but 30 kW does not fit in the 20 kW left after losing the 50 kW unit

    async def _heavier(db, site_id, now, freshness_seconds=900):
        return SimpleNamespace(racks={}, rooms={uuid.UUID(room): SimpleNamespace(load_kw=60.0, quality="measured")})

    monkeypatch.setattr("app.application.thermal.capacity.rollup_for_site", _heavier)
    warn = next(i for i in (await exceptions(client, world))["items"] if i["type"] == "capacity_headroom_breach")
    assert warn["severity"] == "warning" and warn["headroom_kw"] == 10.0 and warn["utilization_pct"] == 85.7

    async def _overload(db, site_id, now, freshness_seconds=900):
        return SimpleNamespace(racks={}, rooms={uuid.UUID(room): SimpleNamespace(load_kw=80.0, quality="measured")})

    monkeypatch.setattr("app.application.thermal.capacity.rollup_for_site", _overload)
    crit = next(i for i in (await exceptions(client, world))["items"] if i["type"] == "capacity_headroom_breach")
    assert crit["severity"] == "critical" and crit["headroom_kw"] == -10.0


async def test_capacity_inputs_incomplete_and_no_cooling_assigned(client, world):
    admin, room = world["admin"], world["room"]
    zone = await make_zone(client, admin, room, name="Bare")
    body = await exceptions(client, world)
    assert ("no_cooling_assigned", zone["id"]) in kinds(body)
    unit = await make_unit(client, admin, world["site"], name="NoRating", operating_status="online")
    await activate(client, admin, unit["id"])
    await relate(client, admin, unit["id"], zone["id"])
    body = await exceptions(client, world)
    assert ("capacity_inputs_incomplete", zone["id"]) in kinds(body) and next(i for i in body["items"] if i["type"] == "capacity_inputs_incomplete")["severity"] == "info"


async def test_zone_with_no_fresh_sensor_is_flagged_as_inadequate_data_quality(client, world):
    admin, room = world["admin"], world["room"]
    aisle = await make_zone(client, admin, room, zone_kind="cold_aisle", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=4000, name="Cold")
    empty = await make_zone(client, admin, room, zone_kind="hot_aisle", geometry_type="rect", x_mm=4000, y_mm=0, width_mm=1500, height_mm=4000, name="Hot")
    sensor = await make_sensor(client, admin, world["site"], room, 1000, 1000)
    await world["seed"].reading(sensor["id"], "temperature_c", 22.0, age_seconds=5000)
    body = await exceptions(client, world)
    quality = {i["subject_id"]: i for i in body["items"] if i["type"] == "zone_data_quality"}
    assert quality[aisle["id"]]["sensors_inside"] == 1 and quality[aisle["id"]]["fresh_inside"] == 0
    assert quality[empty["id"]]["sensors_inside"] == 0
    await world["seed"].reading(sensor["id"], "temperature_c", 22.0)
    body = await exceptions(client, world)
    assert aisle["id"] not in {i["subject_id"] for i in body["items"] if i["type"] == "zone_data_quality"}


async def test_items_are_ordered_by_severity_and_bounded(client, world):
    for i in range(3):
        await make_sensor(client, world["admin"], world["site"], world["room"], 500 + i * 100, 500, name=f"M{i}")
    body = await exceptions(client, world)
    ranks = [{"critical": 0, "warning": 1, "info": 2}[i["severity"]] for i in body["items"]]
    assert ranks == sorted(ranks) and body["truncated"] is False and len(body["items"]) == 3
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is UTC or datetime.fromisoformat(body["generated_at"]).utcoffset() is not None
