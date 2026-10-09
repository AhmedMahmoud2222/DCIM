"""Issue #105: site- and rack-scoped users through the real HTTP endpoints.

Authorization filtering happens before interpolation and aggregation, so a restricted caller can neither read nor
mathematically infer a hidden sensor from a heat map, quality count, min/max, capacity total or provenance id."""

import json
import uuid

import pytest
from sqlalchemy import text

from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import (
    C,
    TelemetrySeeder,
    make_group,
    make_sensor,
    make_unit,
    make_zone,
    relate,
    seed_plan,
)
from tests.api.test_user_groups import _group, _group_user, _make_site

CODES = ["cooling:read", "cooling:manage", "spatial:read", "telemetry:read", "power:read", "alarm:read", "rack:read"]


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    await seed_plan(db_session, a["room"])
    await seed_plan(db_session, b["room"])
    return {"admin": admin, "a": a, "b": b, "auth_headers": auth_headers, "seed": TelemetrySeeder(db_session), "db": db_session}


async def scoped_user(client, world, *, rack_ids=(), allow=CODES, deny=()):
    grant = {"site_id": world["a"]["site"], "rack_scope": "selected" if rack_ids else "all", "rack_ids": list(rack_ids)}
    group = await _group(client, world["admin"], allow=allow, deny=deny, sites=[grant])
    _, headers = await _group_user(client, world["admin"], [group])
    return headers


async def site_b_assets(client, world):
    admin = world["admin"]
    sensors = []
    for i, (x, y) in enumerate(((1000, 1000), (5000, 1000), (3000, 3000))):
        s = await make_sensor(client, admin, world["b"]["site"], world["b"]["room"], x, y, name=f"B-secret-{i}")
        await world["seed"].reading(s["id"], "temperature_c", 999.0)
        sensors.append(s)
    zone = await make_zone(client, admin, world["b"]["room"], name="B-zone")
    unit = await make_unit(client, admin, world["b"]["site"], rated_cooling_capacity_kw=900, name="B-unit")
    await relate(client, admin, unit["id"], zone["id"])
    group = await make_group(client, admin, world["b"]["site"], "B-group")
    return {"sensors": sensors, "zone": zone, "unit": unit, "group": group}


def blob_has(blob: str, *secrets: str) -> list[str]:
    return [s for s in secrets if s in blob]


async def test_site_a_user_reads_site_a_thermal_views_and_never_site_b(client, world):
    admin, room_a = world["admin"], world["a"]["room"]
    mine = [await make_sensor(client, admin, world["a"]["site"], room_a, x, y, name=f"A{x}") for x, y in ((1000, 1000), (5000, 1000), (3000, 3000))]
    for s, v in zip(mine, (20.0, 22.0, 24.0), strict=True):
        await world["seed"].reading(s["id"], "temperature_c", v)
    theirs = await site_b_assets(client, world)
    headers = await scoped_user(client, world)

    for path in ("layout", "environment", "heat-map", "airflow", "capacity", "exceptions"):
        assert (await client.get(f"{C}/rooms/{room_a}/{path}", headers=headers)).status_code == 200, path
        hidden = await client.get(f"{C}/rooms/{world['b']['room']}/{path}", headers=headers)
        assert hidden.status_code == 404, path
        assert "B-secret" not in hidden.text  # the 404 only echoes the id the caller supplied
    body = (await client.get(f"{C}/rooms/{room_a}/heat-map", headers=headers)).json()
    assert body["state"] == "healthy" and body["quality"]["sensor_count"] == 3 and body["grid"]["max"] <= 24.0
    assert body["sensors"][0]["source"] is None  # integration:read was not granted
    ids = [x["id"] for x in theirs["sensors"]] + [theirs["unit"]["id"], theirs["zone"]["id"], theirs["group"]["id"]]
    for path in ("layout", "environment", "heat-map", "airflow", "capacity", "exceptions"):
        text_a = (await client.get(f"{C}/rooms/{room_a}/{path}", headers=headers)).text
        assert not blob_has(text_a, "B-secret", "B-unit", "B-zone", "999.0", *ids), path


async def test_direct_id_access_to_hidden_site_resources_is_404_without_metadata(client, world):
    theirs = await site_b_assets(client, world)
    headers = await scoped_user(client, world)
    unknown = str(uuid.uuid4())
    probes = [f"/sensors/{theirs['sensors'][0]['id']}", f"/units/{theirs['unit']['id']}", f"/zones/{theirs['zone']['id']}", f"/groups/{theirs['group']['id']}"]
    unknown_probes = [f"/sensors/{unknown}", f"/units/{unknown}", f"/zones/{unknown}", f"/groups/{unknown}"]
    for hidden, missing in zip(probes, unknown_probes, strict=True):
        h = await client.get(C + hidden, headers=headers)
        m = await client.get(C + missing, headers=headers)
        assert h.status_code == m.status_code == 404
        assert h.json()["title"] == m.json()["title"] and "B-" not in h.text  # same answer for hidden and non-existent ids
    for path, body in (
        (f"/sensors/{theirs['sensors'][0]['id']}", {"name": "pwned"}), (f"/units/{theirs['unit']['id']}", {"name": "pwned"}), (f"/zones/{theirs['zone']['id']}", {"name": "pwned"}),
    ):
        assert (await client.patch(C + path, json=body, headers={**headers, "If-Match": "1"})).status_code == 404
    assert (await client.put(f"{C}/sensors/{theirs['sensors'][0]['id']}/placement", json={"room_id": world["a"]["room"]}, headers=headers)).status_code == 404
    assert (await client.post(f"{C}/relations", json={"cooling_unit_id": theirs["unit"]["id"], "thermal_zone_id": theirs["zone"]["id"], "relation_kind": "serves"}, headers=headers)).status_code == 404
    names = {r["name"] for r in (await client.get(f"{C}/sensors", headers=headers)).json()["items"]}
    assert not any(n.startswith("B-") for n in names)
    for listing in ("/sensors", "/units", "/groups"):
        assert (await client.get(C + listing, headers=headers)).json()["total"] == 0, listing
    assert (await client.get(f"{C}/zones", params={"room_id": world["b"]["room"]}, headers=headers)).status_code == 404
    assert (await client.get(f"{C}/sensors", params={"site_id": world["b"]["site"]}, headers=headers)).json()["total"] == 0
    still = await world["db"].execute(text("SELECT name FROM thermal_zone WHERE name = 'B-zone'"))
    assert still.scalar_one() == "B-zone"  # nothing was changed


async def test_site_a_user_with_manage_can_configure_site_a_only(client, world):
    headers = await scoped_user(client, world)
    created = await client.post(f"{C}/units", json={"unit_kind": "crah", "asset_tag": f"U-{uuid.uuid4().hex[:6]}", "site_id": world["a"]["site"], "name": "Mine"}, headers=headers)
    assert created.status_code == 201, created.text
    foreign = await client.post(f"{C}/units", json={"unit_kind": "crah", "asset_tag": f"U-{uuid.uuid4().hex[:6]}", "site_id": world["b"]["site"], "name": "Theirs"}, headers=headers)
    assert foreign.status_code == 422
    assert (await client.post(f"{C}/zones", json={"room_id": world["b"]["room"], "name": "z", "zone_kind": "served_zone"}, headers=headers)).status_code == 404
    assert (await client.post(f"{C}/zones", json={"room_id": world["a"]["room"], "name": "z", "zone_kind": "served_zone"}, headers=headers)).status_code == 201
    assert (await client.get(f"{C}/units", headers=headers)).json()["total"] == 1


async def test_explicit_denies_and_missing_grants_still_block(client, world):
    room = world["a"]["room"]
    no_telemetry = await scoped_user(client, world, allow=[c for c in CODES if c != "telemetry:read"], deny=["telemetry:read"])
    assert (await client.get(f"{C}/rooms/{room}/heat-map", headers=no_telemetry)).status_code == 403
    assert (await client.get(f"{C}/rooms/{room}/capacity", headers=no_telemetry)).status_code == 200  # capacity does not need telemetry
    no_spatial = await scoped_user(client, world, allow=[c for c in CODES if c != "spatial:read"])
    assert (await client.get(f"{C}/rooms/{room}/layout", headers=no_spatial)).status_code == 403
    no_cooling = await scoped_user(client, world, allow=["spatial:read", "telemetry:read", "rack:read"])
    assert (await client.get(f"{C}/rooms/{room}/heat-map", headers=no_cooling)).status_code == 403
    assert (await client.get(f"{C}/units", headers=no_cooling)).status_code == 403


async def rack_world(client, world):
    admin, room, site = world["admin"], world["a"]["room"], world["a"]["site"]
    racks, sensors = [], []
    for i, (x, y) in enumerate(((500, 500), (4500, 500), (500, 2500), (4500, 2500))):
        rack = await create_rack(client, admin, world["auth_headers"], room_id=room, x_mm=x, y_mm=y, rotation_deg=0, name=f"R{i}")
        racks.append(rack["id"])
        sensor = await make_sensor(client, admin, site, name=f"RS{i}")
        placed = await client.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": room, "placement_type": "rack_mounted", "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"}, headers=admin)
        assert placed.status_code == 200, placed.text
        await world["seed"].reading(sensor["id"], "temperature_c", 999.0 if i == 3 else 20.0 + i)
        sensors.append(sensor)
    floor = await make_sensor(client, admin, site, room, 3000, 2000, name="FloorSensor")
    await world["seed"].reading(floor["id"], "temperature_c", 500.0)
    unit = await make_unit(client, admin, site, rated_cooling_capacity_kw=70, name="SiteUnit", operating_status="online")
    zone = await make_zone(client, admin, room, name="SiteZone")
    await relate(client, admin, unit["id"], zone["id"])
    return racks, sensors, floor, unit, zone


async def test_partial_rack_scope_hidden_sensors_never_enter_the_map_counts_or_provenance(client, world):
    racks, sensors, floor, unit, zone = await rack_world(client, world)
    room = world["a"]["room"]
    headers = await scoped_user(client, world, rack_ids=racks[:3])
    body = (await client.get(f"{C}/rooms/{room}/heat-map", headers=headers)).json()
    assert body["state"] == "healthy" and body["quality"]["sensor_count"] == 3 and body["quality"]["fresh_count"] == 3
    assert body["grid"]["min"] >= 20.0 and body["grid"]["max"] <= 22.0 + 1e-9  # neither 999 nor 500 degC leaks into min/max
    blob = json.dumps(body)
    assert not blob_has(blob, sensors[3]["id"], sensors[3]["name"], sensors[3]["asset_tag"], floor["id"], "FloorSensor", racks[3], "999.0", "500.0")
    # equivalence: with the hidden readings removed entirely the visible map is byte-identical
    await world["db"].execute(text("DELETE FROM telemetry_reading WHERE managed_asset_id IN (:a, :b)"), {"a": sensors[3]["id"], "b": floor["id"]})
    await world["db"].commit()
    again = (await client.get(f"{C}/rooms/{room}/heat-map", headers=headers)).json()
    assert again["grid"] == body["grid"] and again["quality"]["sensor_count"] == 3 and again["source_set"]["contributing_sensor_ids"] == body["source_set"]["contributing_sensor_ids"]
    # an unrestricted caller still sees everything
    full = (await client.get(f"{C}/rooms/{room}/heat-map", headers=world["admin"])).json()
    assert full["quality"]["sensor_count"] == 5


async def test_partial_rack_scope_hides_sensor_resources_and_site_level_configuration(client, world):
    racks, sensors, floor, unit, zone = await rack_world(client, world)
    room = world["a"]["room"]
    headers = await scoped_user(client, world, rack_ids=racks[:3])
    listed = (await client.get(f"{C}/sensors", headers=headers)).json()
    assert {s["name"] for s in listed["items"]} == {"RS0", "RS1", "RS2"} and listed["total"] == 3
    assert (await client.get(f"{C}/sensors/{sensors[3]['id']}", headers=headers)).status_code == 404
    assert (await client.get(f"{C}/sensors/{floor['id']}", headers=headers)).status_code == 404  # rack-less asset: not theirs
    assert (await client.get(f"{C}/sensors/{sensors[0]['id']}", headers=headers)).status_code == 200
    assert (await client.patch(f"{C}/sensors/{sensors[0]['id']}", json={"name": "x"}, headers={**headers, "If-Match": "1"})).status_code == 404  # changing needs the whole site
    for path in ("/units", "/groups"):
        assert (await client.get(C + path, headers=headers)).json()["total"] == 0, path
    assert (await client.get(f"{C}/units/{unit['id']}", headers=headers)).status_code == 404
    assert (await client.get(f"{C}/zones", params={"room_id": room}, headers=headers)).status_code == 404
    assert (await client.get(f"{C}/rooms/{room}/capacity", headers=headers)).status_code == 404  # site-level capacity needs the whole site
    layout = (await client.get(f"{C}/rooms/{room}/layout", headers=headers)).json()
    assert layout["zones"] == [] and layout["cooling_units"] == [] and layout["relations"] == []
    assert {s["name"] for s in layout["sensors"]} == {"RS0", "RS1", "RS2"}
    exc = (await client.get(f"{C}/rooms/{room}/exceptions", headers=headers)).text
    assert not blob_has(exc, sensors[3]["id"], floor["id"], unit["id"], zone["id"], "SiteZone")
    airflow = (await client.get(f"{C}/rooms/{room}/airflow", headers=headers)).text
    assert unit["id"] not in airflow


async def test_hidden_cooling_units_do_not_change_visible_capacity_totals(client, world):
    admin = world["admin"]
    zone = await make_zone(client, admin, world["a"]["room"], name="Hall")
    mine = await make_unit(client, admin, world["a"]["site"], rated_cooling_capacity_kw=60, operating_status="online", name="Mine")
    await relate(client, admin, mine["id"], zone["id"])
    await site_b_assets(client, world)
    headers = await scoped_user(client, world)
    z = (await client.get(f"{C}/rooms/{world['a']['room']}/capacity", headers=headers)).json()["zones"][0]
    assert z["installed_rated_kw"] == 60.0 and z["unit_count"] == 1  # site B's 900 kW is not part of any total
    assert z["thermal_load"]["state"] in ("known", "unknown")  # the whole-site caller may see the load; never "withheld"
    assert "900.0" not in json.dumps(z)
