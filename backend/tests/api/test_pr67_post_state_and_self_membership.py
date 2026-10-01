"""PR #67 remediation: resulting-authority validation and the self-membership prohibition.

Blocker 1 (deny removal): the strict pre-mutation check only proves the target is below the actor NOW.
Removing a deny membership, a deny grant or a whole deny group restores authority the target already
held through an allow group, which can exceed the actor. Every path must validate the RESULTING
effective authority of every surviving affected principal before commit, and roll everything back
(membership/grant rows, password and token changes, audit rows) on failure.

Blocker 3 (self-addition): the actor is not yet a member when the membership replacement is checked,
and the outrank check skips the actor, so a group route could add the actor to a deny-only group.

Every test drives the real API against PostgreSQL with persisted sites, groups and users."""


import pytest
from sqlalchemy import text

from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]
ALLOW_WIDE = [*BASE, "equipment:read"]  # the actor does NOT hold equipment:read
DENIED = ["equipment:read", "rack:manage"]
NEW_PW = "attacker-chosen-pass-1"


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    return {"a": a["site"], "b": b["site"], "rack_a1": await _make_rack(client, admin, auth_headers, a["room"])}


def _site(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


async def _effective(client, admin, user_id):
    eff = (await client.get(f"/api/v1/users/{user_id}/effective-access", headers=admin)).json()
    return set(eff["permissions"]), {s["site_id"] for s in eff["sites"]}


async def _audit_rows(admin_engine, actor_id):
    async with admin_engine.connect() as conn:
        rows = await conn.execute(text("SELECT action FROM audit_log WHERE actor_user_id = :a"), {"a": actor_id})
        return sorted(r[0] for r in rows)


async def _deny_world(client, admin, world):
    """Actor holds BASE (no equipment:read). Target is in an allow group (BASE + equipment:read) and a
    deny group (equipment:read, rack:manage), same site scope: target = BASE minus rack:manage, which is
    strictly below the actor, so the pre-mutation check passes. Removing the deny restores authority
    that exceeds the actor."""
    site = [_site(world["a"])]
    actor_group = await _group(client, admin, allow=BASE, sites=site)
    actor, actor_headers = await _group_user(client, admin, [actor_group])
    g_allow = await _group(client, admin, allow=ALLOW_WIDE, sites=site)
    g_deny = await _group(client, admin, deny=DENIED, sites=site)
    target, target_headers = await _group_user(client, admin, [g_allow, g_deny])
    assert (await _effective(client, admin, actor["id"]))[0] == set(BASE)
    perms, sites = await _effective(client, admin, target["id"])
    assert perms == set(BASE) - {"rack:manage"} and sites == {world["a"]}, "precondition: target strictly below actor"
    return {
        "actor": actor, "headers": actor_headers, "target": target, "target_headers": target_headers,
        "g_allow": g_allow, "g_deny": g_deny,
    }


DENY_REMOVAL_PATHS = {
    "user-membership-replacement": lambda c, w: c.patch(
        f"/api/v1/users/{w['target']['id']}", json={"group_ids": [w["g_allow"]]}, headers=w["headers"]),
    "group-member-removal": lambda c, w: c.put(
        f"/api/v1/groups/{w['g_deny']}/members", json={"user_ids": []}, headers=w["headers"]),
    "group-permission-replacement": lambda c, w: c.put(
        f"/api/v1/groups/{w['g_deny']}/permissions", json={"allow": [], "deny": []}, headers=w["headers"]),
    "group-deletion": lambda c, w: c.delete(f"/api/v1/groups/{w['g_deny']}", headers=w["headers"]),
}


# ------------------------------------------------------------------ Blocker 1
@pytest.mark.parametrize("path", sorted(DENY_REMOVAL_PATHS))
async def test_removing_a_deny_that_would_restore_authority_above_the_actor_is_rejected_and_rolled_back(
    client, admin, world, _admin_engine, path
):
    w = await _deny_world(client, admin, world)
    before_perms = (await _effective(client, admin, w["target"]["id"]))[0]

    resp = await DENY_REMOVAL_PATHS[path](client, w)
    assert resp.status_code == 403, resp.text

    # nothing leaked: the target never gained equipment:read, the deny group is intact and still holds the target
    assert (await _effective(client, admin, w["target"]["id"]))[0] == before_perms
    detail = (await client.get(f"/api/v1/groups/{w['g_deny']}", headers=admin)).json()
    assert sorted(detail["deny_permissions"]) == sorted(DENIED) and w["target"]["id"] in detail["member_ids"]
    user = (await client.get(f"/api/v1/users/{w['target']['id']}", headers=admin)).json()
    assert {g["id"] for g in user["groups"]} == {w["g_allow"], w["g_deny"]}
    # the rejected attempt left no audit row behind
    mutating = {"user.update", "group.members.update", "group.permissions.update", "group.delete"}
    assert not mutating & set(await _audit_rows(_admin_engine, w["actor"]["id"]))


async def test_password_reset_combined_with_membership_replacement_is_rolled_back_together(client, admin, world, _admin_engine):
    """The actor controls the credentials it sets: if the membership half were refused after the
    password half was flushed (or committed), the actor could log in as a principal that now exceeds it."""
    w = await _deny_world(client, admin, world)
    email = (await client.get(f"/api/v1/users/{w['target']['id']}", headers=admin)).json()["email"]
    resp = await client.patch(
        f"/api/v1/users/{w['target']['id']}", json={"password": NEW_PW, "group_ids": [w["g_allow"]]}, headers=w["headers"]
    )
    assert resp.status_code == 403, resp.text
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": NEW_PW})).status_code == 401
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": PW})).status_code == 200
    async with _admin_engine.connect() as conn:
        revoked = (
            await conn.execute(
                text("SELECT count(*) FROM refresh_token WHERE user_id = :u AND revoked_at IS NOT NULL"), {"u": w["target"]["id"]}
            )
        ).scalar_one()
    assert revoked == 0, "refresh tokens were revoked by a request that was refused"
    assert not {"user.update"} & set(await _audit_rows(_admin_engine, w["actor"]["id"]))


async def test_deny_removal_group_with_mixed_members_is_rejected_atomically(client, admin, world):
    """Two members: the first would exceed the actor, the second would only reach equality. The whole
    change is refused and the second member keeps its deny too (no partial application)."""
    w = await _deny_world(client, admin, world)
    g_base = await _group(client, admin, allow=BASE, sites=[_site(world["a"])])
    other, _ = await _group_user(client, admin, [g_base, w["g_deny"]])
    assert (await _effective(client, admin, other["id"]))[0] == set(BASE) - {"rack:manage"}
    resp = await DENY_REMOVAL_PATHS["group-permission-replacement"](client, w)
    assert resp.status_code == 403, resp.text
    assert (await _effective(client, admin, other["id"]))[0] == set(BASE) - {"rack:manage"}
    detail = (await client.get(f"/api/v1/groups/{w['g_deny']}", headers=admin)).json()
    assert sorted(detail["deny_permissions"]) == sorted(DENIED) and set(detail["member_ids"]) == {w["target"]["id"], other["id"]}


@pytest.mark.parametrize("surface", ["membership-replacement", "member-removal", "permission-replacement", "deletion"])
async def test_each_removal_path_permits_a_result_equal_to_the_actor(client, admin, world, surface):
    """Positive control for each of the four paths, and legitimate equal-authority delegation: the target
    is BASE minus rack:manage (strictly below), and removing the deny leaves it exactly equal to the actor."""
    site = [_site(world["a"])]
    g_actor = await _group(client, admin, allow=BASE, sites=site)
    _, h = await _group_user(client, admin, [g_actor])
    g_allow = await _group(client, admin, allow=BASE, sites=site)
    g_deny = await _group(client, admin, deny=["rack:manage"], sites=site)
    z, _ = await _group_user(client, admin, [g_allow, g_deny])
    assert (await _effective(client, admin, z["id"]))[0] == set(BASE) - {"rack:manage"}
    if surface == "membership-replacement":
        resp = await client.patch(f"/api/v1/users/{z['id']}", json={"group_ids": [g_allow]}, headers=h)
    elif surface == "member-removal":
        resp = await client.put(f"/api/v1/groups/{g_deny}/members", json={"user_ids": []}, headers=h)
    elif surface == "permission-replacement":
        resp = await client.put(f"/api/v1/groups/{g_deny}/permissions", json={"allow": [], "deny": []}, headers=h)
    else:
        resp = await client.delete(f"/api/v1/groups/{g_deny}", headers=h)
    assert resp.status_code in (200, 204), resp.text
    assert (await _effective(client, admin, z["id"]))[0] == set(BASE)


# ------------------------------------------------------------------ Blocker 3
async def _delegate(client, admin, world):
    site = [_site(world["a"])]
    group = await _group(client, admin, allow=BASE, sites=site)
    user, headers = await _group_user(client, admin, [group])
    ok_group = await _group(client, admin, allow=["rack:read"], sites=site)
    deny_only = await _group(client, admin, deny=["user:manage"], sites=site)
    low, _ = await _group_user(client, admin, [await _group(client, admin, allow=["rack:read"], sites=site)])
    return {"user": user, "headers": headers, "group": group, "ok_group": ok_group, "deny_only": deny_only, "low": low}


async def test_group_route_refuses_adding_the_actor_to_an_ordinary_group(client, admin, world):
    d = await _delegate(client, admin, world)
    resp = await client.put(f"/api/v1/groups/{d['ok_group']}/members", json={"user_ids": [d["user"]["id"]]}, headers=d["headers"])
    assert resp.status_code == 403, resp.text
    assert (await client.get(f"/api/v1/groups/{d['ok_group']}", headers=admin)).json()["member_ids"] == []


async def test_group_route_refuses_adding_the_actor_to_a_deny_only_group_and_keeps_its_authority(client, admin, world):
    d = await _delegate(client, admin, world)
    resp = await client.put(f"/api/v1/groups/{d['deny_only']}/members", json={"user_ids": [d["user"]["id"]]}, headers=d["headers"])
    assert resp.status_code == 403, resp.text
    assert (await _effective(client, admin, d["user"]["id"]))[0] == set(BASE)
    # the actor can still act: administer the subordinate user
    ok = await client.patch(f"/api/v1/users/{d['low']['id']}", json={"full_name": "Still Works"}, headers=d["headers"])
    assert ok.status_code == 200, ok.text


async def test_group_route_refuses_a_mixed_request_that_includes_the_actor(client, admin, world):
    d = await _delegate(client, admin, world)
    resp = await client.put(
        f"/api/v1/groups/{d['deny_only']}/members", json={"user_ids": [d["low"]["id"], d["user"]["id"]]}, headers=d["headers"]
    )
    assert resp.status_code == 403, resp.text
    assert (await client.get(f"/api/v1/groups/{d['deny_only']}", headers=admin)).json()["member_ids"] == []


@pytest.mark.parametrize("surface", ["group-route-empty", "group-route-others-only", "user-route"])
async def test_actor_cannot_remove_itself_from_a_group(client, admin, world, surface):
    d = await _delegate(client, admin, world)
    member_of = await _group(client, admin, allow=["rack:read"], sites=[_site(world["a"])])
    assert (
        await client.put(f"/api/v1/groups/{member_of}/members", json={"user_ids": [d["user"]["id"]]}, headers=admin)
    ).status_code == 200
    if surface == "group-route-empty":
        resp = await client.put(f"/api/v1/groups/{member_of}/members", json={"user_ids": []}, headers=d["headers"])
    elif surface == "group-route-others-only":
        resp = await client.put(f"/api/v1/groups/{member_of}/members", json={"user_ids": [d["low"]["id"]]}, headers=d["headers"])
    else:
        resp = await client.patch(f"/api/v1/users/{d['user']['id']}", json={"group_ids": [d["group"]]}, headers=d["headers"])
    assert resp.status_code == 403, resp.text
    assert d["user"]["id"] in (await client.get(f"/api/v1/groups/{member_of}", headers=admin)).json()["member_ids"]


async def test_user_route_refuses_the_actor_adding_itself_to_a_group(client, admin, world):
    d = await _delegate(client, admin, world)
    resp = await client.patch(
        f"/api/v1/users/{d['user']['id']}", json={"group_ids": [d["group"], d["deny_only"]]}, headers=d["headers"]
    )
    assert resp.status_code == 403, resp.text
    assert (await _effective(client, admin, d["user"]["id"]))[0] == set(BASE)


async def test_unrestricted_administrator_cannot_add_itself_to_a_deny_group(client, admin, auth_headers):
    """The original last-administrator test relied on exactly this self-addition."""
    root = await auth_headers("Administrator")
    root_id = (await client.get("/api/v1/auth/me", headers=root)).json()["id"]
    denier = await _group(client, root, deny=["group:manage"])
    resp = await client.put(f"/api/v1/groups/{denier}/members", json={"user_ids": [root_id]}, headers=root)
    assert resp.status_code == 403, resp.text
    assert (await client.get(f"/api/v1/groups/{denier}", headers=root)).json()["member_ids"] == []


async def test_self_profile_and_password_changes_are_still_permitted(client, admin, world):
    d = await _delegate(client, admin, world)
    email = d["user"]["email"]
    resp = await client.patch(
        f"/api/v1/users/{d['user']['id']}", json={"full_name": "Renamed Self", "password": "a-new-long-password-9"}, headers=d["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": "a-new-long-password-9"})).status_code == 200
    assert (await _effective(client, admin, d["user"]["id"]))[0] == set(BASE)
