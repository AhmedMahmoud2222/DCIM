"""User & group management: CRUD, effective permissions, site/rack scoping, privilege
escalation, last-administrator protection and audit trail (docs/USER_GROUP_MANAGEMENT.md)."""

import uuid

import pytest
from sqlalchemy import text

from tests.api._phase2_helpers import create_rack_model_revision

PW = "correct horse battery staple"


async def _login(client, email: str, password: str = PW) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_site(client, admin, org_id=None):
    """Full chain, optionally reusing an organization. Returns dict of ids."""
    if org_id is None:
        org_id = (await client.post("/api/v1/organizations", json={"name": f"Org-{uuid.uuid4().hex[:8]}"}, headers=admin)).json()["id"]
    country = (await client.post("/api/v1/countries", json={"organization_id": org_id, "name": "TL", "iso_code": "TL"}, headers=admin)).json()
    city = (await client.post("/api/v1/cities", json={"country_id": country["id"], "name": f"C-{uuid.uuid4().hex[:5]}"}, headers=admin)).json()
    site = (await client.post("/api/v1/sites", json={"city_id": city["id"], "code": f"S-{uuid.uuid4().hex[:6]}", "name": "Site"}, headers=admin)).json()
    building = (await client.post("/api/v1/buildings", json={"site_id": site["id"], "code": "A", "name": "B"}, headers=admin)).json()
    floor = (await client.post("/api/v1/floors", json={"building_id": building["id"], "name": "F", "level_number": 1}, headers=admin)).json()
    room = (await client.post("/api/v1/rooms", json={"floor_id": floor["id"], "code": f"R-{uuid.uuid4().hex[:5]}", "name": "Room"}, headers=admin)).json()
    return {"org": org_id, "site": site["id"], "room": room["id"]}


async def _make_rack(client, admin, auth_headers, room_id):
    revision = await create_rack_model_revision(client, auth_headers)
    resp = await client.post(
        "/api/v1/racks",
        json={"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision, "name": "Rack", "room_id": room_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _group(client, admin, name=None, *, allow=(), deny=(), sites=()):
    g = (await client.post("/api/v1/groups", json={"name": name or f"G-{uuid.uuid4().hex[:8]}"}, headers=admin)).json()
    if allow or deny:
        r = await client.put(f"/api/v1/groups/{g['id']}/permissions", json={"allow": list(allow), "deny": list(deny)}, headers=admin)
        assert r.status_code == 200, r.text
    if sites:
        r = await client.put(f"/api/v1/groups/{g['id']}/site-access", json={"sites": list(sites)}, headers=admin)
        assert r.status_code == 200, r.text
    return g["id"]


async def _group_user(client, admin, group_ids, email=None):
    email = email or f"gu-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/api/v1/users", json={"email": email, "full_name": "Group User", "password": PW, "group_ids": group_ids}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json(), await _login(client, email)


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


# ------------------------------------------------------------------ CRUD
async def test_group_crud_and_duplicate_name(client, admin):
    r = await client.post("/api/v1/groups", json={"name": "  Ops Team ", "description": "d"}, headers=admin)
    assert r.status_code == 201
    gid = r.json()["id"]
    assert r.json()["name"] == "Ops Team"
    dup = await client.post("/api/v1/groups", json={"name": "ops team"}, headers=admin)
    assert dup.status_code == 409
    upd = await client.patch(f"/api/v1/groups/{gid}", json={"name": "Ops", "description": "x"}, headers=admin)
    assert upd.status_code == 200 and upd.json()["name"] == "Ops"
    listing = await client.get("/api/v1/groups?q=ops", headers=admin)
    assert listing.json()["total"] == 1
    assert (await client.delete(f"/api/v1/groups/{gid}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/v1/groups/{gid}", headers=admin)).status_code == 404


async def test_group_input_validation(client, admin):
    assert (await client.post("/api/v1/groups", json={"name": "   "}, headers=admin)).status_code == 422
    assert (await client.post("/api/v1/groups", json={"name": "x" * 101}, headers=admin)).status_code == 422
    assert (await client.post("/api/v1/groups", json={"name": "bad\x00name"}, headers=admin)).status_code == 422
    gid = await _group(client, admin)
    r = await client.put(f"/api/v1/groups/{gid}/permissions", json={"allow": ["nope:nothing"]}, headers=admin)
    assert r.status_code == 422
    r = await client.put(f"/api/v1/groups/{gid}/permissions", json={"allow": ["rack:read"], "deny": ["rack:read"]}, headers=admin)
    assert r.status_code == 422
    r = await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": str(uuid.uuid4())}]}, headers=admin)
    assert r.status_code == 422


async def test_admin_endpoints_require_permissions(client, auth_headers):
    viewer = await auth_headers("Viewer")
    assert (await client.get("/api/v1/groups", headers=viewer)).status_code == 403
    assert (await client.get("/api/v1/users", headers=viewer)).status_code == 403
    assert (await client.post("/api/v1/groups", json={"name": "x"}, headers=viewer)).status_code == 403
    manager = await auth_headers("DCIM Manager")
    assert (await client.get("/api/v1/groups", headers=manager)).status_code == 403
    assert (await client.get("/api/v1/groups")).status_code == 401


async def test_user_lifecycle(client, admin):
    gid = await _group(client, admin)
    user, headers = await _group_user(client, admin, [gid])
    assert user["is_restricted"] is True and user["groups"][0]["id"] == gid
    weak = await client.post("/api/v1/users", json={"email": "w@example.com", "full_name": "W", "password": "short"}, headers=admin)
    assert weak.status_code == 422
    dup = await client.post("/api/v1/users", json={"email": user["email"], "full_name": "W", "password": PW}, headers=admin)
    assert dup.status_code == 409

    r = await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401  # token no longer usable
    assert (await client.post("/api/v1/auth/login", json={"email": user["email"], "password": PW})).status_code == 401
    r = await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": True, "full_name": "Renamed"}, headers=admin)
    assert r.json()["is_active"] is True and r.json()["full_name"] == "Renamed"

    assert (await client.delete(f"/api/v1/users/{user['id']}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/v1/users/{user['id']}", headers=admin)).status_code == 404


async def test_multiple_group_membership_and_listing(client, admin):
    g1, g2 = await _group(client, admin), await _group(client, admin)
    user, _ = await _group_user(client, admin, [g1, g2])
    assert {g["id"] for g in user["groups"]} == {g1, g2}
    r = await client.patch(f"/api/v1/users/{user['id']}", json={"group_ids": [g2]}, headers=admin)
    assert [g["id"] for g in r.json()["groups"]] == [g2]
    listing = await client.get(f"/api/v1/users?group_id={g2}", headers=admin)
    assert listing.json()["total"] == 1
    detail = (await client.get(f"/api/v1/groups/{g2}", headers=admin)).json()
    assert detail["member_ids"] == [user["id"]]


# ------------------------------------------------------------------ Effective permissions
async def test_group_user_has_no_access_by_default(client, admin):
    user, headers = await _group_user(client, admin, [])
    assert (await client.get("/api/v1/sites", headers=headers)).status_code == 403
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["permission_codes"] == []


async def test_permissions_union_across_groups_and_deny_wins(client, admin):
    ga = await _group(client, admin, allow=["location:read", "rack:read"])
    gb = await _group(client, admin, allow=["equipment:read"], deny=["rack:read"])
    user, headers = await _group_user(client, admin, [ga, gb])
    codes = set((await client.get("/api/v1/auth/me", headers=headers)).json()["permission_codes"])
    assert codes == {"location:read", "equipment:read"}  # union, with rack:read denied by gb
    eff = (await client.get(f"/api/v1/users/{user['id']}/effective-access", headers=admin)).json()
    assert eff["denied_permissions"] == ["rack:read"]
    assert eff["permissions"]["location:read"] == [f"group:{(await client.get(f'/api/v1/groups/{ga}', headers=admin)).json()['name']}"]
    assert eff["unrestricted"] is False


async def test_deny_overrides_role_permission(client, admin, make_user):
    await make_user("op@example.com", PW, "Viewer")
    op = (await client.get("/api/v1/users?q=op@example.com", headers=admin)).json()["items"][0]
    deny_group = await _group(client, admin, deny=["dashboard:read"])
    r = await client.patch(f"/api/v1/users/{op['id']}", json={"group_ids": [deny_group]}, headers=admin)
    assert r.status_code == 200
    headers = await _login(client, "op@example.com")
    assert (await client.get("/api/v1/dashboard/summary", headers=headers)).status_code in (403, 404)
    codes = (await client.get("/api/v1/auth/me", headers=headers)).json()["permission_codes"]
    assert "dashboard:read" not in codes and "rack:read" in codes


async def test_non_site_scoped_permissions_are_inactive_for_restricted_users(client, admin):
    gid = await _group(client, admin, allow=["alarm:read", "telemetry:read", "location:read"])
    user, headers = await _group_user(client, admin, [gid])
    assert (await client.get("/api/v1/alarms", headers=headers)).status_code == 403
    eff = (await client.get(f"/api/v1/users/{user['id']}/effective-access", headers=admin)).json()
    assert set(eff["inactive_permissions"]) == {"alarm:read", "telemetry:read"}
    assert list(eff["permissions"]) == ["location:read"]


# ------------------------------------------------------------------ Site / rack scoping
async def test_site_access_scopes_locations_and_racks(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)  # different organization: cross-tenant
    rack_a = await _make_rack(client, admin, auth_headers, a["room"])
    rack_b = await _make_rack(client, admin, auth_headers, b["room"])

    gid = await _group(
        client, admin, allow=["location:read", "organization:read", "rack:read", "equipment:read"],
        sites=[{"site_id": a["site"], "rack_scope": "all"}],
    )
    _, headers = await _group_user(client, admin, [gid])

    sites = (await client.get("/api/v1/sites", headers=headers)).json()
    assert [s["id"] for s in sites["items"]] == [a["site"]] and sites["total"] == 1
    assert (await client.get(f"/api/v1/sites/{a['site']}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/sites/{b['site']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/rooms/{b['room']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/rooms/{a['room']}", headers=headers)).status_code == 200
    assert [o["id"] for o in (await client.get("/api/v1/organizations", headers=headers)).json()["items"]] == [a["org"]]
    assert (await client.get(f"/api/v1/organizations/{b['org']}", headers=headers)).status_code == 404
    assert len((await client.get("/api/v1/buildings", headers=headers)).json()["items"]) == 1
    assert len((await client.get("/api/v1/rooms", headers=headers)).json()["items"]) == 1

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    assert [r["id"] for r in racks["items"]] == [rack_a] and racks["total"] == 1
    assert (await client.get(f"/api/v1/racks/{rack_a}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/racks/{rack_b}/elevation", headers=headers)).status_code == 404


async def test_selected_rack_scope(client, admin, auth_headers):
    a = await _make_site(client, admin)
    r1 = await _make_rack(client, admin, auth_headers, a["room"])
    r2 = await _make_rack(client, admin, auth_headers, a["room"])
    gid = await _group(
        client, admin, allow=["location:read", "rack:read"],
        sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r1]}],
    )
    _, headers = await _group_user(client, admin, [gid])
    assert [r["id"] for r in (await client.get("/api/v1/racks", headers=headers)).json()["items"]] == [r1]
    assert (await client.get(f"/api/v1/racks/{r2}", headers=headers)).status_code == 404
    # A second group granting the whole site widens access (union; 'all' wins).
    g2 = await _group(client, admin, sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid, g2])
    assert (await client.get("/api/v1/racks", headers=headers)).json()["total"] == 2


async def test_rack_grant_must_belong_to_site(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    rack_b = await _make_rack(client, admin, auth_headers, b["room"])
    gid = await _group(client, admin)
    r = await client.put(
        f"/api/v1/groups/{gid}/site-access",
        json={"sites": [{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack_b]}]}, headers=admin,
    )
    assert r.status_code == 422
    r = await client.put(
        f"/api/v1/groups/{gid}/site-access",
        json={"sites": [{"site_id": a["site"], "rack_scope": "all", "rack_ids": [rack_b]}]}, headers=admin,
    )
    assert r.status_code == 422


async def test_moved_rack_does_not_leak_through_stale_grant(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    rack = await _make_rack(client, admin, auth_headers, a["room"])
    gid = await _group(
        client, admin, allow=["rack:read", "location:read"],
        sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack]}],
    )
    _, headers = await _group_user(client, admin, [gid])
    assert (await client.get(f"/api/v1/racks/{rack}", headers=headers)).status_code == 200
    moved = await client.post(f"/api/v1/racks/{rack}/move", json={"room_id": b["room"]}, headers=admin)
    assert moved.status_code == 200
    assert (await client.get(f"/api/v1/racks/{rack}", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/racks", headers=headers)).json()["total"] == 0


async def test_restricted_user_cannot_move_or_create_racks_across_sites(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    rack = await _make_rack(client, admin, auth_headers, a["room"])
    revision = await create_rack_model_revision(client, auth_headers)
    gid = await _group(
        client, admin, allow=["rack:read", "rack:manage", "rack:place", "location:read"],
        sites=[{"site_id": a["site"], "rack_scope": "all"}],
    )
    _, headers = await _group_user(client, admin, [gid])
    assert (await client.post(f"/api/v1/racks/{rack}/move", json={"room_id": b["room"]}, headers=headers)).status_code == 404
    body = {"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision, "name": "n"}
    assert (await client.post("/api/v1/racks", json=body, headers=headers)).status_code == 403  # unplaced
    assert (await client.post("/api/v1/racks", json={**body, "room_id": b["room"]}, headers=headers)).status_code == 404
    ok = await client.post("/api/v1/racks", json={**body, "room_id": a["room"]}, headers=headers)
    assert ok.status_code == 201


async def test_equipment_follows_rack_visibility(client, admin, auth_headers):
    from tests.api._phase2_helpers import create_equipment_model_revision

    a, b = await _make_site(client, admin), await _make_site(client, admin)
    rack_a = await _make_rack(client, admin, auth_headers, a["room"])
    rack_b = await _make_rack(client, admin, auth_headers, b["room"])
    eq_rev = await create_equipment_model_revision(client, auth_headers)
    ids = {}
    for key, rack, room in (("a", rack_a, a["room"]), ("b", rack_b, b["room"])):
        eq = (await client.post("/api/v1/equipment", json={"asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "model_revision_id": eq_rev}, headers=admin)).json()
        mv = await client.post(
            f"/api/v1/equipment/{eq['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": room, "rack_id": rack, "u_start": 1, "u_end": 2, "side": "front"},
            headers=admin,
        )
        assert mv.status_code == 200, mv.text
        ids[key] = eq["id"]
    gid = await _group(client, admin, allow=["equipment:read", "rack:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    assert [e["id"] for e in (await client.get("/api/v1/equipment", headers=headers)).json()["items"]] == [ids["a"]]
    assert (await client.get(f"/api/v1/equipment/{ids['b']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/equipment/{ids['b']}/ports", headers=headers)).status_code == 404


async def test_existing_role_users_remain_unrestricted(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    viewer = await auth_headers("Viewer")
    assert (await client.get("/api/v1/sites", headers=viewer)).json()["total"] == 2
    del a, b


async def test_removing_group_access_takes_effect_immediately(client, admin):
    a = await _make_site(client, admin)
    gid = await _group(client, admin, allow=["location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    assert (await client.get("/api/v1/sites", headers=headers)).json()["total"] == 1
    await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": []}, headers=admin)
    assert (await client.get("/api/v1/sites", headers=headers)).status_code == 403


# ------------------------------------------------------------------ Privilege escalation
async def _delegated_admin(client, admin, extra_allow=()):
    gid = await _group(client, admin, allow=["user:read", "user:manage", "group:read", "group:manage", "location:read", *extra_allow])
    user, headers = await _group_user(client, admin, [gid])
    return gid, user, headers


async def test_delegate_cannot_grant_permissions_they_lack(client, admin):
    _, _, dh = await _delegated_admin(client, admin)
    target = await _group(client, dh)
    r = await client.put(f"/api/v1/groups/{target}/permissions", json={"allow": ["rack:read"]}, headers=dh)
    assert r.status_code == 403
    r = await client.put(f"/api/v1/groups/{target}/permissions", json={"allow": ["group:manage"]}, headers=dh)
    assert r.status_code == 200
    r = await client.put(f"/api/v1/groups/{target}/permissions", json={"allow": ["role:manage"]}, headers=dh)
    assert r.status_code == 403


async def test_delegate_cannot_assign_administrator_role_or_powerful_group(client, admin):
    _, _, dh = await _delegated_admin(client, admin)
    r = await client.post(
        "/api/v1/users", json={"email": "esc@example.com", "full_name": "E", "password": PW, "role_name": "Administrator"}, headers=dh
    )
    assert r.status_code == 403
    powerful = await _group(client, admin, allow=["rack:manage"])
    r = await client.post("/api/v1/users", json={"email": "esc2@example.com", "full_name": "E", "password": PW, "group_ids": [powerful]}, headers=dh)
    assert r.status_code == 403


async def test_delegate_cannot_edit_own_group_or_memberships(client, admin):
    gid, user, dh = await _delegated_admin(client, admin)
    assert (await client.put(f"/api/v1/groups/{gid}/permissions", json={"allow": ["rack:read"]}, headers=dh)).status_code == 403
    assert (await client.delete(f"/api/v1/groups/{gid}", headers=dh)).status_code == 403
    assert (await client.patch(f"/api/v1/users/{user['id']}", json={"group_ids": []}, headers=dh)).status_code == 403


async def test_delegate_cannot_grant_sites_outside_own_scope(client, admin):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    gid = await _group(
        client, admin, allow=["user:read", "user:manage", "group:read", "group:manage"],
        sites=[{"site_id": a["site"], "rack_scope": "all"}],
    )
    _, dh = await _group_user(client, admin, [gid])
    target = await _group(client, dh)
    assert (await client.put(f"/api/v1/groups/{target}/site-access", json={"sites": [{"site_id": b["site"], "rack_scope": "all"}]}, headers=dh)).status_code == 403
    assert (await client.put(f"/api/v1/groups/{target}/site-access", json={"sites": [{"site_id": a["site"], "rack_scope": "all"}]}, headers=dh)).status_code == 200


async def test_delegate_cannot_administer_more_powerful_user(client, admin, make_user):
    _, _, dh = await _delegated_admin(client, admin)
    victim = await make_user("victim@example.com", PW, "Administrator")
    assert (await client.patch(f"/api/v1/users/{victim.id}", json={"is_active": False}, headers=dh)).status_code == 403
    assert (await client.patch(f"/api/v1/users/{victim.id}", json={"password": "x" * 20}, headers=dh)).status_code == 403
    assert (await client.delete(f"/api/v1/users/{victim.id}", headers=dh)).status_code == 403


# ------------------------------------------------------------------ Last administrator
async def test_cannot_deactivate_or_delete_own_account(client, admin):
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert (await client.patch(f"/api/v1/users/{me['id']}", json={"is_active": False}, headers=admin)).status_code == 403
    assert (await client.delete(f"/api/v1/users/{me['id']}", headers=admin)).status_code == 403


async def test_deny_that_would_remove_the_last_administrator_is_rolled_back(client, admin, make_user, db_session):
    gid = await _group(client, admin, allow=["user:manage", "user:read", "group:manage", "group:read"])
    user, _ = await _group_user(client, admin, [gid])
    # Make the group member the only administrator.
    await db_session.execute(text("DELETE FROM role_assignment WHERE user_id <> :u"), {"u": user["id"]})
    await db_session.commit()
    root = await make_user("root2@example.com", PW, "Administrator")
    root_headers = await _login(client, "root2@example.com")
    # Removing the member leaves `root` -> fine.
    assert (await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": []}, headers=root_headers)).status_code == 200
    # Deleting the last role-based admin's only power path is blocked: deny on a group containing root.
    denier = await _group(client, root_headers, deny=["group:manage"])
    r = await client.put(f"/api/v1/groups/{denier}/members", json={"user_ids": [str(root.id)]}, headers=root_headers)
    assert r.status_code == 409
    detail = (await client.get(f"/api/v1/groups/{denier}", headers=root_headers)).json()
    assert detail["member_ids"] == []  # rolled back


# ------------------------------------------------------------------ Audit
async def test_admin_mutations_are_audited(client, admin, _admin_engine):
    gid = await _group(client, admin, allow=["rack:read"])
    user, _ = await _group_user(client, admin, [gid])
    await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}, headers=admin)
    await client.delete(f"/api/v1/users/{user['id']}", headers=admin)
    async with _admin_engine.connect() as conn:
        actions = {row[0] for row in await conn.execute(text("SELECT action FROM audit_log"))}
        dump = (await conn.execute(text("SELECT COALESCE(string_agg(after::text || before::text, ' '), '') FROM audit_log"))).scalar_one()
    assert {"group.create", "group.permissions.update", "user.create", "user.deactivate", "user.delete"} <= actions
    assert PW not in dump


async def test_password_change_is_audited_without_secret(client, admin, _admin_engine):
    user, _ = await _group_user(client, admin, [])
    new_pw = "another very long passphrase"
    r = await client.patch(f"/api/v1/users/{user['id']}", json={"password": new_pw}, headers=admin)
    assert r.status_code == 200
    assert (await client.post("/api/v1/auth/login", json={"email": user["email"], "password": new_pw})).status_code == 200
    async with _admin_engine.connect() as conn:
        dump = (await conn.execute(text("SELECT COALESCE(string_agg(after::text, ' '), '') FROM audit_log"))).scalar_one()
    assert new_pw not in dump


# ------------------------------------------------------------------ Migration / seed
async def test_new_permissions_seeded_for_administrator_only(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT r.name, p.resource || ':' || p.action FROM role r "
                "JOIN role_permission rp ON rp.role_id = r.id JOIN permission p ON p.id = rp.permission_id "
                "WHERE (p.resource, p.action) IN (('user','read'),('group','read'),('group','manage'))"
            )
        )
    ).all()
    assert {r[0] for r in rows} == {"Administrator"}
    assert {r[1] for r in rows} == {"user:read", "group:read", "group:manage"}


async def test_existing_role_users_keep_their_permissions(client, auth_headers):
    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS

    for role, expected in DEFAULT_ROLE_PERMISSIONS.items():
        headers = await auth_headers(role)
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        assert set(me["permission_codes"]) == set(expected), role
