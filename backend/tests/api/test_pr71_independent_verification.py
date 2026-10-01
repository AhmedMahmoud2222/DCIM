"""Independent verification of PR #71 / #76 (lifecycle transition serialisation), tests only.

Differences from tests/api/test_pr71_lifecycle_hostile.py:
  * the row lock is observed IN POSTGRES (a second connection holds the row and the request is seen waiting in
    pg_stat_activity), not inferred from a timed barrier;
  * contention is proven to have happened (>= 2 backends seen waiting on the row) before the outcome is judged;
  * the audit trail is checked as a CHAIN (each transition starts where the previous one ended and follows the
    allowed-transition graph), not just counted;
  * 'the row lock stays held until commit' is probed with a second connection using NOWAIT while the first request
    is paused between its writes and its commit.
All requests use independent database sessions/connections.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import managed_assets as module
from app.db.session import get_db
from app.domain.identity.models import ALLOWED_LIFECYCLE_TRANSITIONS
from app.main import app

PASSWORD = "correct horse battery staple"
WAIT = 20
ROW_LOCK_WAITERS = (
    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
    "AND query ILIKE '%FROM managed_asset%' AND query ILIKE '%FOR UPDATE%'"
)


@pytest_asyncio.fixture
async def race_client(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def headers(race_client, make_user):
    user = await make_user(f"life-{uuid.uuid4().hex[:6]}@example.com", PASSWORD, "Engineer")
    login = await race_client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _transition(client, headers, asset_id, to_status):
    return await client.post(f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": to_status}, headers=headers)


async def _asset(client, headers, *path):
    created = await client.post("/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"R-{uuid.uuid4().hex[:8]}"}, headers=headers)
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    for status in path:
        resp = await _transition(client, headers, asset_id, status)
        assert resp.status_code == 200, resp.text
    return asset_id


async def _settle(*tasks):
    """Cancel and drain request tasks so a failing assertion can never leave a transaction open for the next test."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _waiters(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(ROW_LOCK_WAITERS))).scalar_one()


async def _until(predicate, what):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _row(engine, asset_id):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT lifecycle_status, decommissioned_at FROM managed_asset WHERE id = :i"), {"i": asset_id})).one()


async def _trail(engine, asset_id):
    async with engine.connect() as conn:
        audits = (
            await conn.execute(
                text("SELECT before->>'lifecycle_status', after->>'lifecycle_status' FROM audit_log "
                     "WHERE entity_id = :i AND action = 'managed_asset.lifecycle_transition' ORDER BY timestamp"),
                {"i": asset_id},
            )
        ).all()
        events = (await conn.execute(text("SELECT payload->>'from', payload->>'to' FROM outbox_event WHERE aggregate_id = :i AND event_type = 'ManagedAssetLifecycleChanged' ORDER BY created_at"), {"i": asset_id})).all()
    return [tuple(a) for a in audits], [tuple(e) for e in events]


# ------------------------------------------------------------------ #76 is in #71 and the lock is real
def test_the_row_lock_is_in_the_handler_source():
    import inspect

    source = inspect.getsource(module.transition_lifecycle)
    assert ".with_for_update()" in source and source.index(".with_for_update()") < source.index("ALLOWED_LIFECYCLE_TRANSITIONS")


async def test_a_transition_waits_for_a_row_lock_held_by_another_transaction(race_client, headers, db_engine):
    """A second connection holds the asset row. The request must be seen WAITING in PostgreSQL (not completing, not
    failing) and complete only after the holder releases. Without FOR UPDATE on the read, validation would run
    against the stale row and the request would not wait."""
    asset_id = await _asset(race_client, headers, "installed", "active")
    pending = None
    try:
        async with db_engine.connect() as holder:
            tx = await holder.begin()
            try:
                await holder.execute(text("SELECT id FROM managed_asset WHERE id = :i FOR UPDATE"), {"i": asset_id})
                pending = asyncio.create_task(_transition(race_client, headers, asset_id, "decommissioned"))
                await _until(lambda: _is_waiting(db_engine), "the request to block on the asset row lock")
                assert not pending.done(), "the request finished while another transaction held the row"
                assert (await _row(db_engine, asset_id))[0] == "active"
            finally:
                await tx.rollback()
        resp = await asyncio.wait_for(pending, WAIT)
        assert resp.status_code == 200, resp.text
        assert (await _row(db_engine, asset_id))[0] == "decommissioned"
    finally:
        if pending is not None:
            await _settle(pending)


async def _is_waiting(engine):
    return await _waiters(engine) >= 1


async def test_the_row_lock_is_held_until_commit(race_client, headers, db_engine, monkeypatch):
    """Pause the request after all its writes and before the commit; a second connection must not be able to take the
    row (NOWAIT fails). After the response, the same probe succeeds."""
    asset_id = await _asset(race_client, headers, "installed", "active")
    reached, release = asyncio.Event(), asyncio.Event()
    original = module.write_outbox_event

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)  # the last write before db.commit()
        reached.set()
        await asyncio.wait_for(release.wait(), WAIT * 2)
        return result

    monkeypatch.setattr(module, "write_outbox_event", paused)
    request = asyncio.create_task(_transition(race_client, headers, asset_id, "decommissioned"))
    try:
        await asyncio.wait_for(reached.wait(), WAIT)
        async with db_engine.connect() as probe:
            with pytest.raises(DBAPIError) as caught:
                await probe.execute(text("SELECT id FROM managed_asset WHERE id = :i FOR UPDATE NOWAIT"), {"i": asset_id})
            assert "lock" in str(caught.value).lower()
        assert (await _row(db_engine, asset_id))[0] == "active", "uncommitted change must not be visible to other sessions"
    finally:
        release.set()
    try:
        resp = await asyncio.wait_for(request, WAIT)
    finally:
        await _settle(request)
    assert resp.status_code == 200, resp.text
    async with db_engine.connect() as probe:  # committed: the row is free again
        await probe.execute(text("SELECT id FROM managed_asset WHERE id = :i FOR UPDATE NOWAIT"), {"i": asset_id})


# ------------------------------------------------------------------ competing decommissions
@pytest.mark.parametrize("round_no", range(4))
async def test_competing_decommissions_one_wins_the_rest_are_rejected_safely(race_client, headers, db_engine, round_no):
    contenders = 6
    asset_id = await _asset(race_client, headers, "installed", "active")
    seen = {"max": 0}
    stop = asyncio.Event()

    async def watch():
        while not stop.is_set():
            seen["max"] = max(seen["max"], await _waiters(db_engine))
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch())
    tasks: list[asyncio.Task] = []
    try:
        async with db_engine.connect() as holder:  # force every contender to queue behind one lock, then release together
            tx = await holder.begin()
            try:
                await holder.execute(text("SELECT id FROM managed_asset WHERE id = :i FOR UPDATE"), {"i": asset_id})
                tasks = [asyncio.create_task(_transition(race_client, headers, asset_id, "decommissioned")) for _ in range(contenders)]
                await _until(lambda: _at_least(db_engine, contenders), f"all {contenders} contenders to queue on the row")
            finally:
                await tx.rollback()
        results = await asyncio.wait_for(asyncio.gather(*tasks), WAIT * 2)
    finally:
        stop.set()
        await _settle(watcher, *tasks)

    codes = sorted(r.status_code for r in results)
    assert codes == [200] + [422] * (contenders - 1), codes
    assert seen["max"] >= contenders - 1, f"contention was not exercised: at most {seen['max']} waiting"
    status, stamp = await _row(db_engine, asset_id)
    assert status == "decommissioned" and stamp is not None and stamp.tzinfo is None
    audits, events = await _trail(db_engine, asset_id)
    decommission = ("active", "decommissioned")
    assert audits.count(decommission) == 1 and events.count(decommission) == 1, (audits, events)
    assert all(r.status_code != 500 for r in results)


async def _at_least(engine, n):
    return await _waiters(engine) >= n


# ------------------------------------------------------------------ maintenance / decommission races
@pytest.mark.parametrize("order", ["decommission-first", "maintenance-first", "simultaneous-a", "simultaneous-b"])
async def test_decommission_and_maintenance_races_leave_a_consistent_chain(race_client, headers, db_engine, order):
    for _ in range(3):
        asset_id = await _asset(race_client, headers, "installed", "active")
        calls = {
            "decommission-first": ["decommissioned", "maintenance"],
            "maintenance-first": ["maintenance", "decommissioned"],
            "simultaneous-a": ["decommissioned", "maintenance", "decommissioned", "active"],
            "simultaneous-b": ["maintenance", "active", "decommissioned", "maintenance"],
        }[order]
        results = await asyncio.gather(*[_transition(race_client, headers, asset_id, to) for to in calls])
        codes = [r.status_code for r in results]
        assert set(codes) <= {200, 422} and 200 in codes, codes
        status, stamp = await _row(db_engine, asset_id)
        if status in ("active", "maintenance"):
            assert stamp is None, "a live asset must carry no decommission timestamp"
        if status == "decommissioned":
            assert stamp is not None
        audits, events = await _trail(db_engine, asset_id)
        applied = [(a, b) for (a, b) in audits if (a, b) not in (("installed", "active"), ("planned", "installed"))]
        # every applied transition is allowed, and they chain: each starts where the previous one ended, ending at the row's status
        for before, after in audits:
            assert after in ALLOWED_LIFECYCLE_TRANSITIONS[before], (before, after)
        chain = [audits[0][0]] + [after for _, after in audits]
        assert all(audits[i][1] == audits[i + 1][0] for i in range(len(audits) - 1)), audits
        assert chain[-1] == status and len(events) == len(audits) == len(applied) + 2
        assert sum(1 for c in codes if c == 200) == len(applied)


# ------------------------------------------------------------------ write-path inventory (characterisation, see PR notes)
async def test_import_can_create_equipment_in_a_terminal_status_without_a_decommission_timestamp(client, auth_headers, db_session):
    """CHARACTERISATION, not an endorsement. The only lifecycle write outside transition_lifecycle is bulk-import equipment
    CREATE, which accepts any LIFECYCLE_STATUSES value as the initial status. It does not set decommissioned_at, so an
    equipment asset can exist as 'decommissioned' with a NULL timestamp (and with no lifecycle audit row or outbox event).
    This is a current integrity inconsistency that a CHECK constraint alone would turn into a failed import commit; the
    fix needs a product decision (reject non-'planned' initial statuses on import, or stamp the timestamp)."""
    from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job
    from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
    from tests.api.test_bulk_import_equipment import EQUIPMENT_HEADERS, _create_equipment_model

    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_equipment_model(client, auth_headers)
    tag = f"EQ-{uuid.uuid4().hex[:8]}"
    row = [tag, "srv-x", model["manufacturer"], model["model_name"], "", "floor_standing", room["site_code"], room["building_code"],
           room["floor_level"], room["room_code"], "", "", "", "", "", "", "Ops", "", "", "", "decommissioned"]
    upload = await client.post(
        "/api/v1/equipment/import-jobs?mode=create_only",
        files={"file": ("equipment.xlsx", build_workbook(EQUIPMENT_HEADERS, [row]), "application/octet-stream")}, headers=headers,
    )
    assert upload.status_code == 202, upload.text
    job_id = upload.json()["id"]
    parse_and_validate_bulk_import_job.run(job_id)
    assert (await client.post(f"/api/v1/import-jobs/{job_id}/commit", headers=headers)).status_code == 202
    commit_bulk_import_job.run(job_id)
    db_session.expire_all()
    status, stamp, audits = (
        await db_session.execute(
            text("SELECT lifecycle_status, decommissioned_at, (SELECT count(*) FROM audit_log a WHERE a.entity_id = m.id "
                 "AND a.action = 'managed_asset.lifecycle_transition') FROM managed_asset m WHERE asset_tag = :t"),
            {"t": tag},
        )
    ).one()
    assert (status, stamp, audits) == ("decommissioned", None, 0)
