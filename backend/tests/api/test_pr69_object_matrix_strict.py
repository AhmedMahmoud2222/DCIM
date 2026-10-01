"""Object-level authorization matrix for PR #59 with no vacuous acceptance paths.

What distinguishes this from the earlier matrix:
  * every request carries a VALID payload against REAL resources (a real rack model, real foreign rack,
    equipment, ports, rooms), so a refusal cannot be an unrelated 409/422 validation failure;
  * the caller's effective permissions are read from the effective-access endpoint BEFORE any operation
    is judged (what is active, what is reported inactive);
  * the intended denial is asserted exactly: object-level isolation answers 404 identical to a missing id,
    permission enforcement answers 403 (and the permission is shown to be inactive for this caller);
  * every operation family has an authorised positive control on the caller's own resources;
  * after each rejected mutation the protected state is re-read and compared with the snapshot.

Three different claims are kept apart (see test_pr69_route_sweep_strict.py for the first two):
  route registration / sweep coverage   -> which operations exist and how each answers
  authentication + permission enforcement -> 403 comes from the permission layer, contrasted with the admin
  cross-resource isolation              -> this file: Site B objects are invisible and untouchable from Site A
"""

import uuid
from types import SimpleNamespace

import pytest

from tests.api._phase2_helpers import create_equipment
from tests.api.test_equipment_instantiation import _make_published_equipment_revision
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

DATA = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]
INACTIVE_WRITES = ["equipment:manage", "equipment:place", "location:manage", "location:update"]  # granted, but endpoints are not site-aware


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _place(client, admin, eq, site, rack, u):
    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": site["room"], "rack_id": rack, "u_start": u, "u_end": u + 1, "side": "front"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture
async def w(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)
    rack_a1, rack_a2 = [await _make_rack(client, admin, auth_headers, a["room"]) for _ in range(2)]
    rack_b1 = await _make_rack(client, admin, auth_headers, b["room"])
    catalog_revision = await _make_published_equipment_revision(client, admin, port_count=2, psu_quantity=0)

    async def instantiate():
        resp = await client.post(
            "/api/v1/equipment/instantiate",
            json={"asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": catalog_revision["id"]}, headers=admin,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    eq_a, eq_b = await instantiate(), await instantiate()  # real equipment with real ports
    eq_b2 = await create_equipment(client, admin, auth_headers)
    await _place(client, admin, eq_a, a, rack_a1, 1)
    await _place(client, admin, eq_b, b, rack_b1, 1)
    await _place(client, admin, eq_b2, b, rack_b1, 10)
    gid = await _group(client, admin, allow=[*DATA, *INACTIVE_WRITES], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    revision = (await client.get(f"/api/v1/racks/{rack_a1}", headers=admin)).json()["model_revision_id"]
    return SimpleNamespace(
        a=a, b=b, rack_a1=rack_a1, rack_a2=rack_a2, rack_b1=rack_b1, eq_a=eq_a["id"], eq_b=eq_b["id"], eq_b2=eq_b2["id"],
        headers=headers, user=user, admin=admin, revision=revision,
        port_b=eq_b["ports"][0]["id"], port_a=eq_a["ports"][0]["id"],
    )


async def _version(client, admin, kind, rid):
    """Current version for If-Match; 1 for an id that does not exist (the baseline request)."""
    resp = await client.get(f"/api/v1/{kind}/{rid}", headers=admin)
    return resp.json().get("version", 1) if resp.status_code == 200 else 1


# ---------------------------------------------------------------- one callable per operation: (client, w, headers, target ids)
# Every callable takes the object id explicitly so the identical request can be replayed against a random id
# (the "missing" baseline) and against the caller's own object (the positive control).
async def _rack_patch(c, w, h, rid, ver=None):
    ver = ver if ver is not None else await _version(c, w.admin, "racks", rid)
    return await c.patch(f"/api/v1/racks/{rid}", json={"name": f"renamed-{uuid.uuid4().hex[:6]}"}, headers={**h, "If-Match": str(ver)})


OBJECT_OPS = {
    # name: (callable(c, w, h, rid), foreign target attr, own target attr, own-expected-status)
    "rack.read": (lambda c, w, h, rid: c.get(f"/api/v1/racks/{rid}", headers=h), "rack_b1", "rack_a1", 200),
    "rack.elevation": (lambda c, w, h, rid: c.get(f"/api/v1/racks/{rid}/elevation", headers=h), "rack_b1", "rack_a1", 200),
    "rack.update": (lambda c, w, h, rid: _rack_patch(c, w, h, rid), "rack_b1", "rack_a1", 200),
    "rack.move-foreign-rack-into-own-room": (
        lambda c, w, h, rid: c.post(f"/api/v1/racks/{rid}/move", json={"room_id": w.a["room"], "x_mm": 100, "y_mm": 100, "rotation_deg": 0}, headers=h),
        "rack_b1", "rack_a1", 200),
    "rack.retire": (lambda c, w, h, rid: c.post(f"/api/v1/racks/{rid}/retire", headers=h), "rack_b1", "rack_a2", 200),
    "equipment.read": (lambda c, w, h, rid: c.get(f"/api/v1/equipment/{rid}", headers=h), "eq_b", "eq_a", 200),
    "equipment.ports": (lambda c, w, h, rid: c.get(f"/api/v1/equipment/{rid}/ports", headers=h), "eq_b", "eq_a", 200),
}
# operations whose TARGET is a room/site/org, or whose DESTINATION is a foreign room
SCOPE_TARGET_OPS = {
    "site.read": (lambda c, w, h, sid: c.get(f"/api/v1/sites/{sid}", headers=h), lambda w: w.b["site"], lambda w: w.a["site"], 200),
    "room.read": (lambda c, w, h, sid: c.get(f"/api/v1/rooms/{sid}", headers=h), lambda w: w.b["room"], lambda w: w.a["room"], 200),
    "organization.read": (lambda c, w, h, sid: c.get(f"/api/v1/organizations/{sid}", headers=h), lambda w: w.b["org"], lambda w: w.a["org"], 200),
    "rack.create-in-foreign-room": (
        lambda c, w, h, room: c.post(
            "/api/v1/racks", json={"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": w.revision, "name": "new", "room_id": str(room)}, headers=h),
        lambda w: w.b["room"], lambda w: w.a["room"], 201),
}
# operations a restricted caller can never perform: the permission is inactive, so the answer is 403 for foreign AND own objects
PERMISSION_OPS = {
    "equipment.update": (lambda c, w, h, rid: _equipment_patch(c, w, h, rid), "eq_b", "eq_a"),
    "equipment.move": (
        lambda c, w, h, rid: c.post(
            f"/api/v1/equipment/{rid}/move",
            json={"placement_type": "rack_mounted", "room_id": w.a["room"], "rack_id": w.rack_a1, "u_start": 20, "u_end": 21, "side": "front"}, headers=h),
        "eq_b", "eq_a"),
    "equipment.retire": (lambda c, w, h, rid: c.post(f"/api/v1/equipment/{rid}/retire", headers=h), "eq_b", "eq_a"),
    "equipment.port-connect": (
        lambda c, w, h, rid: c.post(f"/api/v1/equipment/{rid}/ports/connect", json={"port_id": w.port_b or str(uuid.uuid4()), "target_port_id": w.port_a or str(uuid.uuid4())}, headers=h),
        "eq_b", "eq_a"),
    "room.update": (lambda c, w, h, rid: _room_patch(c, w, h, rid), "b_room", "a_room"),
}


async def _room_patch(c, w, h, rid):
    ver = await _version(c, w.admin, "rooms", rid)
    return await c.patch(f"/api/v1/rooms/{rid}", json={"name": "renamed by attacker"}, headers={**h, "If-Match": str(ver)})


async def _equipment_patch(c, w, h, rid):
    ver = await _version(c, w.admin, "equipment", rid)
    return await c.patch(f"/api/v1/equipment/{rid}", json={"owner": "attacker"}, headers={**h, "If-Match": str(ver)})


def _target(w, attr):
    return {"b_room": w.b["room"], "a_room": w.a["room"]}.get(attr) or getattr(w, attr)


async def _state(c, w):
    """Everything a rejected mutation must leave untouched, read as the administrator."""
    rack = (await c.get(f"/api/v1/racks/{w.rack_b1}", headers=w.admin)).json()
    equipment = [(await c.get(f"/api/v1/equipment/{e}", headers=w.admin)).json() for e in (w.eq_b, w.eq_b2)]
    racks = (await c.get("/api/v1/racks?limit=200", headers=w.admin)).json()["total"]
    room = (await c.get(f"/api/v1/rooms/{w.b['room']}", headers=w.admin)).json()
    return {"rack": rack, "equipment": equipment, "rack_total": racks, "room_b": room}


def _same_denial(resp, baseline):
    assert resp.status_code == baseline.status_code, (resp.status_code, resp.text, baseline.status_code)
    assert resp.json().get("title") == baseline.json().get("title")


# ---------------------------------------------------------------- 0. prerequisites
async def test_prerequisites_effective_permissions_before_any_operation(client, w):
    eff = (await client.get(f"/api/v1/users/{w.user['id']}/effective-access", headers=w.admin)).json()
    assert eff["unrestricted"] is False
    assert {s["site_id"] for s in eff["sites"]} == {w.a["site"]}
    assert set(DATA) <= set(eff["permissions"]), "read/manage/place data permissions must be ACTIVE for the caller"
    assert set(INACTIVE_WRITES) <= set(eff["inactive_permissions"]), "write permissions must be reported INACTIVE, not silently granted"
    # Site B objects exist and the administrator can read them: a 404 below is isolation, not absence.
    for url in (f"/api/v1/racks/{w.rack_b1}", f"/api/v1/equipment/{w.eq_b}", f"/api/v1/rooms/{w.b['room']}", f"/api/v1/sites/{w.b['site']}"):
        assert (await client.get(url, headers=w.admin)).status_code == 200, url
    assert w.port_b and w.port_a, "fixtures must expose real ports for the port-connect payload"


# ---------------------------------------------------------------- 1. object isolation: exact 404, identical to a missing id, state unchanged
@pytest.mark.parametrize("op", sorted(OBJECT_OPS))
async def test_foreign_object_is_a_uniform_404_and_nothing_changes(client, w, op):
    call, foreign_attr, _, _ = OBJECT_OPS[op]
    before = await _state(client, w)
    resp = await call(client, w, w.headers, getattr(w, foreign_attr))
    baseline = await call(client, w, w.headers, uuid.uuid4())
    assert baseline.status_code == 404, "the baseline (random id) must itself be a 404 from the same code path"
    _same_denial(resp, baseline)
    assert await _state(client, w) == before


@pytest.mark.parametrize("op", sorted(SCOPE_TARGET_OPS))
async def test_foreign_site_room_org_or_destination_is_a_uniform_404_and_nothing_changes(client, w, op):
    call, foreign, _, _ = SCOPE_TARGET_OPS[op]
    before = await _state(client, w)
    resp = await call(client, w, w.headers, foreign(w))
    baseline = await call(client, w, w.headers, uuid.uuid4())
    assert baseline.status_code == 404
    _same_denial(resp, baseline)
    assert await _state(client, w) == before


async def test_own_rack_cannot_be_moved_into_a_foreign_room(client, w):
    """Cross-site widening of the caller's own rack: the destination room is outside the caller's scope."""
    before = (await client.get(f"/api/v1/racks/{w.rack_a1}", headers=w.admin)).json()
    resp = await client.post(f"/api/v1/racks/{w.rack_a1}/move", json={"room_id": w.b["room"], "x_mm": 1, "y_mm": 1, "rotation_deg": 0}, headers=w.headers)
    baseline = await client.post(f"/api/v1/racks/{w.rack_a1}/move", json={"room_id": str(uuid.uuid4()), "x_mm": 1, "y_mm": 1, "rotation_deg": 0}, headers=w.headers)
    assert baseline.status_code == 404
    _same_denial(resp, baseline)
    assert (await client.get(f"/api/v1/racks/{w.rack_a1}", headers=w.admin)).json() == before
    assert (await client.get(f"/api/v1/racks/{w.rack_a1}", headers=w.headers)).status_code == 200  # still visible: it did not leave Site A


# ---------------------------------------------------------------- 2. permission enforcement: exact 403 for foreign AND own objects
@pytest.mark.parametrize("op", sorted(PERMISSION_OPS))
async def test_inactive_write_permission_is_a_403_for_foreign_and_for_own_objects_and_changes_nothing(client, w, op):
    call, foreign_attr, own_attr = PERMISSION_OPS[op]
    before = await _state(client, w)
    for attr in (foreign_attr, own_attr):
        resp = await call(client, w, w.headers, _target(w, attr))
        assert resp.status_code == 403, (op, attr, resp.status_code, resp.text)
    missing = await call(client, w, w.headers, uuid.uuid4())
    assert missing.status_code == 403, "permission check precedes the lookup: no existence oracle"
    assert await _state(client, w) == before


# ---------------------------------------------------------------- 3. authorised positive controls (same requests, entitled objects)
@pytest.mark.parametrize("op", sorted(OBJECT_OPS))
async def test_positive_control_own_object_succeeds(client, w, op):
    call, _, own_attr, expected = OBJECT_OPS[op]
    resp = await call(client, w, w.headers, getattr(w, own_attr))
    assert resp.status_code == expected, (op, resp.status_code, resp.text)


@pytest.mark.parametrize("op", sorted(SCOPE_TARGET_OPS))
async def test_positive_control_own_site_room_org_succeeds(client, w, op):
    call, _, own, expected = SCOPE_TARGET_OPS[op]
    resp = await call(client, w, w.headers, own(w))
    assert resp.status_code == expected, (op, resp.status_code, resp.text)


async def test_positive_control_unrestricted_administrator_can_use_the_foreign_objects(client, w):
    for op in ("rack.read", "rack.elevation", "rack.update", "equipment.read", "equipment.ports"):
        call, foreign_attr, _, expected = OBJECT_OPS[op]
        resp = await call(client, w, w.admin, getattr(w, foreign_attr))
        assert resp.status_code == expected, (op, resp.status_code, resp.text)
    assert (await PERMISSION_OPS["equipment.update"][0](client, w, w.admin, w.eq_b)).status_code == 200


async def test_equipment_in_a_foreign_rack_is_invisible_in_lists_and_totals(client, w):
    listing = (await client.get("/api/v1/equipment?limit=200", headers=w.headers)).json()
    assert {i["id"] for i in listing["items"]} == {w.eq_a} and listing["total"] == 1
    racks = (await client.get("/api/v1/racks?limit=200", headers=w.headers)).json()
    assert {i["id"] for i in racks["items"]} == {w.rack_a1, w.rack_a2} and racks["total"] == 2
