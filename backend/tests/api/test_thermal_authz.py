"""Issue #105: authorization and no-leak behaviour of every cooling / thermal surface.

Site-restricted callers hold no effective cooling permission (cooling:* is not scope-aware), so the HTTP layer refuses
them outright. The service layer is nevertheless scope-aware and is attacked directly here with restricted scopes, so
that making a permission scope-aware later cannot silently open a leak: interpolation, counts, quality metadata and
provenance ids must all be computed from the authorised sensor set only."""

import json
import uuid

import pytest

from app.application.access_control import AccessScope
from app.application.thermal import airflow, capacity, exceptions, heatmap
from app.application.thermal.environment import load_room_sensor_snapshot
from app.core.errors import NotFoundError
from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import C, TelemetrySeeder, make_sensor, make_unit, make_zone, relate, seed_plan
from tests.api.test_user_groups import _group, _group_user, _make_site

SERVICE_CALLS = {
    "heat_map": lambda db, room, scope: heatmap.build_heat_map(db, room_id=uuid.UUID(room), metric="temperature_c", scope=scope),
    "snapshot": lambda db, room, scope: load_room_sensor_snapshot(db, room_id=uuid.UUID(room), metric="temperature_c", scope=scope),
    "airflow": lambda db, room, scope: airflow.build_airflow(db, room_id=uuid.UUID(room), scope=scope),
    "capacity": lambda db, room, scope: capacity.build_room_capacity(db, room_id=uuid.UUID(room), scope=scope, can_read_power=True),
    "exceptions": lambda db, room, scope: exceptions.build_exceptions(db, room_id=uuid.UUID(room), scope=scope, can_read_power=True),
}


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    await seed_plan(db_session, a["room"])
    await seed_plan(db_session, b["room"])
    return {"admin": admin, "a": a, "b": b, "auth_headers": auth_headers, "seed": TelemetrySeeder(db_session), "db": db_session}


def full_scope(*site_ids: str) -> AccessScope:
    ids = frozenset(uuid.UUID(s) for s in site_ids)
    return AccessScope(unrestricted=False, site_ids=ids, full_site_ids=ids)


def rack_scope(site_id: str, *rack_ids: str) -> AccessScope:
    site = uuid.UUID(site_id)
    racks = frozenset(uuid.UUID(r) for r in rack_ids)
    return AccessScope(unrestricted=False, site_ids=frozenset({site}), full_site_ids=frozenset(), rack_ids=racks, selected_racks_by_site={site: racks})


# --------------------------------------------------------------------------------- HTTP: permissions and refusal


READ_ENDPOINTS = [
    ("/units", "cooling:read"), ("/groups", "cooling:read"), ("/sensors", "cooling:read"), ("/zones?room_id={room}", "cooling:read"),
    ("/rooms/{room}/layout", "cooling:read"), ("/rooms/{room}/environment", "telemetry:read"), ("/rooms/{room}/heat-map", "telemetry:read"),
    ("/rooms/{room}/airflow", "telemetry:read"), ("/rooms/{room}/capacity", "cooling:read"), ("/rooms/{room}/exceptions", "telemetry:read"),
]


async def deny(client, world, headers_role: str, *codes: str) -> dict:
    headers = await world["auth_headers"](headers_role)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    group = (await client.post("/api/v1/groups", json={"name": f"deny-{uuid.uuid4().hex[:6]}"}, headers=world["admin"])).json()["id"]
    assert (await client.put(f"/api/v1/groups/{group}/permissions", json={"allow": [], "deny": list(codes)}, headers=world["admin"])).status_code == 200
    assert (await client.put(f"/api/v1/groups/{group}/members", json={"user_ids": [me["id"]]}, headers=world["admin"])).status_code == 200
    return headers


@pytest.mark.parametrize("role", ["Viewer", "Operator"])
async def test_read_only_roles_can_read_but_never_write(client, world, role):
    headers = await world["auth_headers"](role)
    room = world["a"]["room"]
    for path, _ in READ_ENDPOINTS:
        response = await client.get(C + path.format(room=room), headers=headers)
        assert response.status_code == 200, (path, response.text)
    unit = await make_unit(client, world["admin"], world["a"]["site"])
    sensor = await make_sensor(client, world["admin"], world["a"]["site"])
    zone = await make_zone(client, world["admin"], room)
    group = (await client.post(f"{C}/groups", json={"site_id": world["a"]["site"], "name": "g"}, headers=world["admin"])).json()
    writes = [
        ("post", "/units", {"unit_kind": "crah", "asset_tag": "X", "site_id": world["a"]["site"], "name": "x"}),
        ("patch", f"/units/{unit['id']}", {"name": "x"}), ("put", f"/units/{unit['id']}/placement", {"room_id": room}),
        ("delete", f"/units/{unit['id']}/placement", None), ("post", f"/units/{unit['id']}/retire", None),
        ("post", "/sensors", {"asset_tag": "Y", "site_id": world["a"]["site"], "name": "y", "sensor_kind": "temperature"}),
        ("patch", f"/sensors/{sensor['id']}", {"name": "y"}), ("put", f"/sensors/{sensor['id']}/placement", {"room_id": room}),
        ("delete", f"/sensors/{sensor['id']}/placement", None), ("post", "/zones", {"room_id": room, "name": "z", "zone_kind": "served_zone"}),
        ("patch", f"/zones/{zone['id']}", {"name": "z2"}), ("post", f"/zones/{zone['id']}/retire", None),
        ("post", f"/zones/{zone['id']}/containment-elements", {"element_kind": "boundary", "x1_mm": 0, "y1_mm": 0, "x2_mm": 1, "y2_mm": 0}),
        ("post", "/relations", {"cooling_unit_id": unit["id"], "thermal_zone_id": zone["id"], "relation_kind": "serves"}),
        ("delete", f"/relations/{uuid.uuid4()}", None), ("post", "/groups", {"site_id": world["a"]["site"], "name": "g2"}),
        ("patch", f"/groups/{group['id']}", {"name": "g3"}), ("post", f"/groups/{group['id']}/retire", None),
    ]
    for method, path, body in writes:
        response = await getattr(client, method)(C + path, headers={**headers, "If-Match": "1"}, **({"json": body} if body is not None else {}))
        assert response.status_code == 403, (method, path, response.status_code, response.text)
        assert "cooling:manage" in response.text


@pytest.mark.parametrize(("denied", "blocked"), [
    ("cooling:read", {"/units", "/groups", "/sensors", "/zones?room_id={room}", "/rooms/{room}/layout", "/rooms/{room}/environment", "/rooms/{room}/heat-map",
                      "/rooms/{room}/airflow", "/rooms/{room}/capacity", "/rooms/{room}/exceptions"}),
    ("spatial:read", {"/rooms/{room}/layout", "/rooms/{room}/environment", "/rooms/{room}/heat-map", "/rooms/{room}/airflow"}),
    ("telemetry:read", {"/rooms/{room}/environment", "/rooms/{room}/heat-map", "/rooms/{room}/airflow", "/rooms/{room}/exceptions"}),
])
async def test_each_view_requires_exactly_its_literal_permissions(client, world, denied, blocked):
    headers = await deny(client, world, "Viewer", denied)
    room = world["a"]["room"]
    for path, _ in READ_ENDPOINTS:
        response = await client.get(C + path.format(room=room), headers=headers)
        expected = 403 if path in blocked else 200
        assert response.status_code == expected, (denied, path, response.status_code, response.text)


async def test_capacity_hides_thermal_load_without_power_read(client, world, monkeypatch):
    admin, room = world["admin"], world["a"]["room"]
    zone = await make_zone(client, admin, room)
    unit = await make_unit(client, admin, world["a"]["site"], rated_cooling_capacity_kw=40)
    await relate(client, admin, unit["id"], zone["id"])
    headers = await deny(client, world, "Viewer", "power:read")
    z = (await client.get(f"{C}/rooms/{room}/capacity", headers=headers)).json()["zones"][0]
    assert z["thermal_load"]["state"] == "not_permitted" and z["thermal_load"]["thermal_kw"] is None and z["headroom_kw"] is None
    assert z["installed_rated_kw"] == 40.0  # the cooling side is still visible


async def test_site_restricted_user_is_refused_everywhere_even_with_every_grant(client, world):
    admin = world["admin"]
    codes = ["cooling:read", "cooling:manage", "spatial:read", "telemetry:read", "rack:read", "power:read", "alarm:read"]
    group = await _group(client, admin, allow=codes, sites=[{"site_id": world["a"]["site"], "rack_scope": "all", "rack_ids": []}])
    _, headers = await _group_user(client, admin, [group])
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    effective = set(me["permission_codes"])
    assert not effective & {"cooling:read", "cooling:manage", "spatial:read", "telemetry:read", "power:read", "alarm:read"}  # none is site-scope-aware
    assert "rack:read" in effective
    sensor = await make_sensor(client, admin, world["b"]["site"], world["b"]["room"], 1000, 1000)
    unit = await make_unit(client, admin, world["b"]["site"])
    for room in (world["a"]["room"], world["b"]["room"]):
        for path, _ in READ_ENDPOINTS:
            response = await client.get(C + path.format(room=room), headers=headers)
            assert response.status_code == 403, (path, response.status_code)
    for path in (f"/sensors/{sensor['id']}", f"/units/{unit['id']}", f"/units/{uuid.uuid4()}"):
        assert (await client.get(C + path, headers=headers)).status_code == 403  # same answer for hidden, real and unknown ids
    assert (await client.post(f"{C}/units", json={"unit_kind": "crah", "asset_tag": "H", "site_id": world["a"]["site"], "name": "h"}, headers=headers)).status_code == 403


async def test_unknown_ids_are_404_not_500(client, world):
    admin = world["admin"]
    for path in (f"/units/{uuid.uuid4()}", f"/sensors/{uuid.uuid4()}", f"/zones/{uuid.uuid4()}", f"/groups/{uuid.uuid4()}", f"/rooms/{uuid.uuid4()}/capacity", f"/rooms/{uuid.uuid4()}/heat-map"):
        assert (await client.get(C + path, headers=admin)).status_code == 404, path


# --------------------------------------------------------------------------------- service layer: hostile scopes


async def build_rack_world(client, world, hidden_value=999.0):
    """Site A room with 4 racks (3 readable + 1 hidden), one rack-mounted sensor in each, plus a floor sensor."""
    admin, room, site = world["admin"], world["a"]["room"], world["a"]["site"]
    racks, sensors = [], []
    for i, (x, y) in enumerate(((500, 500), (4500, 500), (500, 2500), (4500, 2500))):
        rack = await create_rack(client, admin, world["auth_headers"], room_id=room, x_mm=x, y_mm=y, rotation_deg=0, name=f"R{i}")
        racks.append(rack["id"])
        sensor = await make_sensor(client, admin, site, name=f"RS{i}")
        placed = await client.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": room, "placement_type": "rack_mounted", "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"}, headers=admin)
        assert placed.status_code == 200, placed.text
        await world["seed"].reading(sensor["id"], "temperature_c", hidden_value if i == 3 else 20.0 + i)
        sensors.append(sensor)
    floor = await make_sensor(client, admin, site, room, 3000, 2000, name="FloorSensor")
    await world["seed"].reading(floor["id"], "temperature_c", 500.0)
    return racks, sensors, floor


@pytest.mark.parametrize("name", sorted(SERVICE_CALLS))
async def test_every_service_entry_point_treats_a_hidden_room_as_missing(client, world, name):
    for scope in (full_scope(world["a"]["site"]), full_scope()):
        with pytest.raises(NotFoundError):
            await SERVICE_CALLS[name](world["db"], world["b"]["room"], scope)
    ok = await SERVICE_CALLS[name](world["db"], world["a"]["room"], full_scope(world["a"]["site"]))
    assert ok is not None


async def test_site_scoped_map_only_ever_contains_that_sites_sensors(client, world):
    admin = world["admin"]
    mine = [await make_sensor(client, admin, world["a"]["site"], world["a"]["room"], x, y, name=f"A{x}") for x, y in ((1000, 1000), (5000, 1000), (3000, 3000))]
    for s, v in zip(mine, (20.0, 22.0, 24.0), strict=True):
        await world["seed"].reading(s["id"], "temperature_c", v)
    theirs = [await make_sensor(client, admin, world["b"]["site"], world["b"]["room"], x, y, name=f"B{x}") for x, y in ((1000, 1000), (5000, 1000), (3000, 3000))]
    for s in theirs:
        await world["seed"].reading(s["id"], "temperature_c", 999.0)
    body = await heatmap.build_heat_map(world["db"], room_id=uuid.UUID(world["a"]["room"]), metric="temperature_c", scope=full_scope(world["a"]["site"]))
    blob = json.dumps(body)
    assert body["state"] == "healthy" and body["grid"]["max"] <= 24.0 and body["quality"]["sensor_count"] == 3
    assert not any(s["id"] in blob or s["name"] in blob for s in theirs)


async def test_partial_rack_scope_cannot_reveal_hidden_sensors_through_interpolation_counts_or_provenance(client, world):
    racks, sensors, floor = await build_rack_world(client, world)
    room = uuid.UUID(world["a"]["room"])
    visible = rack_scope(world["a"]["site"], *racks[:3])
    hidden_world = await heatmap.build_heat_map(world["db"], room_id=room, metric="temperature_c", scope=visible)
    # The hidden rack's 999 degC sensor and the rack-less floor sensor (500 degC) must not exist for this caller.
    assert hidden_world["quality"]["sensor_count"] == 3 and hidden_world["state"] == "healthy"
    assert hidden_world["grid"]["max"] <= 22.0 + 1e-9
    blob = json.dumps(hidden_world)
    for secret in (sensors[3]["id"], sensors[3]["name"], sensors[3]["asset_tag"], floor["id"], floor["name"], racks[3], "999", "500.0"):
        assert secret not in blob, secret
    # Equivalence: removing the hidden sensor entirely gives a byte-identical field.
    from sqlalchemy import text

    await world["db"].execute(text("DELETE FROM telemetry_reading WHERE managed_asset_id IN (:a, :b)"), {"a": sensors[3]["id"], "b": floor["id"]})
    await world["db"].commit()
    without = await heatmap.build_heat_map(world["db"], room_id=room, metric="temperature_c", scope=visible)
    assert without["grid"] == hidden_world["grid"] and without["quality"]["fresh_count"] == hidden_world["quality"]["fresh_count"]
    full = await heatmap.build_heat_map(world["db"], room_id=room, metric="temperature_c", scope=full_scope(world["a"]["site"]))
    assert full["quality"]["sensor_count"] == 5  # a full-site scope does see floor sensors and every rack


async def test_hidden_sensors_do_not_appear_in_exceptions_airflow_or_snapshot(client, world):
    racks, sensors, floor = await build_rack_world(client, world)
    room = uuid.UUID(world["a"]["room"])
    from sqlalchemy import text

    await world["db"].execute(text("DELETE FROM telemetry_reading WHERE managed_asset_id = :a"), {"a": sensors[3]["id"]})
    await world["db"].commit()
    visible = rack_scope(world["a"]["site"], *racks[:3])
    body = await exceptions.build_exceptions(world["db"], room_id=room, scope=visible, can_read_power=False)
    assert sensors[3]["id"] not in json.dumps(body)  # its missing-data exception exists only for callers who can see it
    full = await exceptions.build_exceptions(world["db"], room_id=room, scope=full_scope(world["a"]["site"]), can_read_power=False)
    assert ("missing_sensor_data", sensors[3]["id"]) in {(i["type"], i["subject_id"]) for i in full["items"]}
    snap = await load_room_sensor_snapshot(world["db"], room_id=room, metric="temperature_c", scope=visible)
    assert {str(p.sensor_id) for p in snap.points} == {s["id"] for s in sensors[:3]}


async def test_capacity_hides_hidden_units_and_withholds_load_for_restricted_scopes(client, world):
    admin = world["admin"]
    zone = await make_zone(client, admin, world["a"]["room"])
    unit = await make_unit(client, admin, world["a"]["site"], rated_cooling_capacity_kw=60)
    await relate(client, admin, unit["id"], zone["id"])
    room = uuid.UUID(world["a"]["room"])
    own = await capacity.build_room_capacity(world["db"], room_id=room, scope=full_scope(world["a"]["site"]), can_read_power=True)
    z = own["zones"][0]
    assert z["installed_rated_kw"] == 60.0 and z["thermal_load"]["state"] == "withheld_by_scope" and z["headroom_kw"] is None
    foreign_site = full_scope(world["b"]["site"])
    with pytest.raises(NotFoundError):
        await capacity.build_room_capacity(world["db"], room_id=room, scope=foreign_site, can_read_power=True)
    other_zone = await make_zone(client, admin, world["b"]["room"])
    other_unit = await make_unit(client, admin, world["b"]["site"], rated_cooling_capacity_kw=900)
    await relate(client, admin, other_unit["id"], other_zone["id"])
    blob = json.dumps(own)
    assert other_unit["id"] not in blob and "900" not in blob  # site B capacity is not folded into A's totals
    sites = await capacity.groups_summary(world["db"], uuid.UUID(world["b"]["site"]), full_scope(world["a"]["site"]))
    assert sites == []


async def test_http_responses_never_contain_ids_from_another_site(client, world):
    admin = world["admin"]
    sensor_b = await make_sensor(client, admin, world["b"]["site"], world["b"]["room"], 1000, 1000, name="OnlyInB")
    await world["seed"].reading(sensor_b["id"], "temperature_c", 44.0)
    for path in ("/rooms/{room}/layout", "/rooms/{room}/environment", "/rooms/{room}/heat-map", "/rooms/{room}/airflow", "/rooms/{room}/capacity", "/rooms/{room}/exceptions"):
        text_a = (await client.get(C + path.format(room=world["a"]["room"]), headers=admin)).text
        assert sensor_b["id"] not in text_a and "OnlyInB" not in text_a, path
    listed = (await client.get(f"{C}/sensors", params={"room_id": world["a"]["room"]}, headers=admin)).json()
    assert listed["total"] == 0
