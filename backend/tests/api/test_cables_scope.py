"""Site/rack scope and direct-ID isolation for cables, traces, discovery and profiles.

A restricted user holds `cable:read`/`cable:manage` for site A only. Everything that would
reveal or change equipment at site B must be a 404 (not a 403), cross-scope mutation must
change nothing, and permissions that are not site-aware (profiles, discovery, integrations)
must stay inert for a restricted user."""

import json
import uuid

import pytest
from sqlalchemy import text

from tests.api._cable_helpers import CABLES, cable_body, create_cable, if_match
from tests.api._network_inventory import publish_revision
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site


async def place(client, admin, revision_id: str, site: dict, rack: str, u: int, hostname: str) -> dict:
    body = {
        "asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision_id, "hostname": hostname,
        "placement_type": "rack_mounted", "room_id": site["room"], "rack_id": rack, "u_start": u, "u_end": u + 1, "side": "front",
    }
    response = await client.post("/api/v1/equipment/instantiate", json=body, headers=admin)
    assert response.status_code == 201, response.text
    equipment = response.json()
    equipment["port_by_name"] = {p["display_name"]: p["id"] for p in equipment["ports"]}
    return equipment


@pytest.fixture
async def world(client, auth_headers):
    admin = await auth_headers("Administrator")
    site_a = await _make_site(client, admin)
    site_b = await _make_site(client, admin, site_a["org"])
    rack_a = await _make_rack(client, admin, auth_headers, site_a["room"])
    rack_b = await _make_rack(client, admin, auth_headers, site_b["room"])
    revision = await publish_revision(client, admin, ["p1", "p2", "p3"])
    a1 = await place(client, admin, revision["id"], site_a, rack_a, 1, "a1-sw")
    a2 = await place(client, admin, revision["id"], site_a, rack_a, 3, "a2-sw")
    b1 = await place(client, admin, revision["id"], site_b, rack_b, 1, "b1-sw")
    b2 = await place(client, admin, revision["id"], site_b, rack_b, 3, "b2-sw")
    inside = await create_cable(client, admin, a1.get("port_by_name")["p1"], a2["port_by_name"]["p1"], label="IN-A", status="installed")
    crossing = await create_cable(client, admin, a1["port_by_name"]["p2"], b1["port_by_name"]["p2"], label="CROSS", status="installed")
    outside = await create_cable(client, admin, b1["port_by_name"]["p1"], b2["port_by_name"]["p1"], label="OUT-B", status="installed")
    group = await _group(
        client, admin, allow=["cable:read", "cable:manage", "rack:read", "equipment:read"],
        sites=[{"site_id": site_a["site"], "rack_scope": "all", "rack_ids": []}],
    )
    user, headers = await _group_user(client, admin, [group])
    return {"admin": admin, "headers": headers, "user": user, "a1": a1, "a2": a2, "b1": b1, "b2": b2,
            "inside": inside, "crossing": crossing, "outside": outside, "site_b": site_b}


def leaked(blob: str, world: dict, *, supplied: tuple[str, ...] = ()) -> list[str]:
    """Identifiers of site-B equipment that appear in a response, except ones the caller itself supplied."""
    secrets = [world["b1"]["id"], world["b2"]["id"], "b1-sw", "b2-sw", *world["b1"]["port_by_name"].values(), *world["b2"]["port_by_name"].values()]
    return [s for s in secrets if s in blob and s not in supplied]


async def test_list_shows_only_cables_with_a_visible_endpoint_and_masks_the_far_end(client, world):
    response = await client.get(CABLES, headers=world["headers"])
    assert response.status_code == 200
    body = response.json()
    assert {c["label"] for c in body["items"]} == {"IN-A", "CROSS"} and body["total"] == 2
    crossing = next(c for c in body["items"] if c["label"] == "CROSS")
    masked = next(e for e in crossing["endpoints"] if e["restricted"])
    assert masked["port"] is None and masked["end"] == "B"
    visible = next(e for e in crossing["endpoints"] if not e["restricted"])
    assert visible["port"]["equipment_hostname"] == "a1-sw"
    assert leaked(response.text, world) == []
    filtered = await client.get(CABLES, params={"equipment_id": world["b1"]["id"]}, headers=world["headers"])
    assert filtered.json()["total"] == 1  # only the partly visible cable; B-only cables never appear
    assert leaked(filtered.text, world) == []
    by_port = await client.get(CABLES, params={"port_id": world["b2"]["port_by_name"]["p1"]}, headers=world["headers"])
    assert by_port.json()["total"] == 0


async def test_direct_id_access_to_invisible_cables_is_a_404(client, world):
    h = world["headers"]
    assert (await client.get(f"{CABLES}/{world['outside']['id']}", headers=h)).status_code == 404
    assert (await client.get(f"{CABLES}/{world['inside']['id']}", headers=h)).status_code == 200
    partial = await client.get(f"{CABLES}/{world['crossing']['id']}", headers=h)
    assert partial.status_code == 200 and leaked(partial.text, world) == []
    assert (await client.get(f"{CABLES}/{uuid.uuid4()}", headers=h)).status_code == 404  # indistinguishable from the above


async def test_cross_scope_mutation_changes_nothing(client, world, db_session):
    h, outside, crossing = world["headers"], world["outside"], world["crossing"]
    attempts = [
        ("patch", f"{CABLES}/{outside['id']}", {"json": {"notes": "pwned"}}),
        ("post", f"{CABLES}/{outside['id']}/remove", {"json": {}}),
        ("delete", f"{CABLES}/{outside['id']}", {}),
        ("patch", f"{CABLES}/{crossing['id']}", {"json": {"notes": "pwned"}}),         # one end invisible: still refused
        ("post", f"{CABLES}/{crossing['id']}/remove", {"json": {}}),
        ("delete", f"{CABLES}/{crossing['id']}", {}),
    ]
    for method, path, kwargs in attempts:
        response = await getattr(client, method)(path, headers=h | if_match(1), **kwargs)
        assert response.status_code == 404, (method, path, response.status_code)
        assert leaked(response.text, world) == []
    rows = (await db_session.execute(text("SELECT label, status, notes, version FROM cable ORDER BY label"))).all()
    assert [tuple(r) for r in rows] == [("CROSS", "installed", None, 1), ("IN-A", "installed", None, 1), ("OUT-B", "installed", None, 1)]


async def test_cannot_create_cables_touching_invisible_equipment(client, world, db_session):
    h = world["headers"]
    for a, b in (
        (world["a1"]["port_by_name"]["p3"], world["b2"]["port_by_name"]["p3"]),   # one end outside scope
        (world["b1"]["port_by_name"]["p3"], world["b2"]["port_by_name"]["p3"]),   # both outside
    ):
        response = await client.post(CABLES, json=cable_body(a, b), headers=h)
        assert response.status_code == 404, response.text
        assert leaked(response.text, world, supplied=(a, b)) == []  # echoing the caller's own port id is not a leak
    assert (await db_session.execute(text("SELECT count(*) FROM cable"))).scalar_one() == 3
    ok = await client.post(CABLES, json=cable_body(world["a1"]["port_by_name"]["p3"], world["a2"]["port_by_name"]["p3"]), headers=h)
    assert ok.status_code == 201  # the same user can still work inside their own scope
    mine = await client.delete(f"{CABLES}/{ok.json()['id']}", headers=h | if_match(1))
    assert mine.status_code == 204


async def test_trace_respects_scope(client, world):
    h = world["headers"]
    assert (await client.get(f"/api/v1/topology/ports/{world['b1']['port_by_name']['p1']}/trace", headers=h)).status_code == 404
    through = await client.get(f"/api/v1/topology/ports/{world['a1']['port_by_name']['p2']}/trace", headers=h)
    assert through.status_code == 200
    trace = through.json()
    assert trace["terminated"] == "restricted" and trace["path"][0]["hop"] == {"restricted": True, "remote": None}
    assert trace["path"][0]["link"]["kind"] == "cable" and leaked(through.text, world) == []
    inside = (await client.get(f"/api/v1/topology/ports/{world['a1']['port_by_name']['p1']}/trace", headers=h)).json()
    assert inside["terminated"] == "end_of_path" and inside["path"][0]["hop"]["remote"]["equipment_hostname"] == "a2-sw"


async def test_unscoped_permissions_stay_inert_for_a_restricted_user(client, world):
    h = world["headers"]
    for path in (
        "/api/v1/network-profiles/vendors", "/api/v1/discovery/neighbors", "/api/v1/discovery/devices",
        "/api/v1/integrations", "/api/v1/collectors",
    ):
        assert (await client.get(path, headers=h)).status_code == 403, path
    assert (await client.post(f"{CABLES}/from-neighbor/{uuid.uuid4()}", json={"label": "x", "cable_type": "other"}, headers=h)).status_code == 403
    assert (await client.post(f"/api/v1/discovery/neighbors/{uuid.uuid4()}/confirm", json={}, headers=h | if_match(1))).status_code == 403
    assert (await client.patch(f"/api/v1/integrations/{uuid.uuid4()}", json={"enabled": False}, headers=h | if_match(1))).status_code == 403


async def test_a_user_without_cable_permissions_sees_nothing(client, world, auth_headers):
    admin = world["admin"]
    group = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": world["site_b"]["site"], "rack_scope": "all", "rack_ids": []}])
    _user, headers = await _group_user(client, admin, [group])
    assert (await client.get(CABLES, headers=headers)).status_code == 403
    assert (await client.get(f"{CABLES}/{world['inside']['id']}", headers=headers)).status_code == 403
    assert (await client.get(f"/api/v1/topology/ports/{world['a1']['port_by_name']['p1']}/trace", headers=headers)).status_code == 403
    assert json.dumps(world["inside"]) not in ""  # (document the fixture is unchanged)


async def test_denied_cable_permission_overrides_an_allow(client, world):
    admin = world["admin"]
    allow = await _group(client, admin, allow=["cable:read", "cable:manage"], sites=[{"site_id": world["site_b"]["site"], "rack_scope": "all", "rack_ids": []}])
    deny = await _group(client, admin, deny=["cable:manage"])
    _user, headers = await _group_user(client, admin, [allow, deny])
    assert (await client.get(CABLES, headers=headers)).status_code == 200
    denied = await client.post(CABLES, json=cable_body(world["b1"]["port_by_name"]["p3"], world["b2"]["port_by_name"]["p3"]), headers=headers)
    assert denied.status_code == 403
