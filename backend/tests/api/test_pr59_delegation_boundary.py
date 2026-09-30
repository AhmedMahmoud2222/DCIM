"""Hostile review of PR #59 (user & group administration as a security boundary).

Every test asserts the SECURE outcome, so a failure means the delegated-administration
boundary can be crossed. Actors are *site-restricted delegated administrators*: users with
no global role, whose groups grant user/group administration inside one site."""

import uuid

import pytest

from tests.api.test_user_groups import PW, _group, _group_user, _login, _make_rack, _make_site

ADMIN_PERMS = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _delegated_admin(client, admin, site, *, rack_scope="all", rack_ids=(), perms=ADMIN_PERMS, email=None):
    """A restricted user who may administer users/groups but only holds `site`."""
    entry = {"site_id": site, "rack_scope": rack_scope, "rack_ids": list(rack_ids)}
    gid = await _group(client, admin, allow=perms, sites=[entry])
    user, headers = await _group_user(client, admin, [gid], email=email)
    return {"id": user["id"], "email": user["email"], "headers": headers, "group": gid}


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    rack_a1 = await _make_rack(client, admin, auth_headers, a["room"])
    rack_a2 = await _make_rack(client, admin, auth_headers, a["room"])
    rack_b = await _make_rack(client, admin, auth_headers, b["room"])
    return {"a": a, "b": b, "rack_a1": rack_a1, "rack_a2": rack_a2, "rack_b": rack_b}


# --------------------------------------------------------------- SEC-RBAC-59-01: peer / wider-scope administration
async def test_site_a_admin_cannot_reset_password_of_site_b_admin(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    admin_b = await _delegated_admin(client, admin, world["b"]["site"])
    resp = await client.patch(f"/api/v1/users/{admin_b['id']}", json={"password": "attacker-chosen-pass-1"}, headers=admin_a["headers"])
    assert resp.status_code in (403, 404), resp.text
    # the victim's own password still works
    assert (await client.post("/api/v1/auth/login", json={"email": admin_b["email"], "password": PW})).status_code == 200


async def test_site_a_admin_cannot_deactivate_or_delete_site_b_admin(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    admin_b = await _delegated_admin(client, admin, world["b"]["site"])
    r1 = await client.patch(f"/api/v1/users/{admin_b['id']}", json={"is_active": False}, headers=admin_a["headers"])
    r2 = await client.delete(f"/api/v1/users/{admin_b['id']}", headers=admin_a["headers"])
    assert r1.status_code in (403, 404) and r2.status_code in (403, 404), (r1.text, r2.text)


async def test_site_a_admin_cannot_administer_user_with_wider_scope(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    both = await _group(
        client, admin, allow=["rack:read"],
        sites=[{"site_id": world["a"]["site"], "rack_scope": "all"}, {"site_id": world["b"]["site"], "rack_scope": "all"}],
    )
    wide, _ = await _group_user(client, admin, [both])
    resp = await client.patch(f"/api/v1/users/{wide['id']}", json={"password": "attacker-chosen-pass-1"}, headers=admin_a["headers"])
    assert resp.status_code in (403, 404), resp.text


# --------------------------------------------------------------- SEC-RBAC-59-02: group route bypasses the outrank check
async def test_restricted_admin_cannot_lock_out_global_admin_via_deny_group(client, admin, world, auth_headers):
    delegated = await _delegated_admin(client, admin, world["a"]["site"])
    victim_email = f"global-{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post(
        "/api/v1/users", json={"email": victim_email, "full_name": "Global", "password": PW, "role_name": "Administrator"}, headers=admin
    )
    assert r.status_code == 201, r.text
    victim_id = r.json()["id"]
    trap = (await client.post("/api/v1/groups", json={"name": f"trap-{uuid.uuid4().hex[:6]}"}, headers=delegated["headers"])).json()["id"]
    perms = await client.put(f"/api/v1/groups/{trap}/permissions", json={"allow": [], "deny": ["user:manage", "group:manage", "rack:read"]}, headers=delegated["headers"])
    assert perms.status_code == 200, perms.text
    add = await client.put(f"/api/v1/groups/{trap}/members", json={"user_ids": [victim_id]}, headers=delegated["headers"])
    assert add.status_code in (403, 404), add.text
    victim = await _login(client, victim_email)
    assert (await client.get("/api/v1/users", headers=victim)).status_code == 200


async def test_group_route_membership_change_requires_outranking_the_target_user(client, admin, world):
    delegated = await _delegated_admin(client, admin, world["a"]["site"])
    other = await _delegated_admin(client, admin, world["b"]["site"])
    grp = (await client.post("/api/v1/groups", json={"name": f"g-{uuid.uuid4().hex[:6]}"}, headers=delegated["headers"])).json()["id"]
    add = await client.put(f"/api/v1/groups/{grp}/members", json={"user_ids": [other["id"]]}, headers=delegated["headers"])
    assert add.status_code in (403, 404), add.text


# --------------------------------------------------------------- SEC-RBAC-59-03: rack-scope widening
async def test_rack_limited_admin_cannot_assign_a_group_with_wider_rack_scope(client, admin, world):
    limited = await _delegated_admin(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    wide_group = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": world["a"]["site"], "rack_scope": "all"}])
    resp = await client.post(
        "/api/v1/users",
        json={"email": f"n-{uuid.uuid4().hex[:6]}@example.com", "full_name": "N", "password": PW, "group_ids": [wide_group]},
        headers=limited["headers"],
    )
    assert resp.status_code == 403, resp.text


async def test_rack_limited_admin_cannot_add_self_to_wider_group_through_group_route(client, admin, world):
    limited = await _delegated_admin(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    wide_group = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": world["a"]["site"], "rack_scope": "all"}])
    resp = await client.put(f"/api/v1/groups/{wide_group}/members", json={"user_ids": [limited["id"]]}, headers=limited["headers"])
    assert resp.status_code in (403, 404), resp.text
    seen = await client.get(f"/api/v1/racks/{world['rack_a2']}", headers=limited["headers"])
    assert seen.status_code == 404


async def test_rack_limited_admin_cannot_grant_all_racks_on_a_group(client, admin, world):
    limited = await _delegated_admin(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    grp = (await client.post("/api/v1/groups", json={"name": f"g-{uuid.uuid4().hex[:6]}"}, headers=limited["headers"])).json()["id"]
    resp = await client.put(
        f"/api/v1/groups/{grp}/site-access", json={"sites": [{"site_id": world["a"]["site"], "rack_scope": "all"}]}, headers=limited["headers"]
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------- SEC-RBAC-59-04: cross-site group administration
async def test_site_a_admin_cannot_modify_or_delete_a_site_b_group(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    admin_b = await _delegated_admin(client, admin, world["b"]["site"])
    target = admin_b["group"]
    denied = {
        "rename": await client.patch(f"/api/v1/groups/{target}", json={"name": "pwned"}, headers=admin_a["headers"]),
        "deny": await client.put(f"/api/v1/groups/{target}/permissions", json={"allow": [], "deny": ["user:manage"]}, headers=admin_a["headers"]),
        "sites": await client.put(f"/api/v1/groups/{target}/site-access", json={"sites": []}, headers=admin_a["headers"]),
        "members": await client.put(f"/api/v1/groups/{target}/members", json={"user_ids": []}, headers=admin_a["headers"]),
        "delete": await client.delete(f"/api/v1/groups/{target}", headers=admin_a["headers"]),
    }
    for name, resp in denied.items():
        assert resp.status_code in (403, 404), (name, resp.status_code, resp.text)
    # Site B's administrator still holds access
    assert (await client.get(f"/api/v1/groups/{target}", headers=admin["headers"] if False else admin_b["headers"])).status_code == 200


# --------------------------------------------------------------- SEC-RBAC-59-05: cross-scope disclosure to restricted admins
async def test_restricted_admin_cannot_read_other_sites_grants(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    admin_b = await _delegated_admin(client, admin, world["b"]["site"], rack_scope="selected", rack_ids=[world["rack_b"]])
    detail = (await client.get(f"/api/v1/groups/{admin_b['group']}", headers=admin_a["headers"]))
    if detail.status_code == 200:
        assert world["b"]["site"] not in {s["site_id"] for s in detail.json()["sites"]}
        assert world["rack_b"] not in {r for s in detail.json()["sites"] for r in s["rack_ids"]}
    else:
        assert detail.status_code == 404
    eff = await client.get(f"/api/v1/users/{admin_b['id']}/effective-access", headers=admin_a["headers"])
    if eff.status_code == 200:
        assert world["b"]["site"] not in {s["site_id"] for s in eff.json()["sites"]}
    else:
        assert eff.status_code in (403, 404)


# --------------------------------------------------------------- positive controls: legitimate delegation still works
async def test_delegated_admin_can_administer_lower_ranked_users_inside_their_scope(client, admin, world):
    admin_a = await _delegated_admin(client, admin, world["a"]["site"])
    reader_group = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": world["a"]["site"], "rack_scope": "all"}])
    reader, _ = await _group_user(client, admin, [reader_group])
    reset = await client.patch(f"/api/v1/users/{reader['id']}", json={"password": "a-new-long-password-1"}, headers=admin_a["headers"])
    assert reset.status_code == 200, reset.text
    team = (await client.post("/api/v1/groups", json={"name": f"team-{uuid.uuid4().hex[:6]}"}, headers=admin_a["headers"])).json()["id"]
    assert (await client.put(f"/api/v1/groups/{team}/members", json={"user_ids": [reader["id"]]}, headers=admin_a["headers"])).status_code == 200
    assert (await client.put(f"/api/v1/groups/{team}/site-access", json={"sites": [{"site_id": world["a"]["site"], "rack_scope": "all"}]}, headers=admin_a["headers"])).status_code == 200
    assert (await client.delete(f"/api/v1/groups/{team}", headers=admin_a["headers"])).status_code == 204


async def test_rack_scope_delegation_within_own_scope_is_allowed(client, admin, world):
    limited = await _delegated_admin(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"], world["rack_a2"]])
    subset = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": world["a"]["site"], "rack_scope": "selected", "rack_ids": [world["rack_a1"]]}])
    ok = await client.post(
        "/api/v1/users", json={"email": f"n-{uuid.uuid4().hex[:6]}@example.com", "full_name": "N", "password": PW, "group_ids": [subset]},
        headers=limited["headers"],
    )
    assert ok.status_code == 201, ok.text
    full = await _delegated_admin(client, admin, world["a"]["site"], rack_scope="all")
    narrower = await client.post(
        "/api/v1/users", json={"email": f"m-{uuid.uuid4().hex[:6]}@example.com", "full_name": "M", "password": PW, "group_ids": [subset]},
        headers=full["headers"],
    )
    assert narrower.status_code == 201, narrower.text


async def test_unrestricted_administrator_is_unaffected(client, admin, world):
    admin_b = await _delegated_admin(client, admin, world["b"]["site"])
    assert (await client.patch(f"/api/v1/users/{admin_b['id']}", json={"password": "another-long-password-2"}, headers=admin)).status_code == 200
    detail = (await client.get(f"/api/v1/groups/{admin_b['group']}", headers=admin)).json()
    assert world["b"]["site"] in {s["site_id"] for s in detail["sites"]}
