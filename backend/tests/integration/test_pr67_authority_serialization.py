"""Authority-changing requests racing each other, over HTTP with independent DB sessions.

Pure behaviour: nothing here imports a helper that only exists after the fix, so the file runs
unchanged against #67's own head (808707a) and fails there for the intended reason.

Pattern (same as test_pr67_authority_relocation_race.py): an event barrier is placed INSIDE the
authorization decision of request T1 ("the actor outranks the target" has just been decided, nothing
committed yet). A second authority-changing request T2 then tries to change the very relationship T1
decided on. If T2 can COMMIT before T1 does, T1 commits on an authorization that no longer holds.
The fix serialises them: T2 blocks on a transaction-scoped advisory lock until T1 commits.

Every request gets its own session/connection (shared-session `client` would hide the race).
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import get_db
from app.main import app
from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]
SENIOR = [*BASE, "equipment:read"]
NEW_PW = "attacker-chosen-pass-1"
WAIT = 20


def _entry(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def concurrent_client(client, db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _per_request_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _per_request_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def barrier(monkeypatch):
    """Pauses the first authorization decision that concerns another user, right after it passed."""
    import app.application.user_admin_service as svc

    original = svc.assert_actor_outranks_users
    state = SimpleNamespace(authorized=asyncio.Event(), proceed=asyncio.Event())

    async def paused(db, ctx, user_ids):
        await original(db, ctx, user_ids)
        if (user_ids - {ctx.user.id}) and not state.authorized.is_set():
            state.authorized.set()
            await asyncio.wait_for(state.proceed.wait(), WAIT * 2)

    monkeypatch.setattr(svc, "assert_actor_outranks_users", paused)
    return state


async def _blocked_advisory(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"))).scalar_one()


async def _until(predicate, what):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    rack = await _make_rack(client, admin, auth_headers, a["room"])
    sites = [_entry(a["site"])]
    senior_group = await _group(client, admin, allow=SENIOR, sites=sites)
    senior, senior_headers = await _group_user(client, admin, [senior_group])
    low_group = await _group(client, admin, allow=BASE, sites=sites)
    low, low_headers = await _group_user(client, admin, [low_group])
    boost_group = await _group(client, admin, allow=["equipment:read", "organization:read"], sites=sites)
    return SimpleNamespace(
        site=a["site"], rack=rack, sites=sites, admin=admin,
        senior=senior, senior_headers=senior_headers, senior_group=senior_group,
        low=low, low_headers=low_headers, low_group=low_group, boost_group=boost_group,
    )


async def _race(first, second, barrier, db_engine):
    """Runs `first` to its authorization barrier, starts `second`, and reports whether `second`
    committed while `first`'s decision was still open. Returns (r1, r2, second_finished_early)."""
    t1 = asyncio.create_task(first())
    t2 = None
    early = False
    try:
        await asyncio.wait_for(barrier.authorized.wait(), WAIT)
        t2 = asyncio.create_task(second())

        async def settled():
            return t2.done() or await _blocked_advisory(db_engine) > 0

        await _until(settled, "the second request to finish or to block on the authority lock")
        early = t2.done()
    finally:
        barrier.proceed.set()
    r1 = await asyncio.wait_for(t1, WAIT)
    r2 = await asyncio.wait_for(t2, WAIT) if t2 is not None else None
    return r1, r2, early


# --------------------------------------------------------------------- the actor loses authority mid-request
@pytest.mark.parametrize("surface", ["user-password-reset", "group-permission-replacement"])
async def test_actor_authority_cannot_be_revoked_while_its_decision_is_open(world, concurrent_client, barrier, db_engine, surface):
    """T1: senior actor administers a lower principal (decision made, uncommitted). T2: an administrator
    strips the actor's extra permission, which makes the actor a PEER of the target. T2 must not commit
    before T1: otherwise T1 commits an action the actor was already not entitled to."""
    w, c = world, concurrent_client
    if surface == "user-password-reset":
        first = lambda: c.patch(f"/api/v1/users/{w.low['id']}", json={"password": NEW_PW}, headers=w.senior_headers)  # noqa: E731
    else:
        first = lambda: c.put(  # noqa: E731
            f"/api/v1/groups/{w.low_group}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=w.senior_headers
        )
    second = lambda: c.put(  # noqa: E731
        f"/api/v1/groups/{w.senior_group}/permissions", json={"allow": BASE, "deny": []}, headers=w.admin
    )
    r1, r2, early = await _race(first, second, barrier, db_engine)
    assert not early, (
        "RACE: the actor's authority was revoked and committed while its administration decision was open; "
        "the decision then committed without being re-evaluated"
    )
    assert r1.status_code == 200, r1.text  # serial order T1, T2: the actor was entitled when it ran
    assert r2.status_code == 200, r2.text


# --------------------------------------------------------------------- the target gains authority mid-request
@pytest.mark.parametrize("surface", ["user-password-reset", "group-permission-replacement"])
async def test_target_cannot_be_promoted_above_the_actor_while_its_decision_is_open(world, concurrent_client, barrier, db_engine, surface):
    """T1: the actor administers a strictly lower principal. T2: an administrator adds that principal to a
    group that makes it exceed the actor. T2 must wait for T1."""
    w, c = world, concurrent_client
    # the actor here is the plain BASE user; the target is a user below it
    reader_group = await _group(c, w.admin, allow=["rack:read"], sites=w.sites)
    reader, _ = await _group_user(c, w.admin, [reader_group])
    if surface == "user-password-reset":
        first = lambda: c.patch(f"/api/v1/users/{reader['id']}", json={"password": NEW_PW}, headers=w.low_headers)  # noqa: E731
    else:
        first = lambda: c.put(  # noqa: E731
            f"/api/v1/groups/{reader_group}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=w.low_headers
        )
    second = lambda: c.put(  # noqa: E731
        f"/api/v1/groups/{w.boost_group}/members", json={"user_ids": [reader["id"]]}, headers=w.admin
    )
    r1, r2, early = await _race(first, second, barrier, db_engine)
    assert not early, (
        "RACE: the target was promoted above the actor and committed while the actor's administration decision "
        "was open; the actor then administered a principal it no longer outranked"
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


# --------------------------------------------------------------------- peers cannot administer each other, concurrently either
async def test_equal_authority_peers_attacking_each_other_concurrently_both_fail(client, admin, db_engine, auth_headers):
    """Two peers deactivate each other at the same moment, each on its own DB session. Both must be
    refused and both stay active. (Before the fix both calls succeed; one of them is only stopped
    by the last-administrator check when no other administrator exists.)"""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _per_request_db():
        async with factory() as session:
            yield session

    a_site = await _make_site(client, admin)
    sites = [_entry(a_site["site"])]
    ga = await _group(client, admin, allow=BASE, sites=sites)
    gb = await _group(client, admin, allow=BASE, sites=sites)
    a, ha = await _group_user(client, admin, [ga])
    b, hb = await _group_user(client, admin, [gb])

    app.dependency_overrides[get_db] = _per_request_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for _ in range(3):
                r = await asyncio.gather(
                    c.patch(f"/api/v1/users/{b['id']}", json={"is_active": False}, headers=ha),
                    c.patch(f"/api/v1/users/{a['id']}", json={"is_active": False}, headers=hb),
                )
                assert [x.status_code for x in r] == [403, 403], [x.text for x in r]
    finally:
        app.dependency_overrides.pop(get_db, None)
    for u in (a, b):
        assert (await client.get(f"/api/v1/users/{u['id']}", headers=admin)).json()["is_active"] is True
        assert (await client.post("/api/v1/auth/login", json={"email": u["email"], "password": PW})).status_code == 200


async def test_many_concurrent_authority_changes_stay_consistent_and_deadlock_free(world, concurrent_client):
    """Mixed membership/permission/site changes from several actors at once on independent sessions:
    no 500s, no deadlock, and the final effective access of the shared target is one of the legal serial results."""
    w, c = world, concurrent_client
    extra = [await _group(c, w.admin, allow=["rack:read"], sites=w.sites) for _ in range(4)]
    users = []
    for _ in range(4):
        users.append((await _group_user(c, w.admin, [extra[0]]))[0])
    calls = []
    for i, u in enumerate(users):
        calls.append(c.patch(f"/api/v1/users/{u['id']}", json={"full_name": f"Concurrent {i} {uuid.uuid4().hex[:4]}"}, headers=w.senior_headers))
        calls.append(c.put(f"/api/v1/groups/{extra[1 + i % 3]}/members", json={"user_ids": [u["id"]]}, headers=w.senior_headers))
        calls.append(c.put(f"/api/v1/groups/{extra[1 + i % 3]}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=w.senior_headers))
    results = await asyncio.wait_for(asyncio.gather(*calls), 60)
    assert all(r.status_code < 500 for r in results), [r.text for r in results if r.status_code >= 500]
    assert all(r.status_code in (200, 409) for r in results), [(r.status_code, r.text) for r in results if r.status_code not in (200, 409)]
    for u in users:
        eff = (await c.get(f"/api/v1/users/{u['id']}/effective-access", headers=w.admin)).json()
        assert set(eff["permissions"]) == {"rack:read"}, eff
