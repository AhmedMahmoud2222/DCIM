"""Strict-outranking rule for delegated administration (PR #67 review finding).

Effective authority is the pair (permission set, site/rack scope), a partial order. An actor may
administer an existing principal only when target <= actor AND NOT actor <= target. Equal (peer),
wider and incomparable principals are refused. Creation and delegation may confer authority up to,
and including, the actor's own.

Every case is its own parametrized test (one route per test) so that an early assertion cannot mask
an untested route when the rule is mutated. Each test builds persisted sites, racks, groups and users
and sends valid payloads: a 403 therefore comes from the authority rule, not from a malformed request.
Concurrency tests live in tests/integration/test_pr67_authority_*.py; the two tests that call the
lock helper directly live in tests/integration/test_pr67_authority_lock_protocol.py so that this
file is pure HTTP and runs unchanged against #67's own head."""

import uuid

import pytest

from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]
NEW_PW = "attacker-chosen-pass-1"


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    return {
        "room_a": a["room"],
        "a": a["site"],
        "b": b["site"],
        "rack_a1": await _make_rack(client, admin, auth_headers, a["room"]),
        "rack_a2": await _make_rack(client, admin, auth_headers, a["room"]),
    }


def _entry(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


async def _principal(client, admin, perms, sites, *, extra_groups=()):
    """A persisted user whose authority comes from its own fresh group (plus any extra groups)."""
    gid = await _group(client, admin, allow=perms, sites=sites)
    user, headers = await _group_user(client, admin, [gid, *extra_groups])
    return {"id": user["id"], "email": user["email"], "headers": headers, "group": gid}


def _denied(resp):
    assert resp.status_code == 403, resp.text


async def _effective(client, admin, user_id):
    eff = (await client.get(f"/api/v1/users/{user_id}/effective-access", headers=admin)).json()
    return set(eff["permissions"]), {s["site_id"] for s in eff["sites"]}


# ------------------------------------------------------------------ one callable per mutating route
USER_ROUTES = {
    "password": lambda c, h, u, g: c.patch(f"/api/v1/users/{u}", json={"password": NEW_PW}, headers=h),
    "deactivate": lambda c, h, u, g: c.patch(f"/api/v1/users/{u}", json={"is_active": False}, headers=h),
    "rename": lambda c, h, u, g: c.patch(f"/api/v1/users/{u}", json={"full_name": "Renamed By Peer"}, headers=h),
    "regroup": lambda c, h, u, g: c.patch(f"/api/v1/users/{u}", json={"group_ids": []}, headers=h),
    "delete": lambda c, h, u, g: c.delete(f"/api/v1/users/{u}", headers=h),
}
GROUP_ROUTES = {
    "deny_permissions": lambda c, h, u, g: c.put(f"/api/v1/groups/{g}/permissions", json={"allow": [], "deny": ["user:manage"]}, headers=h),
    "allow_permissions": lambda c, h, u, g: c.put(f"/api/v1/groups/{g}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=h),
    "site_access": lambda c, h, u, g: c.put(f"/api/v1/groups/{g}/site-access", json={"sites": []}, headers=h),
    "members": lambda c, h, u, g: c.put(f"/api/v1/groups/{g}/members", json={"user_ids": []}, headers=h),
    "rename": lambda c, h, u, g: c.patch(f"/api/v1/groups/{g}", json={"name": f"renamed-by-peer-{uuid.uuid4().hex[:8]}"}, headers=h),
    "delete": lambda c, h, u, g: c.delete(f"/api/v1/groups/{g}", headers=h),
}
user_route = pytest.mark.parametrize("route", sorted(USER_ROUTES))
group_route = pytest.mark.parametrize("route", sorted(GROUP_ROUTES))


async def _assert_user_untouched(client, admin, target):
    me = (await client.get(f"/api/v1/users/{target['id']}", headers=admin)).json()
    assert me["is_active"] is True and me["full_name"] != "Renamed By Peer"
    assert (await client.post("/api/v1/auth/login", json={"email": target["email"], "password": PW})).status_code == 200
    assert (await client.post("/api/v1/auth/login", json={"email": target["email"], "password": NEW_PW})).status_code == 401
    assert target["group"] in {g["id"] for g in me["groups"]}


async def _assert_group_untouched(client, admin, group_id, *, perms, name_prefix="renamed-by-peer"):
    detail = (await client.get(f"/api/v1/groups/{group_id}", headers=admin)).json()
    assert set(detail["allow_permissions"]) == set(perms) and detail["deny_permissions"] == []
    assert not detail["name"].startswith(name_prefix) and len(detail["sites"]) >= 1


# ------------------------------------------------------------------ equal-authority peer
@user_route
async def test_equal_authority_peer_user_route_is_rejected(client, admin, world, route):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    _denied(await USER_ROUTES[route](client, a["headers"], b["id"], b["group"]))
    await _assert_user_untouched(client, admin, b)


@group_route
async def test_equal_authority_peer_group_route_is_rejected(client, admin, world, route):
    """A and B have identical permissions and scope; B sits in a separate group A is not in."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    _denied(await GROUP_ROUTES[route](client, a["headers"], b["id"], b["group"]))
    await _assert_group_untouched(client, admin, b["group"], perms=BASE)
    assert b["id"] in (await client.get(f"/api/v1/groups/{b['group']}", headers=admin)).json()["member_ids"]


@pytest.mark.parametrize("route", ["password", "deactivate", "delete"])
async def test_unrestricted_administrators_do_not_administer_each_other(client, admin, auth_headers, route):
    """Two global Administrators are peers as well: equality holds for unrestricted scope."""
    peer = await auth_headers("Administrator")
    peer_id = (await client.get("/api/v1/auth/me", headers=peer)).json()["id"]
    _denied(await USER_ROUTES[route](client, admin, peer_id, None))
    assert (await client.get(f"/api/v1/users/{peer_id}", headers=admin)).json()["is_active"] is True


# ------------------------------------------------------------------ lower / higher / wider / incomparable
# (id, actor permissions+scope, target permissions+scope, expected outcome)
LOWER = [
    ("fewer-permissions", lambda w: ([*BASE], [_entry(w["a"])]), lambda w: (["rack:read"], [_entry(w["a"])])),
    ("narrower-rack-scope", lambda w: ([*BASE], [_entry(w["a"])]), lambda w: ([*BASE], [_entry(w["a"], "selected", [w["rack_a1"]])])),
    ("fewer-permissions-and-narrower-scope", lambda w: ([*BASE], [_entry(w["a"])]),
     lambda w: (["rack:read"], [_entry(w["a"], "selected", [w["rack_a1"]])])),
]
NOT_LOWER = [
    ("wider-permission-set", lambda w: ([*BASE], [_entry(w["a"])]), lambda w: ([*BASE, "equipment:read"], [_entry(w["a"])])),
    ("wider-rack-scope", lambda w: ([*BASE], [_entry(w["a"], "selected", [w["rack_a1"]])]),
     lambda w: ([*BASE], [_entry(w["a"], "selected", [w["rack_a1"], w["rack_a2"]])])),
    ("wider-site-scope", lambda w: ([*BASE], [_entry(w["a"], "selected", [w["rack_a1"]])]),
     lambda w: (["rack:read"], [_entry(w["a"], "selected", [w["rack_a1"]]), _entry(w["b"])])),
    ("higher-in-both", lambda w: ([*BASE], [_entry(w["a"])]),
     lambda w: ([*BASE, "equipment:read"], [_entry(w["a"]), _entry(w["b"])])),
    ("incomparable-permissions", lambda w: ([*BASE, "equipment:read"], [_entry(w["a"])]),
     lambda w: ([*BASE, "organization:read"], [_entry(w["a"])])),
    ("lower-permissions-but-other-site", lambda w: ([*BASE], [_entry(w["a"])]), lambda w: (["rack:read"], [_entry(w["b"])])),
    ("incomparable-scope", lambda w: ([*BASE], [_entry(w["a"])]), lambda w: ([*BASE], [_entry(w["b"])])),
]


@pytest.mark.parametrize("route", ["password", "deactivate", "rename", "regroup", "delete"])
@pytest.mark.parametrize("case", LOWER, ids=[c[0] for c in LOWER])
async def test_strictly_lower_target_is_administrable(client, admin, world, case, route):
    _, actor_spec, target_spec = case
    a = await _principal(client, admin, *actor_spec(world))
    t = await _principal(client, admin, *target_spec(world))
    resp = await USER_ROUTES[route](client, a["headers"], t["id"], t["group"])
    assert resp.status_code in (200, 204), resp.text


@user_route
@pytest.mark.parametrize("case", NOT_LOWER, ids=[c[0] for c in NOT_LOWER])
async def test_wider_higher_or_incomparable_target_user_route_is_rejected(client, admin, world, case, route):
    _, actor_spec, target_spec = case
    a = await _principal(client, admin, *actor_spec(world))
    t = await _principal(client, admin, *target_spec(world))
    _denied(await USER_ROUTES[route](client, a["headers"], t["id"], t["group"]))
    await _assert_user_untouched(client, admin, t)


@group_route
@pytest.mark.parametrize("case", NOT_LOWER, ids=[c[0] for c in NOT_LOWER])
async def test_wider_higher_or_incomparable_target_group_route_is_rejected(client, admin, world, case, route):
    _, actor_spec, target_spec = case
    a = await _principal(client, admin, *actor_spec(world))
    t = await _principal(client, admin, *target_spec(world))
    _denied(await GROUP_ROUTES[route](client, a["headers"], t["id"], t["group"]))
    assert t["id"] in (await client.get(f"/api/v1/groups/{t['group']}", headers=admin)).json()["member_ids"]


async def test_incomparability_is_symmetric(client, admin, world):
    a = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"])])
    t = await _principal(client, admin, [*BASE, "organization:read"], [_entry(world["a"])])
    _denied(await USER_ROUTES["password"](client, a["headers"], t["id"], None))
    _denied(await USER_ROUTES["password"](client, t["headers"], a["id"], None))


# ------------------------------------------------------------------ multiple groups
async def test_authority_is_the_union_across_all_of_a_users_groups(client, admin, world):
    """No single group of the target equals the actor, but the two together do: a peer."""
    half_1, half_2 = BASE[:4], BASE[4:]
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    g1 = await _group(client, admin, allow=half_1, sites=[_entry(world["a"])])
    g2 = await _group(client, admin, allow=half_2, sites=[_entry(world["a"])])
    both, _ = await _group_user(client, admin, [g1, g2])
    one, _ = await _group_user(client, admin, [g1])
    _denied(await USER_ROUTES["password"](client, a["headers"], both["id"], g1))
    assert (await USER_ROUTES["password"](client, a["headers"], one["id"], g1)).status_code == 200


# ------------------------------------------------------------------ groups holding an equal / higher member
async def _shared_group_with(client, admin, world, members):
    shared = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    assert (await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": members}, headers=admin)).status_code == 200
    return shared


@group_route
async def test_group_with_equal_member_cannot_be_modified_even_when_its_grants_fit_the_actor(client, admin, world, route):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    shared = await _shared_group_with(client, admin, world, [low["id"], b["id"]])
    _denied(await GROUP_ROUTES[route](client, a["headers"], b["id"], shared))
    detail = (await client.get(f"/api/v1/groups/{shared}", headers=admin)).json()
    assert set(detail["member_ids"]) == {low["id"], b["id"]} and detail["allow_permissions"] == ["rack:read"]


@group_route
async def test_group_with_higher_member_cannot_be_modified(client, admin, world, route):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    boss = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"]), _entry(world["b"])])
    shared = await _shared_group_with(client, admin, world, [boss["id"]])
    _denied(await GROUP_ROUTES[route](client, a["headers"], boss["id"], shared))
    assert (await client.get(f"/api/v1/groups/{shared}", headers=admin)).json()["member_ids"] == [boss["id"]]


@group_route
async def test_group_whose_only_members_are_strictly_lower_can_be_modified(client, admin, world, route):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    shared = await _shared_group_with(client, admin, world, [low["id"]])
    resp = await GROUP_ROUTES[route](client, a["headers"], low["id"], shared)
    assert resp.status_code in (200, 204), resp.text


# ------------------------------------------------------------------ membership paths
async def test_adding_an_equal_authority_peer_to_a_group_is_rejected_on_the_group_route(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    trap = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    _denied(await client.put(f"/api/v1/groups/{trap}/members", json={"user_ids": [b["id"]]}, headers=a["headers"]))
    assert (await client.get(f"/api/v1/groups/{trap}", headers=admin)).json()["member_ids"] == []


async def test_adding_an_equal_authority_peer_to_a_group_is_rejected_on_the_user_route(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    trap = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    _denied(await client.patch(f"/api/v1/users/{b['id']}", json={"group_ids": [b["group"], trap]}, headers=a["headers"]))
    assert (await client.get(f"/api/v1/groups/{trap}", headers=admin)).json()["member_ids"] == []


@pytest.mark.parametrize("surface", ["group-route", "user-route"])
@pytest.mark.parametrize("widening", ["more-permissions", "more-sites"])
async def test_membership_that_would_widen_authority_beyond_the_actor_is_rejected(client, admin, world, widening, surface):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    if widening == "more-permissions":
        gid = await _group(client, admin, allow=["rack:read", "equipment:read"], sites=[_entry(world["a"])])
    else:
        gid = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"]), _entry(world["b"])])
    if surface == "group-route":
        resp = await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": [low["id"]]}, headers=a["headers"])
    else:
        resp = await client.patch(f"/api/v1/users/{low['id']}", json={"group_ids": [low["group"], gid]}, headers=a["headers"])
    _denied(resp)
    perms, sites = await _effective(client, admin, low["id"])
    assert perms == {"rack:read"} and sites == {world["a"]}


@pytest.mark.parametrize("surface", ["group-route", "user-route"])
async def test_cannot_remove_a_higher_authority_member_from_a_group(client, admin, world, surface):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    boss = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"])])
    extra = await _shared_group_with(client, admin, world, [boss["id"]])
    if surface == "group-route":
        resp = await client.put(f"/api/v1/groups/{extra}/members", json={"user_ids": []}, headers=a["headers"])
    else:
        resp = await client.patch(f"/api/v1/users/{boss['id']}", json={"group_ids": [boss["group"]]}, headers=a["headers"])
    _denied(resp)
    assert boss["id"] in (await client.get(f"/api/v1/groups/{extra}", headers=admin)).json()["member_ids"]


# ------------------------------------------------------------------ equal-authority creation and what follows
async def test_creating_an_equal_authority_principal_succeeds(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    equal_group = await _group(client, admin, allow=BASE, sites=[_entry(world["a"])])
    created = await client.post(
        "/api/v1/users",
        json={"email": f"peer-{uuid.uuid4().hex[:8]}@example.com", "full_name": "New Peer", "password": PW, "group_ids": [equal_group]},
        headers=a["headers"],
    )
    assert created.status_code == 201, created.text
    perms, sites = await _effective(client, admin, created.json()["id"])
    assert perms == set(BASE) and sites == {world["a"]}


@user_route
async def test_the_actor_cannot_administer_the_peer_it_created(client, admin, world, route):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    equal_group = await _group(client, admin, allow=BASE, sites=[_entry(world["a"])])
    email = f"peer-{uuid.uuid4().hex[:8]}@example.com"
    created = (
        await client.post(
            "/api/v1/users",
            json={"email": email, "full_name": "New Peer", "password": PW, "group_ids": [equal_group]},
            headers=a["headers"],
        )
    ).json()
    _denied(await USER_ROUTES[route](client, a["headers"], created["id"], equal_group))
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": PW})).status_code == 200


async def test_a_group_created_for_a_new_peer_cannot_be_used_to_alter_an_existing_peer(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    existing = await _principal(client, admin, BASE, [_entry(world["a"])])
    new_group = (await client.post("/api/v1/groups", json={"name": f"mine-{uuid.uuid4().hex[:6]}"}, headers=a["headers"])).json()["id"]
    for resp in (
        await client.put(f"/api/v1/groups/{new_group}/members", json={"user_ids": [existing["id"]]}, headers=a["headers"]),
        await client.patch(f"/api/v1/users/{existing['id']}", json={"group_ids": [existing["group"], new_group]}, headers=a["headers"]),
        await client.put(f"/api/v1/groups/{existing['group']}/members", json={"user_ids": []}, headers=a["headers"]),
    ):
        _denied(resp)
    assert (await client.get(f"/api/v1/groups/{new_group}", headers=admin)).json()["member_ids"] == []


async def test_promoting_a_user_to_equal_authority_through_an_intermediary_group_cannot_be_undone_by_the_actor(client, admin, world):
    """Delegation up to the actor's own authority is allowed, and one-way: the promoted user is a peer."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"], "selected", [world["rack_a1"]])])
    inter = (await client.post("/api/v1/groups", json={"name": f"inter-{uuid.uuid4().hex[:6]}"}, headers=a["headers"])).json()["id"]
    h = a["headers"]
    assert (await client.put(f"/api/v1/groups/{inter}/members", json={"user_ids": [low["id"]]}, headers=h)).status_code == 200
    # permissions now equal the actor's, scope still narrower: low stays strictly below
    assert (await client.put(f"/api/v1/groups/{inter}/permissions", json={"allow": BASE, "deny": []}, headers=h)).status_code == 200
    # scope widened to the actor's: low is now equal in both
    assert (await client.put(f"/api/v1/groups/{inter}/site-access", json={"sites": [_entry(world["a"])]}, headers=h)).status_code == 200
    assert (await _effective(client, admin, low["id"]))[0] == set(BASE)
    # now a peer: every way back down is refused
    _denied(await client.put(f"/api/v1/groups/{inter}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=h))
    _denied(await client.put(f"/api/v1/groups/{inter}/members", json={"user_ids": []}, headers=h))
    _denied(await client.patch(f"/api/v1/users/{low['id']}", json={"group_ids": [low["group"]]}, headers=h))
    _denied(await client.delete(f"/api/v1/groups/{inter}", headers=h))
    assert (await _effective(client, admin, low["id"]))[0] == set(BASE)


async def test_intermediary_group_cannot_grant_more_than_the_actor_holds(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    inter = (await client.post("/api/v1/groups", json={"name": f"inter-{uuid.uuid4().hex[:6]}"}, headers=a["headers"])).json()["id"]
    h = a["headers"]
    assert (await client.put(f"/api/v1/groups/{inter}/members", json={"user_ids": [low["id"]]}, headers=h)).status_code == 200
    _denied(await client.put(f"/api/v1/groups/{inter}/permissions", json={"allow": [*BASE, "equipment:read"], "deny": []}, headers=h))
    _denied(await client.put(f"/api/v1/groups/{inter}/site-access", json={"sites": [_entry(world["a"]), _entry(world["b"])]}, headers=h))
    assert (await _effective(client, admin, low["id"]))[0] == {"rack:read"}


async def test_removing_and_recreating_a_group_cannot_shed_a_peer_member(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    peer = await _principal(client, admin, BASE, [_entry(world["a"])])
    shared = await _shared_group_with(client, admin, world, [peer["id"]])
    _denied(await client.delete(f"/api/v1/groups/{shared}", headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": []}, headers=a["headers"]))
    assert (await client.get(f"/api/v1/groups/{shared}", headers=admin)).json()["member_ids"] == [peer["id"]]


# ------------------------------------------------------------------ legitimate delegation
async def test_delegation_inside_a_strictly_subordinate_scope_is_permitted(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    reader = await _principal(client, admin, ["rack:read"], [_entry(world["a"], "selected", [world["rack_a1"]])])
    team = (await client.post("/api/v1/groups", json={"name": f"team-{uuid.uuid4().hex[:6]}"}, headers=a["headers"])).json()["id"]
    h = a["headers"]
    assert (await client.put(f"/api/v1/groups/{team}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=h)).status_code == 200
    assert (await client.put(f"/api/v1/groups/{team}/site-access", json={"sites": [_entry(world["a"], "selected", [world["rack_a1"]])]}, headers=h)).status_code == 200
    assert (await client.put(f"/api/v1/groups/{team}/members", json={"user_ids": [reader["id"]]}, headers=h)).status_code == 200
    assert (await client.patch(f"/api/v1/groups/{team}", json={"name": f"team2-{uuid.uuid4().hex[:6]}"}, headers=h)).status_code == 200
    assert (await client.patch(f"/api/v1/users/{reader['id']}", json={"is_active": False}, headers=h)).status_code == 200
    assert (await client.delete(f"/api/v1/groups/{team}", headers=h)).status_code == 204
    assert (await client.delete(f"/api/v1/users/{reader['id']}", headers=h)).status_code == 204


# ------------------------------------------------------------------ stale selected-rack grants (Codex review, P1)
async def test_a_stale_selected_rack_grant_does_not_make_a_peer_look_lower(client, admin, world, auth_headers):
    """The actor's group still lists a rack that has since moved to a site neither user holds. Neither can see it, so the
    two have identical effective authority and are peers: the actor must not be able to administer the other."""
    other = await _make_site(client, admin)
    moved = await _make_rack(client, admin, auth_headers, world["room_a"])
    actor = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"], moved])])
    twin = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"]])])
    lower = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"]])])
    # control: while the extra rack is still visible to the actor, the actor really is strictly above the other two
    assert (await USER_ROUTES["rename"](client, actor["headers"], lower["id"], lower["group"])).status_code == 200
    # the rack moves to a site nobody in this scenario holds
    mv = await client.post(f"/api/v1/racks/{moved}/move", json={"room_id": other["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin)
    assert mv.status_code == 200, mv.text
    assert (await _effective(client, admin, actor["id"])) == (await _effective(client, admin, twin["id"]))
    for route in ("password", "deactivate", "delete"):
        _denied(await USER_ROUTES[route](client, actor["headers"], twin["id"], twin["group"]))
    await _assert_user_untouched(client, admin, twin)


async def test_a_stale_group_rack_grant_cannot_be_conferred_by_an_actor_who_does_not_hold_it(client, admin, world, auth_headers):
    """Group G lists rack R2 in a site the actor reaches only through R1. R2 then moves to a site nobody here holds, so
    the grant is latent: it confers nothing today and revives when R2 returns. Normalising stale grants away must apply
    to the peer test only. Delegation still compares raw grants, so the actor cannot put a user into G."""
    other = await _make_site(client, admin)
    r2 = await _make_rack(client, admin, auth_headers, world["room_a"])
    g = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"], "selected", [r2])])
    actor = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"]])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"], "selected", [world["rack_a1"]])])
    mv = await client.post(f"/api/v1/racks/{r2}/move", json={"room_id": other["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin)
    assert mv.status_code == 200, mv.text
    # both delegation routes that would put `low` into G
    _denied(await client.put(f"/api/v1/groups/{g}/members", json={"user_ids": [low["id"]]}, headers=actor["headers"]))
    _denied(await client.patch(f"/api/v1/users/{low['id']}", json={"group_ids": [low["group"], g]}, headers=actor["headers"]))
    assert (await client.get(f"/api/v1/groups/{g}", headers=admin)).json()["member_ids"] == []
    # revival: the rack returns; `low` must still not see it
    back = await client.post(f"/api/v1/racks/{r2}/move", json={"room_id": world["room_a"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin)
    assert back.status_code == 200, back.text
    assert (await client.get(f"/api/v1/racks/{r2}", headers=low["headers"])).status_code in (403, 404)
