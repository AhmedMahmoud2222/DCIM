"""Issue #104: dimensionally accurate spatial view, incomplete-data states, operational overlays and
authorization of every new surface."""

import json

import pytest

from app.application.catalog_documents.extraction.sandbox import landlock_available
from tests import spatial_fixtures as fx
from tests.api._spatial_helpers import (
    FP,
    accept,
    by_label,
    calibrate_declared,
    candidates,
    import_file,
    new_floor_plan,
    place_rack,
    room_with_plan,
)

pytestmark = pytest.mark.skipif(not landlock_available(), reason="Landlock is required for the parser sandbox")

VIEW = "/api/v1/spatial/rooms/{room}/view"


async def view(client, headers, room_id):
    resp = await client.get(VIEW.format(room=room_id), headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def activate(client, headers, plan_id):
    fp = (await client.get(f"{FP}/{plan_id}", headers=headers)).json()
    resp = await client.post(f"{FP}/{plan_id}/activate", headers={**headers, "If-Match": str(fp["version"])})
    assert resp.status_code == 200, resp.text


async def set_boundary(client, headers, plan_id, w=6000, h=4000):
    fp = (await client.get(f"{FP}/{plan_id}", headers=headers)).json()
    resp = await client.put(f"{FP}/{plan_id}/room-boundary", json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": w, "height_mm": h}, headers={**headers, "If-Match": str(fp["version"])})
    assert resp.status_code == 200, resp.text


async def test_view_reports_catalog_dimensions_and_never_invents_a_position(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    placed = await place_rack(client, headers, auth_headers, room_id, 1000, 2000, name="Placed")
    from tests.api._phase2_helpers import create_rack

    unpositioned = await create_rack(client, headers, auth_headers, room_id=room_id, name="NoXY")
    body = await view(client, headers, room_id)
    by_name = {r["name"]: r for r in body["racks"]}
    assert (by_name["Placed"]["width_mm"], by_name["Placed"]["depth_mm"], by_name["Placed"]["height_u"], by_name["Placed"]["height_mm"]) == (600, 1000, 42, 1867)
    assert by_name["Placed"]["position_state"] == "placed" and by_name["Placed"]["placement_version"] >= 1
    assert by_name["NoXY"]["position_state"] == "missing" and by_name["NoXY"]["x_mm"] is None and by_name["NoXY"]["y_mm"] is None
    assert body["rack_unit_mm"] == 44.45
    assert body["layout_state"] == "incomplete"
    assert {"no_active_floor_plan", "rack_position_missing:1"} <= set(body["incomplete_reasons"])
    assert placed["id"] != unpositioned["id"]


async def test_layout_becomes_validated_only_with_calibration_boundary_and_complete_racks(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    await place_rack(client, headers, auth_headers, room_id, 1000, 2000, name="Rack A")
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "r.dxf", "dxf")
    await activate(client, headers, plan["id"])
    first = await view(client, headers, room_id)
    assert first["layout_state"] == "incomplete" and set(first["incomplete_reasons"]) == {"no_calibration", "no_room_boundary"}
    await calibrate_declared(client, headers, plan["id"], job["id"])
    await set_boundary(client, headers, plan["id"])
    done = await view(client, headers, room_id)
    assert done["layout_state"] == "validated" and done["incomplete_reasons"] == []
    assert done["calibration"]["method"] == "declared_units" and done["calibration"]["error_bound_mm"] == 0.5
    assert done["boundary"]["object_type"] == "room_outline" and (done["room_width_mm"], done["room_height_mm"]) == (6000, 4000)


async def test_accepted_geometry_reaches_the_view_with_provenance_and_polygons(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    body = fx.rack_row_dxf(1) + b""
    entities = fx.lwpoly("P1", "WALLS", [(0, 0), (6000, 0), (6000, 2000), (3000, 4000), (0, 4000)]) + fx.rect_poly("c", "COLUMNS", 100, 100, 300, 300)
    job = await import_file(client, headers, plan["id"], fx.dxf(entities, layers=("WALLS", "COLUMNS")), "w.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    items = await candidates(client, headers, job["id"])
    polygon = next(c for c in items if c["raw_geometry"]["shape_type"] == "polygon")
    column = next(c for c in items if c["raw_geometry"]["shape_type"] == "rect")
    assert polygon["canonical"]["geometry_data"]["points"][0] == [0, 4000] and polygon["canonical"]["geometry_type"] == "polygon"
    assert (await accept(client, headers, job["id"], polygon, {"object_type": "room_outline"})).status_code == 200
    assert (await accept(client, headers, job["id"], column, {"object_type": "column"})).status_code == 200
    await activate(client, headers, plan["id"])
    seen = await view(client, headers, room_id)
    assert {o["object_type"] for o in seen["objects"]} == {"room_outline", "column"}
    assert seen["boundary"]["geometry_data"]["points"][2] == [6000, 2000]
    assert all(o["provenance"]["job_id"] == job["id"] for o in seen["objects"]) and body


async def test_floor_standing_equipment_gets_position_from_its_linked_shape_and_flags_missing_dimensions(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    admin = await auth_headers("Administrator")
    model = (await client.post("/api/v1/equipment-models", json={"manufacturer": "Acme", "model_name": "UPS-X"}, headers=admin)).json()
    full = (await client.post(f"/api/v1/equipment-models/{model['id']}/revisions", json={"height_u": 20, "width_mm": 800, "depth_mm": 900}, headers=admin)).json()
    bare = (await client.post(f"/api/v1/equipment-models/{model['id']}/revisions", json={}, headers=admin)).json()
    made = {}
    for key, revision in (("full", full), ("bare", bare)):
        eq = await client.post("/api/v1/equipment", json={"asset_tag": f"FL-{key}", "model_revision_id": revision["id"], "hostname": f"floor-{key}"}, headers=headers)
        assert eq.status_code == 201, eq.text
        mv = await client.post(f"/api/v1/equipment/{eq.json()['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id, "rotation_deg": 90}, headers=headers)
        assert mv.status_code == 200, mv.text
        made[key] = eq.json()

    before = {e["hostname"]: e for e in (await view(client, headers, room_id))["equipment"]}
    assert before["floor-full"]["position_state"] == "missing" and before["floor-full"]["x_mm"] is None
    assert before["floor-full"]["dimensions_state"] == "complete" and before["floor-full"]["height_mm"] == 889
    assert before["floor-bare"]["dimensions_state"] == "missing" and before["floor-bare"]["width_mm"] is None

    job = await import_file(client, headers, plan["id"], fx.dxf(fx.rect_poly("e1", "EQUIP", 4000, 500, 800, 900)), "e.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"], origin=[0, 4000])
    cand = (await candidates(client, headers, job["id"]))[0]
    resp = await accept(client, headers, job["id"], cand, {"object_type": "equipment", "matched_asset_id": made["full"]["id"], "link_placement": True})
    assert resp.status_code == 200, resp.text
    await activate(client, headers, plan["id"])
    after = {e["hostname"]: e for e in (await view(client, headers, room_id))["equipment"]}
    assert (after["floor-full"]["position_state"], after["floor-full"]["x_mm"], after["floor-full"]["y_mm"]) == ("placed", 4000, 2600)
    assert after["floor-full"]["rotation_deg"] == 90
    reasons = (await view(client, headers, room_id))["incomplete_reasons"]
    assert "equipment_position_missing:1" in reasons and "equipment_dimensions_missing:1" in reasons
    again = await accept(client, headers, job["id"], cand, {"object_type": "equipment", "matched_asset_id": made["bare"]["id"], "apply_position": True})
    assert again.status_code in (409, 422)


# ------------------------------------------------------------------------------------------ overlays
OVERLAYS = "/api/v1/spatial/rooms/{room}/overlays"


async def test_power_overlay_follows_the_protection_topology_and_uses_asset_identities(client, auth_headers):
    from tests.api.test_power_protection_impact import dual_fed_server

    h, server, chain = await dual_fed_server(client, auth_headers)
    # put the dual-fed server in a room (the helper builds its UPSes in a different one)
    room_id = await _room(client, auth_headers)
    moved = await client.post(f"/api/v1/equipment/{server['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id}, headers=h)
    assert moved.status_code == 200, moved.text
    url = OVERLAYS.format(room=room_id)

    def item(body):
        return next(i for i in body["overlays"]["power"]["items"] if i["asset_id"] == server["id"])

    ok = (await client.get(url, params={"kinds": "power"}, headers=h)).json()
    assert item(ok)["state"] == "normal" and item(ok)["redundancy"] == "dual_feed_healthy" and len(item(ok)["feed_node_ids"]) == 2
    trip_a = await client.post(f"/api/v1/power/protection-devices/{chain['A']['brk']}/state", json={"state": "tripped"}, headers={**h, "If-Match": "1"})
    assert trip_a.status_code == 200
    degraded = item((await client.get(url, params={"kinds": "power"}, headers=h)).json())
    assert degraded["state"] == "warning" and degraded["interrupting_devices"] == ["BRK-A"] and "redundancy lost" in degraded["reason"]
    await client.post(f"/api/v1/power/protection-devices/{chain['B']['brk']}/state", json={"state": "tripped"}, headers={**h, "If-Match": "1"})
    dead = item((await client.get(url, params={"kinds": "power"}, headers=h)).json())
    assert dead["state"] == "critical" and sorted(dead["interrupting_devices"]) == ["BRK-A", "BRK-B"]


async def _room(client, auth_headers):
    from tests.api._phase2_helpers import create_room

    return await create_room(client, auth_headers)


async def test_power_overlay_reports_unmodelled_power_as_unavailable_not_normal(client, auth_headers):
    headers, room_id, _ = await room_with_plan(client, auth_headers)
    from tests.api._phase2_helpers import create_equipment

    eq = await create_equipment(client, headers, auth_headers)
    await client.post(f"/api/v1/equipment/{eq['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id}, headers=headers)
    body = (await client.get(OVERLAYS.format(room=room_id), params={"kinds": "power"}, headers=headers)).json()
    item = next(i for i in body["overlays"]["power"]["items"] if i["asset_id"] == eq["id"])
    assert item["state"] == "unavailable" and item["redundancy"] == "no_power_modeled"


async def test_network_overlay_separates_authoritative_cables_from_unconfirmed_discovery(client, auth_headers):
    from tests.api._cable_helpers import create_cable
    from tests.api._network_inventory import publish_revision

    admin = await auth_headers("Administrator")
    room_id = await _room(client, auth_headers)
    rack_id = (await place_rack(client, admin, auth_headers, room_id, 500, 500, name="Net Rack"))["id"]
    revision = await publish_revision(client, admin, ["p1", "p2"])
    ids = []
    for host in ("net-a", "net-b"):
        body = {
            "asset_tag": f"EQ-{host}", "catalog_model_revision_id": revision["id"], "hostname": host, "placement_type": "rack_mounted",
            "room_id": room_id, "rack_id": rack_id, "u_start": 1 if host == "net-a" else 3, "u_end": 2 if host == "net-a" else 4, "side": "front",
        }
        r = await client.post("/api/v1/equipment/instantiate", json=body, headers=admin)
        assert r.status_code == 201, r.text
        ids.append(r.json())
    a, b = ids
    port = lambda eq, name: next(p["id"] for p in eq["ports"] if p["display_name"] == name)  # noqa: E731
    await create_cable(client, admin, port(a, "p1"), port(b, "p1"), label="CAB-1", status="installed")
    await create_cable(client, admin, port(a, "p2"), port(b, "p2"), label="CAB-2", status="planned")
    body = (await client.get(OVERLAYS.format(room=room_id), params={"kinds": "network"}, headers=admin)).json()["overlays"]["network"]
    items = {i["asset_id"]: i for i in body["items"]}
    assert items[a["id"]]["installed_cables"] == 1 and items[a["id"]]["planned_cables"] == 1 and items[a["id"]]["state"] == "normal"
    assert items[a["id"]]["peer_asset_ids"] == [b["id"]] and body["source"] == "authoritative_cables"
    assert items[rack_id]["asset_kind"] == "rack" and items[rack_id]["installed_cables"] == 2
    assert all("discovered_unconfirmed" in i for i in body["items"]), "unapproved discovery is counted apart from topology"


async def test_environment_overlay_distinguishes_measured_stale_and_missing(client, auth_headers, db_session):
    from datetime import UTC, datetime, timedelta

    admin = await auth_headers("Administrator")
    room_id = await _room(client, auth_headers)
    fresh_rack = await place_rack(client, admin, auth_headers, room_id, 100, 100, name="Fresh")
    stale_rack = await place_rack(client, admin, auth_headers, room_id, 1000, 100, name="Stale")
    missing_rack = await place_rack(client, admin, auth_headers, room_id, 2000, 100, name="Missing")
    from tests.api._telemetry_helpers import seed_reading

    now = datetime.now(UTC)
    await seed_reading(db_session, fresh_rack["id"], "temperature_c", 24.5, now - timedelta(seconds=30), poll=60)
    await seed_reading(db_session, stale_rack["id"], "temperature_c", 31.0, now - timedelta(hours=2), poll=60)
    body = (await client.get(OVERLAYS.format(room=room_id), params={"kinds": "environment"}, headers=admin)).json()["overlays"]["environment"]
    items = {i["asset_id"]: i for i in body["items"]}
    assert items[fresh_rack["id"]]["data_quality"] == "measured" and items[fresh_rack["id"]]["value"] == 24.5 and items[fresh_rack["id"]]["state"] == "normal"
    assert items[stale_rack["id"]]["data_quality"] == "stale" and items[stale_rack["id"]]["state"] == "warning" and items[stale_rack["id"]]["age_seconds"] > 3600
    assert items[missing_rack["id"]]["data_quality"] == "missing" and items[missing_rack["id"]]["value"] is None and items[missing_rack["id"]]["state"] == "unavailable"
    assert body["estimated_values"] is False and items[fresh_rack["id"]]["occurred_at"]


async def test_overlay_validation_and_permissions(client, auth_headers):
    headers, room_id, _ = await room_with_plan(client, auth_headers)
    url = OVERLAYS.format(room=room_id)
    assert (await client.get(url, params={"kinds": "weather"}, headers=headers)).status_code == 422
    assert (await client.get(url, params={"kinds": ""}, headers=headers)).status_code == 422
    assert (await client.get(OVERLAYS.format(room="00000000-0000-0000-0000-000000000000"), headers=headers)).status_code == 404
    assert (await client.get(url)).status_code == 401
    everything = (await client.get(url, headers=headers)).json()
    assert set(everything["overlays"]) == {"power", "network", "environment"}


# ------------------------------------------------------------------------------------------ authorization
async def test_restricted_site_users_cannot_reach_any_spatial_surface_or_infer_hidden_assets(client, auth_headers):
    from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

    admin = await auth_headers("Administrator")
    site_a = await _make_site(client, admin)
    site_b = await _make_site(client, admin, site_a["org"])
    secret_rack = await place_rack(client, admin, auth_headers, site_b["room"], 1000, 2000, name="TOP-SECRET-RACK")
    plan_b = await new_floor_plan(client, admin, site_b["room"])
    job = await import_file(client, admin, plan_b["id"], fx.rack_row_dxf(2), "b.dxf", "dxf")
    await calibrate_declared(client, admin, plan_b["id"], job["id"])
    cand = by_label(await candidates(client, admin, job["id"]), "RACK-01")
    await _make_rack(client, admin, auth_headers, site_a["room"])
    group = await _group(
        client, admin,
        allow=["floor_plan:read", "floor_plan:manage", "floor_plan:import", "spatial:read", "power:read", "cable:read", "telemetry:read", "rack:read", "equipment:read"],
        sites=[{"site_id": site_a["site"], "rack_scope": "all", "rack_ids": []}],
    )
    _, user = await _group_user(client, admin, [group])
    calls = [
        ("get", f"{FP}", None), ("get", f"{FP}/{plan_b['id']}", None), ("get", f"{FP}/{plan_b['id']}/objects", None),
        ("get", f"{FP}/{plan_b['id']}/calibrations", None), ("get", f"{FP}/{plan_b['id']}/import-jobs", None),
        ("get", f"{FP}/import-jobs/{job['id']}", None), ("get", f"{FP}/import-jobs/{job['id']}/diagnostics", None),
        ("get", f"{FP}/import-jobs/{job['id']}/candidates", None), ("get", f"{FP}/import-jobs/{job['id']}/source-geometry", None),
        ("get", VIEW.format(room=site_b["room"]), None), ("get", OVERLAYS.format(room=site_b["room"]), None),
        ("get", OVERLAYS.format(room=site_a["room"]), None),
    ]
    for method, url, _payload in calls:
        resp = await getattr(client, method)(url, headers=user)
        assert resp.status_code == 403, (url, resp.status_code, resp.text)
        problem = resp.json()
        problem.pop("instance", None)  # echoes the path the caller itself supplied
        for secret in ("TOP-SECRET-RACK", secret_rack["id"], cand["id"], "RACK-01", job["id"]):
            assert secret not in json.dumps(problem), (url, secret)
    writes = [
        client.post(f"{FP}/{plan_b['id']}/calibration", json={"method": "declared_units", "job_id": job["id"]}, headers={**user, "If-Match": "1"}),
        client.patch(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}", json={"label": "x"}, headers={**user, "If-Match": "1"}),
        client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/accept", json={"object_type": "rack"}, headers={**user, "If-Match": "1"}),
        client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/reject", headers={**user, "If-Match": "1"}),
        client.post(f"{FP}/import-jobs/{job['id']}/reconcile", headers=user),
        client.put(f"{FP}/{plan_b['id']}/room-boundary", json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 5, "height_mm": 5}, headers={**user, "If-Match": "1"}),
        client.post(f"{FP}/{plan_b['id']}/upload", files={"file": ("x.dxf", fx.rack_row_dxf(1), "application/octet-stream")}, headers=user),
    ]
    for resp in [await w for w in writes]:
        assert resp.status_code == 403, resp.text
    assert (await client.get(f"{FP}/import-jobs/{job['id']}/candidates", headers=admin)).status_code == 200


async def test_each_overlay_needs_its_own_permission(client, auth_headers, make_user):
    """A global-role user (unrestricted data scope) whose group denies one permission loses exactly that overlay:
    the request is a 403 naming the permission, never a silent omission of that kind."""
    from tests.api.test_user_groups import PW, _group, _login

    admin = await auth_headers("Administrator")
    headers, room_id, _ = await room_with_plan(client, auth_headers)
    url = OVERLAYS.format(room=room_id)
    for denied, kind in (("power:read", "power"), ("cable:read", "network"), ("telemetry:read", "environment"), ("spatial:read", "power")):
        email = f"ov-{denied.replace(':', '-')}-{kind}@example.com"
        user = await make_user(email, PW, "Viewer")
        group = await _group(client, admin, deny=[denied])
        assert (await client.put(f"/api/v1/groups/{group}/members", json={"user_ids": [str(user.id)]}, headers=admin)).status_code == 200
        limited = await _login(client, email)
        blocked = await client.get(url, params={"kinds": kind}, headers=limited)
        assert blocked.status_code == 403 and denied in blocked.json()["detail"], (denied, blocked.text)
        mixed = await client.get(url, params={"kinds": "power,network,environment"}, headers=limited)
        assert mixed.status_code == 403, "a denied kind is refused outright, not dropped from the response"
        assert (await client.get(VIEW.format(room=room_id), headers=limited)).status_code == (403 if denied == "spatial:read" else 200)
    assert (await client.get(url, headers=headers)).status_code == 200
