"""Strict-outranking rule for delegated administration (PR #67 review finding).

A delegated administrator may change another principal's effective authority only when they
STRICTLY outrank it: target permissions <= actor permissions, target site/rack scope within the
actor's, and the two are not identical. Authority is a partial order (permission-set inclusion x
scope containment), so incomparable principals are refused. Every test builds persisted sites,
racks, groups and users and sends valid payloads; a 403 therefore comes from the authority rule,
not from a malformed request."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.rbac import get_auth_context
from app.application.user_admin_service import begin_authority_change
from app.core.errors import ForbiddenError
from app.domain.auth.models import User, UserGroupMember
from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    return {
        "a": a["site"],
        "b": b["site"],
        "rack_a1": await _make_rack(client, admin, auth_headers, a["room"]),
        "rack_a2": await _make_rack(client, admin, auth_headers, a["room"]),
    }


def _entry(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


async def _principal(client, admin, perms, sites):
    """A persisted user whose only authority comes from its own fresh group."""
    gid = await _group(client, admin, allow=perms, sites=sites)
    user, headers = await _group_user(client, admin, [gid])
    return {"id": user["id"], "headers": headers, "group": gid}


def _denied(resp):
    assert resp.status_code == 403, resp.text


# ------------------------------------------------------------------ equal-authority peer
async def test_equal_authority_peer_cannot_be_administered(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    other = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    _denied(await client.patch(f"/api/v1/users/{b['id']}", json={"password": "attacker-chosen-pass-1"}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/users/{b['id']}", json={"is_active": False}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/users/{b['id']}", json={"group_ids": [b["group"], other]}, headers=a["headers"]))
    _denied(await client.delete(f"/api/v1/users/{b['id']}", headers=a["headers"]))
    # the peer keeps working with the original password
    assert (await client.post("/api/v1/auth/login", json={"email": (await client.get(f"/api/v1/users/{b['id']}", headers=admin)).json()["email"], "password": PW})).status_code == 200


async def test_equal_authority_peer_group_cannot_be_changed_by_the_other_admin(client, admin, world):
    """A and B have identical permissions and scope; B sits in a separate group A is not in."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    g = b["group"]
    _denied(await client.put(f"/api/v1/groups/{g}/permissions", json={"allow": [], "deny": ["user:manage", "group:manage"]}, headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{g}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{g}/site-access", json={"sites": []}, headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{g}/members", json={"user_ids": []}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/groups/{g}", json={"name": "renamed-by-peer"}, headers=a["headers"]))
    _denied(await client.delete(f"/api/v1/groups/{g}", headers=a["headers"]))
    detail = (await client.get(f"/api/v1/groups/{g}", headers=admin)).json()
    assert set(detail["allow_permissions"]) == set(BASE) and detail["name"] != "renamed-by-peer"


async def test_unrestricted_administrators_do_not_administer_each_other(client, admin, auth_headers):
    """Two global Administrators are peers as well: equality holds for unrestricted scope."""
    peer = await auth_headers("Administrator")
    peer_id = (await client.get("/api/v1/auth/me", headers=peer)).json()["id"]
    _denied(await client.patch(f"/api/v1/users/{peer_id}", json={"password": "attacker-chosen-pass-1"}, headers=admin))
    _denied(await client.delete(f"/api/v1/users/{peer_id}", headers=admin))


# ------------------------------------------------------------------ lower / higher / wider / incomparable
async def test_strictly_lower_permission_target_is_administrable(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    ok = await client.patch(f"/api/v1/users/{low['id']}", json={"password": "a-new-long-password-1"}, headers=a["headers"])
    assert ok.status_code == 200, ok.text


async def test_strictly_lower_scope_target_with_equal_permissions_is_administrable(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    narrow = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"]])])
    ok = await client.patch(f"/api/v1/users/{narrow['id']}", json={"password": "a-new-long-password-1"}, headers=a["headers"])
    assert ok.status_code == 200, ok.text


async def test_higher_authority_target_is_rejected(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    boss = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"]), _entry(world["b"])])
    _denied(await client.patch(f"/api/v1/users/{boss['id']}", json={"password": "attacker-chosen-pass-1"}, headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{boss['group']}/permissions", json={"allow": [], "deny": ["user:manage"]}, headers=a["headers"]))


async def test_target_with_wider_permission_set_is_rejected(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    wide = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"])])
    _denied(await client.patch(f"/api/v1/users/{wide['id']}", json={"is_active": False}, headers=a["headers"]))


async def test_target_with_wider_resource_scope_is_rejected(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"]])])
    wide_racks = await _principal(client, admin, BASE, [_entry(world["a"], "selected", [world["rack_a1"], world["rack_a2"]])])
    wide_sites = await _principal(client, admin, ["rack:read"], [_entry(world["a"], "selected", [world["rack_a1"]]), _entry(world["b"])])
    for target in (wide_racks, wide_sites):
        _denied(await client.patch(f"/api/v1/users/{target['id']}", json={"password": "attacker-chosen-pass-1"}, headers=a["headers"]))


async def test_incomparable_permission_sets_are_rejected(client, admin, world):
    """A holds equipment:read, T holds organization:read; neither set contains the other."""
    a = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"])])
    t = await _principal(client, admin, [*BASE, "organization:read"], [_entry(world["a"])])
    _denied(await client.patch(f"/api/v1/users/{t['id']}", json={"password": "attacker-chosen-pass-1"}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/users/{a['id']}", json={"password": "attacker-chosen-pass-1"}, headers=t["headers"]))


async def test_incomparable_scope_and_permission_mix_is_rejected(client, admin, world):
    """T has fewer permissions but a scope A does not have: lower on one axis, higher on the other."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    t = await _principal(client, admin, ["rack:read"], [_entry(world["b"])])
    _denied(await client.patch(f"/api/v1/users/{t['id']}", json={"password": "attacker-chosen-pass-1"}, headers=a["headers"]))


# ------------------------------------------------------------------ groups holding an equal / higher member
async def test_group_with_equal_member_cannot_be_modified_even_when_grants_fit_actor_scope(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    shared = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    assert (await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": [low["id"], b["id"]]}, headers=admin)).status_code == 200
    _denied(await client.put(f"/api/v1/groups/{shared}/permissions", json={"allow": ["rack:read"], "deny": ["user:manage"]}, headers=a["headers"]))
    _denied(await client.put(f"/api/v1/groups/{shared}/site-access", json={"sites": []}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/groups/{shared}", json={"name": "taken-over"}, headers=a["headers"]))
    _denied(await client.delete(f"/api/v1/groups/{shared}", headers=a["headers"]))
    # dropping the peer from the group is also a change to the peer's authority
    _denied(await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": [low["id"]]}, headers=a["headers"]))


async def test_group_with_higher_member_cannot_be_modified(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    boss = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"]), _entry(world["b"])])
    shared = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    assert (await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": [boss["id"]]}, headers=admin)).status_code == 200
    _denied(await client.put(f"/api/v1/groups/{shared}/permissions", json={"allow": [], "deny": ["rack:read", "user:read"]}, headers=a["headers"]))


# ------------------------------------------------------------------ membership paths
async def test_adding_an_equal_authority_peer_to_a_group_is_rejected(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    b = await _principal(client, admin, BASE, [_entry(world["a"])])
    trap = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    _denied(await client.put(f"/api/v1/groups/{trap}/members", json={"user_ids": [b["id"]]}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/users/{b['id']}", json={"group_ids": [b["group"], trap]}, headers=a["headers"]))


async def test_membership_that_would_widen_authority_beyond_the_actor_is_rejected(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    low = await _principal(client, admin, ["rack:read"], [_entry(world["a"])])
    more_perms = await _group(client, admin, allow=["rack:read", "equipment:read"], sites=[_entry(world["a"])])
    more_scope = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"]), _entry(world["b"])])
    for gid in (more_perms, more_scope):
        _denied(await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": [low["id"]]}, headers=a["headers"]))
        _denied(await client.patch(f"/api/v1/users/{low['id']}", json={"group_ids": [low["group"], gid]}, headers=a["headers"]))
    # the target's authority is unchanged
    eff = (await client.get(f"/api/v1/users/{low['id']}/effective-access", headers=admin)).json()
    assert set(eff["permissions"]) == {"rack:read"} and {s["site_id"] for s in eff["sites"]} == {world["a"]}


async def test_cannot_remove_a_higher_authority_member_from_a_group(client, admin, world):
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    boss = await _principal(client, admin, [*BASE, "equipment:read"], [_entry(world["a"])])
    extra = await _group(client, admin, allow=["rack:read"], sites=[_entry(world["a"])])
    assert (await client.put(f"/api/v1/groups/{extra}/members", json={"user_ids": [boss["id"]]}, headers=admin)).status_code == 200
    _denied(await client.put(f"/api/v1/groups/{extra}/members", json={"user_ids": []}, headers=a["headers"]))
    _denied(await client.patch(f"/api/v1/users/{boss['id']}", json={"group_ids": [boss["group"]]}, headers=a["headers"]))


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


# ------------------------------------------------------------------ TOCTOU / concurrency
async def test_authority_is_reevaluated_after_the_serialising_lock(client, admin, world, db_session):
    """A request resolves its AuthContext first; a change committed before the mutation runs
    must be visible to it. `begin_authority_change` re-reads the actor under the lock."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    actor = (await db_session.execute(select(User).where(User.id == uuid.UUID(a["id"])))).scalar_one()
    stale = await get_auth_context(actor, db_session)
    assert "user:manage" in stale.permission_codes
    # the actor's only group is deleted by a more senior administrator
    assert (await client.delete(f"/api/v1/groups/{a['group']}", headers=admin)).status_code == 204
    with pytest.raises(ForbiddenError):
        await begin_authority_change(db_session, stale, "user:manage")
    await db_session.rollback()


async def test_authority_changing_mutations_are_serialised(client, admin, world, db_engine):
    """Two transactions cannot both be inside the authority-change section: the second blocks
    until the first commits and then sees its committed effects (here, the actor losing rights)."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as s1, factory() as s2:
        actor = (await s1.execute(select(User).where(User.id == uuid.UUID(a["id"])))).scalar_one()
        ctx = await get_auth_context(actor, s1)
        await begin_authority_change(s1, ctx, "user:manage")  # s1 holds the lock

        waiter = asyncio.create_task(begin_authority_change(s2, ctx, "user:manage"))
        await asyncio.sleep(0.5)
        assert not waiter.done(), "second mutation entered the authority section while the first held the lock"

        member = await s1.get(UserGroupMember, (uuid.UUID(a["group"]), uuid.UUID(a["id"])))
        await s1.delete(member)
        await s1.commit()  # releases the lock

        with pytest.raises(ForbiddenError):
            await asyncio.wait_for(waiter, timeout=5)
        await s2.rollback()
