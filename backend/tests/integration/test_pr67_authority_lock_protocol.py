"""Lock-protocol tests that call `begin_authority_change` directly (white-box).

These exist only after the serialising lock does, so they cannot run against #67's head; they are not
used as vulnerability reproductions. The behavioural (HTTP) race tests are in
test_pr67_authority_serialization.py and test_pr67_authority_relocation_race.py."""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.authority_lock import AUTHORITY_LOCK_NAME
from app.application.rbac import get_auth_context
from app.application.user_admin_service import begin_authority_change
from app.core.errors import ForbiddenError
from app.domain.auth.models import User, UserGroupMember
from tests.api.test_pr67_strict_outranking import BASE, _entry, _principal
from tests.api.test_user_groups import _make_rack, _make_site


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    return {"a": a["site"], "rack_a1": await _make_rack(client, admin, auth_headers, a["room"])}


async def test_authority_is_reevaluated_after_the_serialising_lock(client, admin, world, db_session):
    """A request resolves its AuthContext first; a change committed before the mutation runs
    must be visible to it. `begin_authority_change` re-reads the actor under the lock."""
    a = await _principal(client, admin, BASE, [_entry(world["a"])])
    actor = (await db_session.execute(select(User).where(User.id == uuid.UUID(a["id"])))).scalar_one()
    stale = await get_auth_context(actor, db_session)
    assert "user:manage" in stale.permission_codes
    assert (await client.delete(f"/api/v1/groups/{a['group']}", headers=admin)).status_code == 204
    with pytest.raises(ForbiddenError):
        await begin_authority_change(db_session, stale, "user:manage")
    await db_session.rollback()


async def _wait_until_authority_waiter_queued(session, waiter, timeout: float = 5.0) -> None:
    """Poll pg_locks until a backend is queued (not granted) on the exclusive authority lock.

    `pg_advisory_xact_lock(bigint)` records the key as classid = high 32 bits, objid = low 32 bits,
    objsubid = 1. `hashtext` returns int4, widened to bigint (sign-extended), so the key is recomposed
    from both halves instead of comparing objid alone."""
    queued = text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1"
        " AND mode = 'ExclusiveLock'"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND ((classid::bigint << 32) | objid::bigint) = hashtext(:name)::bigint"
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (await session.execute(queued, {"name": AUTHORITY_LOCK_NAME})).scalar_one() < 1:
        assert not waiter.done(), "second mutation entered the authority section while the first held the lock"
        if loop.time() >= deadline:
            sql = "SELECT pid, mode, granted, classid, objid FROM pg_locks WHERE locktype = 'advisory'"
            rows = (await session.execute(text(sql))).all()
            raise AssertionError(f"second mutation never queued on the authority lock; advisory locks: {rows}")
        await asyncio.sleep(0.01)


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
        await _wait_until_authority_waiter_queued(s1, waiter)
        assert not waiter.done(), "second mutation entered the authority section while the first held the lock"

        member = await s1.get(UserGroupMember, (uuid.UUID(a["group"]), uuid.UUID(a["id"])))
        await s1.delete(member)
        await s1.commit()  # releases the lock

        with pytest.raises(ForbiddenError):
            await asyncio.wait_for(waiter, timeout=5)
        await s2.rollback()
