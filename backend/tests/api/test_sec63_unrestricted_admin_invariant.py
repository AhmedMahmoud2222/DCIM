"""SEC-63: only active, unrestricted effective administrators preserve the invariant.

Domain transactions reproduce the remaining helper defect. HTTP tests use a deliberately
invalid legacy state: strict outranking and self-edit guards prevent an API caller from
removing the sole unrestricted administrator from an initially valid state. No guard is
mocked or bypassed in the HTTP operation under test.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.access_control import active_administrator_ids, load_effective_access
from app.application.authority_lock import ADMIN_INVARIANT_LOCK_NAME, AUTHORITY_LOCK_NAME, acquire_authority_lock
from app.application.user_admin_service import assert_administrator_remains
from app.core.errors import ConflictError
from app.domain.audit.models import AuditLog
from app.domain.auth.models import (
    Permission,
    RefreshToken,
    Role,
    RoleAssignment,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
    UserGroupPermission,
    UserGroupRackAccess,
    UserGroupSiteAccess,
)
from app.domain.outbox.models import OutboxEvent
from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

ADMIN_PERMISSIONS = {"user:manage", "group:manage"}


@pytest.fixture
async def world(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    root_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])
    site = await _make_site(client, headers)
    group = await _group(
        client, headers,
        allow=["user:read", "user:manage", "group:read", "group:manage", "rack:read"],
        sites=[{"site_id": site["site"], "rack_scope": "all"}],
    )
    restricted, restricted_headers = await _group_user(client, headers, [group])
    return {
        "root": root_id, "headers": headers, "site": site,
        "restricted": uuid.UUID(restricted["id"]), "restricted_headers": restricted_headers,
    }


async def _qualifying(db):
    ids = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())
    access = await load_effective_access(db, ids)
    return {
        uid for uid, item in access.items()
        if item.scope.unrestricted and ADMIN_PERMISSIONS <= item.permission_codes
    }


async def _set_active(db, uid, active):
    user = await db.get(User, uid)
    user.is_active = active
    await db.flush()


@pytest.mark.parametrize("survivor", ["restricted", "inactive", "deny-user", "deny-group", "only-user", "only-group"])
async def test_deactivation_refused_without_a_qualifying_unrestricted_survivor(
    client, world, db_session, make_user, survivor,
):
    if survivor != "restricted":
        role = "Engineer" if survivor.startswith("only-") else "Administrator"
        extra = await make_user(f"survivor-{uuid.uuid4().hex}@example.com", PW, role)
        extra_id = extra.id
        if survivor == "inactive":
            await _set_active(db_session, extra_id, False)
        elif survivor.startswith("deny-"):
            denied = "user:manage" if survivor == "deny-user" else "group:manage"
            group_id = await _group(client, world["headers"], deny=[denied])
            db_session.add(UserGroupMember(group_id=uuid.UUID(group_id), user_id=extra_id))
        else:
            allowed = "user:manage" if survivor == "only-user" else "group:manage"
            group_id = await _group(client, world["headers"], allow=[allowed])
            db_session.add(UserGroupMember(group_id=uuid.UUID(group_id), user_id=extra_id))
        await db_session.commit()

    restricted = (await load_effective_access(db_session, [world["restricted"]]))[world["restricted"]]
    assert not restricted.scope.unrestricted
    assert ADMIN_PERMISSIONS <= restricted.permission_codes
    assert world["root"] in await _qualifying(db_session)

    # Real PostgreSQL mutation and the production authority lock/invariant. This is
    # a domain reproduction, not a claim that the HTTP outranking rule is bypassable.
    await acquire_authority_lock(db_session)
    await _set_active(db_session, world["root"], False)
    assert await _qualifying(db_session) == set()
    with pytest.raises(ConflictError):
        await assert_administrator_remains(db_session)
    await db_session.rollback()
    assert (await db_session.execute(select(User.is_active).where(User.id == world["root"]))).scalar_one()
    assert world["root"] in await _qualifying(db_session)


async def test_deactivation_commits_with_a_valid_unrestricted_survivor(world, db_session, make_user):
    survivor = await make_user(f"valid-{uuid.uuid4().hex}@example.com", PW, "Administrator")
    survivor_id = survivor.id
    await acquire_authority_lock(db_session)
    await _set_active(db_session, world["root"], False)
    await assert_administrator_remains(db_session)
    await db_session.commit()
    assert await _qualifying(db_session) == {survivor_id}
    assert set(await active_administrator_ids(db_session)) == {survivor_id}


@pytest.mark.parametrize("rack_scope", ["all", "selected"])
async def test_site_and_selected_rack_administrators_never_satisfy_invariant(
    client, world, auth_headers, db_session, rack_scope,
):
    racks = []
    if rack_scope == "selected":
        racks = [await _make_rack(client, world["headers"], auth_headers, world["site"]["room"])]
    group = await _group(
        client, world["headers"], allow=sorted(ADMIN_PERMISSIONS),
        sites=[{"site_id": world["site"]["site"], "rack_scope": rack_scope, "rack_ids": racks}],
    )
    user, _ = await _group_user(client, world["headers"], [group])
    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert ADMIN_PERMISSIONS <= access.permission_codes
    assert not access.scope.unrestricted
    assert uid not in await active_administrator_ids(db_session)


async def _target_snapshot(db, uid):
    row = (await db.execute(
        select(User.full_name, User.is_active, User.password_hash).where(User.id == uid)
    )).one()
    memberships = set((await db.execute(
        select(UserGroupMember.group_id).where(UserGroupMember.user_id == uid)
    )).scalars())
    return tuple(row), memberships


async def test_real_user_route_returns_conflict_and_rolls_back_all_fields_and_memberships(
    client, world, db_session,
):
    group = await _group(
        client, world["headers"], allow=["rack:read"],
        sites=[{"site_id": world["site"]["site"], "rack_scope": "selected", "rack_ids": []}],
    )
    target, _ = await _group_user(client, world["headers"], [group])
    target_id = uuid.UUID(target["id"])
    # A legacy/admin database change creates a state containing restricted admins
    # but no qualifying global admin. The subsequent HTTP request is fully real.
    await _set_active(db_session, world["root"], False)
    await db_session.commit()
    assert await _qualifying(db_session) == set()
    before = await _target_snapshot(db_session, target_id)
    response = await client.patch(
        f"/api/v1/users/{target_id}",
        headers=world["restricted_headers"],
        json={"full_name": "Must roll back", "is_active": False,
              "password": "must-not-be-persisted-password", "group_ids": []},
    )
    assert response.status_code == 409, response.text
    assert await _target_snapshot(db_session, target_id) == before


async def _advisory_waiter(engine, lock_name, timeout=10.0):
    """Polls pg_stat_activity until a backend is blocked on an advisory lock, and returns the lock's
    classid/objid as proof it is `lock_name`'s lock. Deterministic: no fixed sleep decides the outcome."""
    expected = text("SELECT hashtext(:n)::bigint & 4294967295 AS objid").bindparams(n=lock_name)
    deadline = asyncio.get_running_loop().time() + timeout
    async with engine.connect() as conn:
        want = (await conn.execute(expected)).scalar_one()
        while asyncio.get_running_loop().time() < deadline:
            rows = (await conn.execute(text(
                "SELECT l.objid, a.query FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND NOT l.granted AND a.datname = current_database()"
            ))).all()
            for objid, query in rows:
                if int(objid) == int(want):
                    return query
            await asyncio.sleep(0.05)
    raise AssertionError(f"no backend became blocked on the {lock_name!r} advisory lock")


async def test_two_postgres_transactions_cannot_remove_both_unrestricted_administrators(
    world, db_session, db_engine, make_user,
):
    second = await make_user(f"second-{uuid.uuid4().hex}@example.com", PW, "Administrator")
    second_id = second.id
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    locked = asyncio.Event()
    release = asyncio.Event()
    attempted = asyncio.Event()

    async def first_transaction():
        async with factory() as db:
            await acquire_authority_lock(db)
            await _set_active(db, world["root"], False)
            await assert_administrator_remains(db)
            locked.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            await db.commit()
            return "committed"

    async def second_transaction():
        await asyncio.wait_for(locked.wait(), timeout=10)
        async with factory() as db:
            attempted.set()
            await acquire_authority_lock(db)
            await _set_active(db, second_id, False)
            try:
                await assert_administrator_remains(db)
            except ConflictError:
                await db.rollback()
                return "conflict"
            await db.commit()
            return "committed"

    first = asyncio.create_task(first_transaction())
    second_task = asyncio.create_task(second_transaction())
    try:
        await asyncio.wait_for(attempted.wait(), timeout=10)
        # Locking evidence from PostgreSQL itself: the second backend is blocked on the exclusive
        # authority lock held by the first, not merely slow.
        waiting_query = await _advisory_waiter(db_engine, AUTHORITY_LOCK_NAME)
        assert "pg_advisory_xact_lock" in waiting_query
        assert not second_task.done(), "the second authority transaction failed to wait"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second_task), timeout=15)
        assert results == ["committed", "conflict"]
    finally:
        release.set()
        for task in (first, second_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second_task, return_exceptions=True)

    await db_session.rollback()
    assert await _qualifying(db_session) == {second_id}
    assert set(await active_administrator_ids(db_session)) == {second_id}


# ---------------------------------------------------------------------------------------------
# Real-route rollback evidence. Each rejected mutation must leave EVERY table the route can write
# exactly as it was: user columns, memberships, refresh tokens, role assignments, group permission
# and site/rack access rows, the audit log and the outbox. A positive control proves the snapshot
# would notice a commit, so the equality assertions are not vacuous.
# ---------------------------------------------------------------------------------------------
async def _state(db, user_id, group_id):
    """Everything the user and group administration routes can write, read straight from PostgreSQL."""
    user = (await db.execute(
        select(User.id, User.full_name, User.is_active, User.password_hash).where(User.id == user_id)
    )).all()
    memberships = set((await db.execute(
        select(UserGroupMember.group_id, UserGroupMember.user_id)
    )).all())
    tokens = set((await db.execute(
        select(RefreshToken.jti, RefreshToken.user_id, RefreshToken.revoked_at)
    )).all())
    assignments = set((await db.execute(
        select(RoleAssignment.id, RoleAssignment.user_id, RoleAssignment.role_id,
               RoleAssignment.scope_type, RoleAssignment.scope_id)
    )).all())
    group = (await db.execute(
        select(UserGroup.id, UserGroup.name, UserGroup.description).where(UserGroup.id == group_id)
    )).all()
    group_permissions = set((await db.execute(
        select(UserGroupPermission.group_id, UserGroupPermission.permission_id, UserGroupPermission.effect)
    )).all())
    site_access = set((await db.execute(
        select(UserGroupSiteAccess.id, UserGroupSiteAccess.group_id, UserGroupSiteAccess.site_id,
               UserGroupSiteAccess.rack_scope)
    )).all())
    rack_access = set((await db.execute(
        select(UserGroupRackAccess.site_access_id, UserGroupRackAccess.rack_id)
    )).all())
    audit = set((await db.execute(select(AuditLog.audit_id, AuditLog.action, AuditLog.entity_id))).all())
    outbox = set((await db.execute(
        select(OutboxEvent.event_id, OutboxEvent.status, OutboxEvent.attempts)
    )).all())
    return {
        "user": user, "memberships": memberships, "tokens": tokens, "role_assignments": assignments,
        "group": group, "group_permissions": group_permissions, "site_access": site_access,
        "rack_access": rack_access, "audit": audit, "outbox": outbox,
    }


async def _state_both(db_session, db_engine, user_id, group_id):
    """The state seen through the request's own session (catches a missing rollback) and through an
    independent connection (catches a premature commit). Both must agree."""
    own = await _state(db_session, user_id, group_id)
    async with async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)() as other:
        committed = await _state(other, user_id, group_id)
    assert own == committed, "the request session and PostgreSQL's committed state disagree"
    return own


async def _scoped_target_world(client, world, auth_headers, db_session):
    """A restricted target that the restricted administrator strictly outranks, holding every kind of
    state the routes can touch: a group with permissions, site access and a selected rack, a
    legacy site-scoped role assignment, and a live refresh token."""
    rack = await _make_rack(client, world["headers"], auth_headers, world["site"]["room"])
    group = await _group(
        client, world["headers"], allow=["rack:read"],
        sites=[{"site_id": world["site"]["site"], "rack_scope": "selected", "rack_ids": [rack]}],
    )
    target, _ = await _group_user(client, world["headers"], [group])
    target_id = uuid.UUID(target["id"])
    permission_id = (await db_session.execute(
        select(Permission.id).where(Permission.resource == "rack", Permission.action == "read")
    )).scalar_one()
    role = Role(id=uuid.uuid4(), name=f"legacy-{uuid.uuid4().hex[:8]}", is_system=False)
    db_session.add(role)
    await db_session.flush()
    db_session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    db_session.add(RoleAssignment(
        id=uuid.uuid4(), user_id=target_id, role_id=role.id,
        scope_type="site", scope_id=uuid.UUID(world["site"]["site"]),
    ))
    await db_session.commit()
    return target_id, uuid.UUID(group)


async def _retire_every_unrestricted_administrator(db):
    """The fixtures create more than one Administrator (the catalog helpers log one in), so deactivate them all."""
    for uid in await _qualifying(db):
        await _set_active(db, uid, False)
    await db.commit()


def _request(route, target_id, group_id):
    url_user, url_group = f"/api/v1/users/{target_id}", f"/api/v1/groups/{group_id}"
    return {
        "user-patch": ("patch", url_user, {"full_name": "Must roll back", "is_active": False,
                                           "password": "must-not-be-persisted-password", "group_ids": []}),
        "user-delete": ("delete", url_user, None),
        "group-delete": ("delete", url_group, None),
        "group-permissions": ("put", f"{url_group}/permissions", {"allow": [], "deny": ["rack:read"]}),
    }[route]


@pytest.mark.parametrize("route", ["user-patch", "user-delete", "group-delete", "group-permissions"])
async def test_real_routes_roll_back_every_write_when_no_unrestricted_administrator_would_remain(
    client, world, auth_headers, db_session, db_engine, route,
):
    target_id, group_id = await _scoped_target_world(client, world, auth_headers, db_session)
    # Legacy/administrative database change: only restricted administrators remain. The HTTP request
    # that follows is fully real, with every guard in place.
    await _retire_every_unrestricted_administrator(db_session)
    assert await _qualifying(db_session) == set()

    before = await _state_both(db_session, db_engine, target_id, group_id)
    # The snapshot must cover real rows, otherwise equality would prove nothing.
    assert before["tokens"] and before["role_assignments"] and before["group_permissions"]
    assert before["site_access"] and before["rack_access"] and before["audit"]
    assert any(m[1] == target_id for m in before["memberships"])

    method, url, body = _request(route, target_id, group_id)
    response = await getattr(client, method)(url, headers=world["restricted_headers"], **({"json": body} if body else {}))
    assert response.status_code == 409, response.text
    assert "unrestricted administrator" in response.text

    after = await _state_both(db_session, db_engine, target_id, group_id)
    for key in before:
        assert after[key] == before[key], f"{route}: {key} changed although the mutation was refused"
    assert not any(row[2] is not None for row in after["tokens"] if row[1] == target_id), "refresh token revoked"


@pytest.mark.parametrize("route", ["user-patch", "user-delete", "group-delete", "group-permissions"])
async def test_same_routes_commit_and_change_the_snapshot_when_an_unrestricted_survivor_exists(
    client, world, auth_headers, db_session, db_engine, make_user, route,
):
    """Positive control for the rollback test above: with a qualifying survivor the identical request
    succeeds, so the snapshot demonstrably detects a commit (audit row, state change)."""
    target_id, group_id = await _scoped_target_world(client, world, auth_headers, db_session)
    await _retire_every_unrestricted_administrator(db_session)
    await make_user(f"survivor-{uuid.uuid4().hex}@example.com", PW, "Administrator")
    assert len(await _qualifying(db_session)) == 1

    before = await _state_both(db_session, db_engine, target_id, group_id)
    method, url, body = _request(route, target_id, group_id)
    response = await getattr(client, method)(url, headers=world["restricted_headers"], **({"json": body} if body else {}))
    assert response.status_code in (200, 204), response.text

    after = await _state_both(db_session, db_engine, target_id, group_id)
    assert after != before
    assert len(after["audit"]) == len(before["audit"]) + 1
    if route == "user-delete":
        assert after["user"] == [] and not any(a[1] == target_id for a in after["role_assignments"])
    if route == "group-delete":
        assert after["group"] == [] and not after["rack_access"]


async def test_invariant_lock_alone_serialises_two_transactions_that_skip_the_authority_lock(
    world, db_session, db_engine, make_user,
):
    """Defence in depth: assert_administrator_remains takes its own transaction-scoped lock, so two
    transactions that never took the authority lock still cannot both remove a qualifying administrator."""
    second = await make_user(f"inv-{uuid.uuid4().hex}@example.com", PW, "Administrator")
    second_id = second.id
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    first_holds = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def first_transaction():
        async with factory() as db:
            await _set_active(db, world["root"], False)
            await assert_administrator_remains(db)  # passes (second remains) and now holds the invariant lock
            first_holds.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            await db.commit()
            return "committed"

    async def second_transaction():
        await asyncio.wait_for(first_holds.wait(), timeout=10)
        async with factory() as db:
            await _set_active(db, second_id, False)
            second_started.set()
            try:
                await assert_administrator_remains(db)
            except ConflictError:
                await db.rollback()
                return "conflict"
            await db.commit()
            return "committed"

    first = asyncio.create_task(first_transaction())
    loser = asyncio.create_task(second_transaction())
    try:
        await asyncio.wait_for(second_started.wait(), timeout=10)
        waiting_query = await _advisory_waiter(db_engine, ADMIN_INVARIANT_LOCK_NAME)
        assert "pg_advisory_xact_lock" in waiting_query
        assert not loser.done()
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, loser), timeout=15) == ["committed", "conflict"]
    finally:
        release.set()
        for task in (first, loser):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, loser, return_exceptions=True)

    await db_session.rollback()
    assert await _qualifying(db_session) == {second_id}
    assert (await db_session.execute(select(User.is_active).where(User.id == second_id))).scalar_one() is True
    assert (await db_session.execute(select(User.is_active).where(User.id == world["root"]))).scalar_one() is False
