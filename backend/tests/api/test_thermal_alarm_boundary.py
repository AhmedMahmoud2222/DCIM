"""Issue #105 (review blocker B1): a caller without effective `alarm:read` receives no alarm-derived information from
any thermal surface, while keeping every cooling / telemetry / spatial capability their other permissions allow.

All callers go through the real HTTP endpoints. The boundary is structural: the alarm tables are not read at all when
`alarm:read` is absent, so a serializer cannot leak what was never loaded."""

import json
import uuid

import pytest
from sqlalchemy import event, text

from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import C, TelemetrySeeder, make_sensor, seed_plan
from tests.api.test_user_groups import _group, _group_user, _make_site

BASE = ["cooling:read", "spatial:read", "telemetry:read", "power:read", "rack:read"]
RULE_TEMP, RULE_HUM, RULE_AIR = "SECRET-RULE-TEMP-NAME", "SECRET-RULE-HUM-NAME", "SECRET-RULE-AIR-NAME"
VALUES = {"temperature_c": (20.0, 21.0, 22.0), "humidity_percent": (40.0, 45.0, 50.0), "airflow_m3_s": (1.0, 1.5, 2.0)}


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    site = await _make_site(client, admin)
    await seed_plan(db_session, site["room"])
    seed = TelemetrySeeder(db_session)
    racks, sensors = [], []
    for i, (x, y) in enumerate(((1000, 1000), (3000, 1000), (5000, 1000))):
        rack = await create_rack(client, admin, auth_headers, room_id=site["room"], x_mm=x, y_mm=y, rotation_deg=0, name=f"R{i}")
        racks.append(rack["id"])
        sensor = await make_sensor(client, admin, site["site"], name=f"S{i}", sensor_kind="combined", flow_direction_deg=90)
        placed = await client.put(
            f"{C}/sensors/{sensor['id']}/placement",
            json={"room_id": site["room"], "placement_type": "rack_mounted", "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"},
            headers=admin,
        )
        assert placed.status_code == 200, placed.text
        for metric, values in VALUES.items():
            await seed.reading(sensor["id"], metric, values[i])
        sensors.append(sensor)

    integration = await seed._integration(60)
    ids: dict[str, str] = {}
    for key, metric, rule_type, threshold, unit, name, sensor in (
        ("temp", "temperature_c", "threshold_high", 35, "degC", RULE_TEMP, sensors[0]),
        ("hum", "humidity_percent", "threshold_high", 70, "%", RULE_HUM, sensors[1]),
        ("air", "airflow_m3_s", "threshold_low", 0.25, "m3/s", RULE_AIR, sensors[2]),
    ):
        rule_id, alarm_id = str(uuid.uuid4()), str(uuid.uuid4())
        await db_session.execute(
            text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) "
                 "VALUES (:r, :i, :m, :t, :th, :u, true, :n)"),
            {"r": rule_id, "i": str(integration.id), "m": metric, "t": rule_type, "th": threshold, "u": unit, "n": name},
        )
        await db_session.execute(
            text("INSERT INTO alarm (id, rule_id, integration_id, managed_asset_id, subject_key, status, opened_at, last_value, details) "
                 "VALUES (:a, :r, :i, :s, :k, 'ACTIVE', now(), 41, '{}'::jsonb)"),
            {"a": alarm_id, "r": rule_id, "i": str(integration.id), "s": sensor["id"], "k": f"k-{key}"},
        )
        ids[key + "_alarm"], ids[key + "_rule"] = alarm_id, rule_id
    await db_session.commit()
    return {"admin": admin, "site": site, "racks": racks, "sensors": sensors, "ids": ids, "auth_headers": auth_headers, "db": db_session}


async def caller(client, world, kind: str) -> dict:
    admin = world["admin"]
    grant_all = {"site_id": world["site"]["site"], "rack_scope": "all", "rack_ids": []}
    grant_racks = {"site_id": world["site"]["site"], "rack_scope": "selected", "rack_ids": list(world["racks"])}
    full = [*BASE, "alarm:read"]
    if kind == "admin":
        return admin
    if kind == "viewer_denied":  # unrestricted role caller, explicit group deny
        headers = await world["auth_headers"]("Viewer")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        deny = await _group(client, admin, deny=["alarm:read"])
        assert (await client.put(f"/api/v1/groups/{deny}/members", json={"user_ids": [me["id"]]}, headers=admin)).status_code == 200
        return headers
    if kind in ("site_allowed", "site_missing", "site_denied", "site_deny_beats_allow"):
        allow = full if kind in ("site_allowed", "site_denied", "site_deny_beats_allow") else BASE
        groups = [await _group(client, admin, allow=allow, sites=[grant_all])]
        if kind in ("site_denied", "site_deny_beats_allow"):
            # the deny sits in a second group: a deny must beat an allow granted by any other group
            groups.append(await _group(client, admin, deny=["alarm:read"]))
        _, headers = await _group_user(client, admin, groups)
        return headers
    if kind in ("rack_allowed", "rack_missing", "rack_denied"):
        allow = full if kind in ("rack_allowed", "rack_denied") else BASE
        groups = [await _group(client, admin, allow=allow, sites=[grant_racks])]
        if kind == "rack_denied":
            groups.append(await _group(client, admin, deny=["alarm:read"]))
        _, headers = await _group_user(client, admin, groups)
        return headers
    raise AssertionError(kind)


NO_ALARM = ["viewer_denied", "site_missing", "site_denied", "site_deny_beats_allow", "rack_missing", "rack_denied"]
WITH_ALARM = ["admin", "site_allowed", "rack_allowed"]


async def surfaces(client, world, headers) -> dict[str, tuple[int, str]]:
    room = world["site"]["room"]
    paths = {
        "layout": f"{C}/rooms/{room}/layout",
        "capacity": f"{C}/rooms/{room}/capacity",
        "airflow": f"{C}/rooms/{room}/airflow",
        "exceptions": f"{C}/rooms/{room}/exceptions",
        "env_temperature": f"{C}/rooms/{room}/environment?metric=temperature_c",
        "env_humidity": f"{C}/rooms/{room}/environment?metric=humidity_percent",
        "env_airflow": f"{C}/rooms/{room}/environment?metric=airflow_m3_s",
        "heat_temperature": f"{C}/rooms/{room}/heat-map?metric=temperature_c",
        "heat_humidity": f"{C}/rooms/{room}/heat-map?metric=humidity_percent",
    }
    out = {}
    for name, path in paths.items():
        response = await client.get(path, headers=headers)
        out[name] = (response.status_code, response.text)
    return out


def forbidden_fragments(world) -> list[str]:
    return [
        "active_alarm_count", RULE_TEMP, RULE_HUM, RULE_AIR, "SECRET-RULE", *world["ids"].values(),
        '"threshold_high"', '"threshold_low"', '"rule_type"', "alarm_id", "rule_id",
    ]


@pytest.mark.parametrize("kind", NO_ALARM)
async def test_without_alarm_read_no_surface_exposes_any_alarm_derived_data(client, world, kind):
    headers = await caller(client, world, kind)
    results = await surfaces(client, world, headers)
    for name, (status, body) in results.items():
        if name == "capacity" and kind.startswith("rack_"):
            assert status == 404, name  # site-level capacity needs the whole site (unchanged #105 rule)
            continue
        assert status == 200, (kind, name, body)
        leaked = [f for f in forbidden_fragments(world) if f in body]
        assert not leaked, (kind, name, leaked)
    # the cooling / telemetry / spatial functionality their other permissions allow still works
    heat = json.loads(results["heat_temperature"][1])
    assert heat["state"] == "healthy" and heat["quality"]["sensor_count"] == 3 and heat["thresholds"] == [] and heat["thresholds_withheld"] is True
    humidity = json.loads(results["heat_humidity"][1])
    assert humidity["quality"]["sensor_count"] == 3 and humidity["thresholds"] == []
    env = json.loads(results["env_temperature"][1])
    assert env["counts"]["measured_fresh"] == 3 and all("active_alarm_count" not in p for p in env["points"])
    assert json.loads(results["exceptions"][1])["alarms_omitted"].startswith("alarm:read")
    assert not any(i["source"] == "alarm" for i in json.loads(results["exceptions"][1])["items"])


@pytest.mark.parametrize("kind", WITH_ALARM)
async def test_with_alarm_read_the_intended_alarm_metadata_remains_available(client, world, kind):
    headers = await caller(client, world, kind)
    results = await surfaces(client, world, headers)
    assert all(status == 200 or (name == "capacity" and kind.startswith("rack_")) for name, (status, _) in results.items()), results
    ids, sensors = world["ids"], world["sensors"]

    env_t = json.loads(results["env_temperature"][1])
    counts = {p["sensor_id"]: p["active_alarm_count"] for p in env_t["points"]}
    assert counts == {sensors[0]["id"]: 1, sensors[1]["id"]: 0, sensors[2]["id"]: 0}  # per-metric: only the temperature alarm counts here
    env_h = json.loads(results["env_humidity"][1])
    assert {p["sensor_id"]: p["active_alarm_count"] for p in env_h["points"]}[sensors[1]["id"]] == 1

    heat_t = json.loads(results["heat_temperature"][1])
    assert heat_t["thresholds_withheld"] is False
    assert heat_t["thresholds"] == [{"rule_type": "threshold_high", "threshold": 35.0, "unit": "degC", "name": RULE_TEMP}]
    assert {s["sensor_id"]: s["active_alarm_count"] for s in heat_t["sensors"]}[sensors[0]["id"]] == 1
    heat_h = json.loads(results["heat_humidity"][1])
    assert heat_h["thresholds"] == [{"rule_type": "threshold_high", "threshold": 70.0, "unit": "%", "name": RULE_HUM}]

    exceptions = json.loads(results["exceptions"][1])
    alarm_items = {i["alarm_id"]: i for i in exceptions["items"] if i["source"] == "alarm"}
    assert set(alarm_items) == {ids["temp_alarm"], ids["hum_alarm"], ids["air_alarm"]}
    assert "alarms_omitted" not in exceptions


async def test_alarm_data_is_not_even_queried_without_alarm_read(client, world, db_session):
    """Structural check: the alarm tables are not read at all, so nothing derived from them can be serialized."""
    headers = await caller(client, world, "site_missing")
    seen: list[str] = []
    engine = db_session.bind
    sync_engine = engine.sync_engine  # type: ignore[union-attr]

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        for name, (status, _) in (await surfaces(client, world, headers)).items():
            assert status == 200, name
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    touched = [s for s in seen if "FROM alarm" in s or "JOIN alarm" in s]
    assert touched == [], touched


async def test_explicit_deny_overrides_an_allow_from_another_group_but_leaves_other_permissions(client, world):
    headers = await caller(client, world, "site_deny_beats_allow")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert "alarm:read" not in me["permission_codes"]
    heat = await client.get(f"{C}/rooms/{world['site']['room']}/heat-map", headers=headers)
    assert heat.status_code == 200 and "active_alarm_count" not in heat.text
    assert (await client.get(f"{C}/sensors", headers=headers)).status_code == 200
