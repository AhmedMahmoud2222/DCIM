"""Lock-protocol tests that call `begin_authority_change` directly (white-box).

These exist only after the serialising lock does, so they cannot run against #67's head; they are not
used as vulnerability reproductions. The behavioural (HTTP) race tests are in
test_pr67_authority_serialization.py and test_pr67_authority_relocation_race.py."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

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
