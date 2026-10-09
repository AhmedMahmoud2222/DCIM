"""SEC-63: only active, unrestricted effective administrators preserve the invariant.

Domain transactions reproduce the remaining helper defect. HTTP tests use a deliberately
invalid legacy state: strict outranking and self-edit guards prevent an API caller from
removing the sole unrestricted administrator from an initially valid state. No guard is
mocked or bypassed in the HTTP operation under test.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.access_control import active_administrator_ids, load_effective_access
from app.application.authority_lock import acquire_authority_lock
from app.application.user_admin_service import assert_administrator_remains
from app.core.errors import ConflictError
from app.domain.auth.models import User, UserGroupMember
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
        await asyncio.sleep(0.2)
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
