"""PR #67 remediation, Blocker 2: rack relocation racing a delegated-administration decision.

Scope is derived from CURRENT rack placement, so a relocation committed between the authorization
decision and the mutation's commit invalidates the decision. These tests run real, concurrent
PostgreSQL transactions (one session per request) and use an event barrier INSIDE the authorization
decision, never a timed sleep, to pin the interleaving:

    T1  actor administers the target; decision made; paused at the barrier (still uncommitted)
    T2  administrator relocates the rack the target's scope depends on
    (barrier released) T1 commits

Waiting is done by polling pg_locks for a blocked advisory-lock request (or for T2 completing),
which is the condition under test, with a generous timeout as a failure bound only.

Scenario (from the review): actor holds every rack in site A and an empty selected-rack grant for
site B; target holds selected rack R in A and an empty grant for B; equal permissions. R is in A, so
the actor strictly outranks the target. After R moves to B the target sees R and the actor does not."""

import asyncio
import inspect
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import get_db
from app.main import app
from tests.api.test_user_groups import PW, _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]
NEW_PW = "attacker-chosen-pass-1"
WAIT_SECONDS = 20


def _site(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def scenario(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    rack = await _make_rack(client, admin, auth_headers, a["room"])
    actor_group = await _group(client, admin, allow=BASE, sites=[_site(a["site"], "selected", [rack]), _site(b["site"], "selected")])
    actor, actor_headers = await _group_user(client, admin, [actor_group])
    target_group = await _group(client, admin, allow=BASE, sites=[_site(a["site"], "selected"), _site(b["site"], "selected")])
    target, _ = await _group_user(client, admin, [target_group])
    return SimpleNamespace(
        site_a=a["site"], site_b=b["site"], room_a=a["room"], room_b=b["room"], rack=rack, admin=admin,
        actor=actor, actor_headers=actor_headers, target=target, target_group=target_group, actor_group=actor_group,
    )


@pytest.fixture
async def concurrent_client(client, db_engine):
    """Same app, but every request gets its OWN session/connection (the shared-session `client`
    fixture would serialise everything through one connection and hide any race)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _per_request_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _per_request_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def barrier(monkeypatch):
    """Pauses the FIRST authorization decision that concerns another user, right after it passed."""
    import app.application.user_admin_service as svc

    original = svc.assert_actor_outranks_users
    state = SimpleNamespace(authorized=asyncio.Event(), proceed=asyncio.Event())

    async def paused(db, ctx, user_ids):
        await original(db, ctx, user_ids)
        if (user_ids - {ctx.user.id}) and not state.authorized.is_set():
            state.authorized.set()
            await asyncio.wait_for(state.proceed.wait(), WAIT_SECONDS * 2)

    monkeypatch.setattr(svc, "assert_actor_outranks_users", paused)
    return state


async def _blocked_advisory_requests(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"))).scalar_one()


async def _until(predicate, what):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


MUTATIONS = {
    "user-password-reset": lambda c, s: c.patch(
        f"/api/v1/users/{s.target['id']}", json={"password": NEW_PW}, headers=s.actor_headers),
    "group-permission-replacement": lambda c, s: c.put(
        f"/api/v1/groups/{s.target_group}/permissions", json={"allow": ["rack:read"], "deny": []}, headers=s.actor_headers),
}


async def _login_status(client, email, password):
    return (await client.post("/api/v1/auth/login", json={"email": email, "password": password})).status_code


# ------------------------------------------------------------------ the race itself
@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_relocation_cannot_commit_between_an_authority_decision_and_its_commit(
    scenario, concurrent_client, barrier, db_engine, mutation
):
    s, c = scenario, concurrent_client
    t1 = asyncio.create_task(MUTATIONS[mutation](c, s))
    t2 = None
    early = False
    try:
        await asyncio.wait_for(barrier.authorized.wait(), WAIT_SECONDS)  # T1 decided "actor outranks target"
        t2 = asyncio.create_task(c.post(f"/api/v1/racks/{s.rack}/move", json={"room_id": s.room_b}, headers=s.admin))

        async def settled():
            return t2.done() or await _blocked_advisory_requests(db_engine) > 0

        await _until(settled, "the relocation to finish or to block on the authority lock")
        early = t2.done()  # True means the relocation committed while T1's decision was still open
    finally:
        barrier.proceed.set()
    r1 = await asyncio.wait_for(t1, WAIT_SECONDS)
    r2 = await asyncio.wait_for(t2, WAIT_SECONDS) if t2 is not None else None

    assert not early, (
        "RACE: the relocation committed while the delegated-administration decision was still open, so T1 "
        "committed using an authorization that no longer held (the actor lost its extra visible rack and is now a peer)"
    )
    assert r1.status_code == 200, r1.text
    assert r2 is not None and r2.status_code == 200, r2.text  # the relocation itself is legitimate and still succeeds


async def test_after_the_relocation_commits_the_actor_no_longer_outranks_the_target(scenario, concurrent_client, barrier):
    """The serial order the lock enforces: T1 then T2. A fresh decision after T2 refuses."""
    s, c = scenario, concurrent_client
    barrier.proceed.set()  # no pausing in this test
    assert (await c.post(f"/api/v1/racks/{s.rack}/move", json={"room_id": s.room_b}, headers=s.admin)).status_code == 200
    refused = await MUTATIONS["user-password-reset"](c, s)
    assert refused.status_code == 403, refused.text
    email = (await c.get(f"/api/v1/users/{s.target['id']}", headers=s.admin)).json()["email"]
    assert await _login_status(c, email, NEW_PW) == 401 and await _login_status(c, email, PW) == 200


async def test_without_a_relocation_the_same_administration_is_permitted(scenario, concurrent_client, barrier):
    """Control: the scenario's initial state really does give the actor strict authority."""
    s, c = scenario, concurrent_client
    barrier.proceed.set()
    ok = await MUTATIONS["user-password-reset"](c, s)
    assert ok.status_code == 200, ok.text


async def test_legitimate_relocation_still_works_when_no_authority_decision_is_open(scenario, concurrent_client):
    s, c = scenario, concurrent_client
    for room in (s.room_b, s.room_a):
        resp = await c.post(f"/api/v1/racks/{s.rack}/move", json={"room_id": room}, headers=s.admin)
        assert resp.status_code == 200, resp.text
    retired = await c.post(f"/api/v1/racks/{s.rack}/retire", headers=s.admin)
    assert retired.status_code == 200, retired.text


# ------------------------------------------------------------------ lock protocol on real PostgreSQL
async def _two_sessions(db_engine, n):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    sessions = [factory() for _ in range(n)]
    return sessions


async def test_placement_writers_share_the_lock_and_authority_writers_exclude_them(db_engine):
    from app.application.authority_lock import acquire_authority_lock, acquire_placement_scope_lock

    s1, s2, s3, s4 = await _two_sessions(db_engine, 4)
    try:
        await acquire_placement_scope_lock(s1)
        await asyncio.wait_for(acquire_placement_scope_lock(s2), 5)  # shared + shared: no wait
        assert await _blocked_advisory_requests(db_engine) == 0

        exclusive = asyncio.create_task(acquire_authority_lock(s3))
        await _until(lambda: _async_value(_blocked_advisory_requests(db_engine), lambda n: n == 1), "the exclusive request to block")
        await s1.rollback()  # one shared holder gone: still blocked by the other
        assert await _blocked_advisory_requests(db_engine) == 1 and not exclusive.done()
        await s2.rollback()
        await asyncio.wait_for(exclusive, 5)  # last shared holder gone: granted

        shared = asyncio.create_task(acquire_placement_scope_lock(s4))
        await _until(lambda: _async_value(_blocked_advisory_requests(db_engine), lambda n: n == 1), "the shared request to block")
        assert not shared.done()
        await s3.rollback()
        await asyncio.wait_for(shared, 5)
    finally:
        for sess in (s1, s2, s3, s4):
            await sess.rollback()
            await sess.close()


async def _async_value(awaitable, check):
    return check(await awaitable)


@pytest.mark.parametrize("writer", ["move_rack", "retire_rack_placement"])
async def test_every_placement_writer_entry_point_waits_for_an_open_authority_decision(
    scenario, db_engine, writer
):
    """Service-level (not API): `move_rack` and `retire_rack_placement` are the only writers of
    scope-relevant placement, and the API routes and the bulk-import rack commit all go through them."""
    from app.application import placement_service
    from app.application.authority_lock import acquire_authority_lock

    s1, s2 = await _two_sessions(db_engine, 2)
    try:
        await acquire_authority_lock(s1)  # an authority decision is open
        if writer == "move_rack":
            call = placement_service.move_rack(s2, rack_id=uuid.UUID(scenario.rack), room_id=uuid.UUID(scenario.room_b))
        else:
            call = placement_service.retire_rack_placement(s2, rack_id=uuid.UUID(scenario.rack))
        task = asyncio.create_task(call)
        await _until(lambda: _async_value(_blocked_advisory_requests(db_engine), lambda n: n == 1), f"{writer} to block")
        assert not task.done()
        await s1.rollback()  # decision committed or rolled back
        await asyncio.wait_for(task, 10)
        await s2.rollback()  # leave the rack where it was
    finally:
        for sess in (s1, s2):
            await sess.rollback()
            await sess.close()


async def test_mixed_authority_and_relocation_load_neither_deadlocks_nor_errors(scenario, concurrent_client, auth_headers, admin):
    """Lock-order check under load: authority writes and relocations of several racks interleave."""
    s, c = scenario, concurrent_client
    racks = [await _make_rack(c, admin, auth_headers, s.room_a) for _ in range(6)]
    site = [_site(s.site_a), _site(s.site_b, "selected")]
    lows = []
    for _ in range(6):
        low, _h = await _group_user(c, admin, [await _group(c, admin, allow=["rack:read"], sites=site)])
        lows.append(low["id"])
    calls = []
    for i, rack in enumerate(racks):
        calls.append(c.post(f"/api/v1/racks/{rack}/move", json={"room_id": s.room_b}, headers=admin))
        calls.append(c.patch(f"/api/v1/users/{lows[i]}", json={"full_name": f"Renamed {i}"}, headers=s.actor_headers))
        calls.append(c.post(f"/api/v1/racks/{rack}/retire", headers=admin))
    results = await asyncio.wait_for(asyncio.gather(*[asyncio.ensure_future(x) for x in calls], return_exceptions=True), 120)
    assert all(not isinstance(r, Exception) for r in results), results
    assert all(r.status_code < 500 for r in results), [r.text for r in results if r.status_code >= 500]
    # every administration of a subordinate user succeeded, and every relocation/retirement was accepted or cleanly 409/404
    assert all(r.status_code == 200 for r in results[1::3]), [r.text for r in results[1::3]]


# ------------------------------------------------------------------ writer inventory guard
def test_only_the_placement_service_writes_rack_placements_and_it_takes_the_scope_lock():
    """Static guard (not a behavioural test): a new writer of RackPlacement must not appear outside
    placement_service, whose two writers must take the shared lock first."""
    from app.application import placement_service

    app_dir = Path(placement_service.__file__).resolve().parents[1]
    offenders = []
    for path in app_dir.rglob("*.py"):
        rel = path.relative_to(app_dir).as_posix()
        if rel.startswith(("domain/", "db/")) or rel == "application/placement_service.py":
            continue
        source = path.read_text()
        if re.search(r"\bRackPlacement\s*\(", source) or re.search(r"update\(\s*RackPlacement\b|delete\(\s*RackPlacement\b", source):
            offenders.append(rel)
    assert offenders == [], f"new RackPlacement writer(s) must go through placement_service (and its lock): {offenders}"
    for fn in (placement_service.move_rack, placement_service.retire_rack_placement):
        assert "acquire_placement_scope_lock(db)" in inspect.getsource(fn)
