"""Issue #150 acceptance-evidence: PDU-specific concurrency against real PostgreSQL, with every concurrent request on its
own AsyncSession/connection (the pattern of test_idempotency_concurrency.py - the shared-session `client` fixture is not
safe for concurrent use).

Covers: two simultaneous publishes of one PDU revision, a publish racing a draft outlet replacement, concurrent
instantiation with the same Idempotency-Key, and concurrent instantiation with the same asset tag under different keys,
including proof that the losing transaction left no partial PDU graph behind. Tests only - no runtime change."""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.main import app
from tests.conftest import TEST_DATABASE_URL

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def per_request_client(db_session):
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=25, max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def _request(method: str, url: str, headers: dict | None = None, **kwargs):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.request(method, url, headers=headers, **kwargs)


async def _admin_headers(make_user) -> dict:
    user = await make_user(f"pdu-conc-{uuid.uuid4().hex[:8]}@example.com", PASSWORD, "Administrator")
    resp = await _request("POST", "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _outlet(number: int) -> dict:
    return {
        "stable_key": f"outlet-{number}", "outlet_number": number, "outlet_type": "iec_c13", "outlet_count": 1,
        "phase": "single", "rated_voltage_v": 230.0, "rated_current_a": 10.0, "side": "front", "sort_order": number,
    }


async def _make_pdu_revision(headers: dict, *, publish: bool, outlets: int = 2) -> dict:
    manufacturer = await _request(
        "POST", "/api/v1/catalog/manufacturers", headers=headers, json={"name": f"PduConc-{uuid.uuid4().hex[:8]}"}
    )
    assert manufacturer.status_code == 201, manufacturer.text
    model = await _request(
        "POST", "/api/v1/catalog/models", headers=headers,
        json={"manufacturer_id": manufacturer.json()["id"], "category": "pdu", "model_name": f"PduConc-{uuid.uuid4().hex[:8]}"},
    )
    assert model.status_code == 201, model.text
    revision = await _request("POST", f"/api/v1/catalog/models/{model.json()['id']}/revisions", headers=headers)
    assert revision.status_code == 201, revision.text
    revision = revision.json()
    patched = await _request(
        "PATCH", f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        headers={**headers, "If-Match": str(revision["version"])},
        json={
            "pdu_input_voltage_v": 400.0, "pdu_input_current_a": 32.0, "pdu_phase": "three",
            "mounting_orientation": "vertical", "outlets": [_outlet(n) for n in range(1, outlets + 1)],
        },
    )
    assert patched.status_code == 200, patched.text
    # PATCH /pdu returns PDUDraftOut (keyed `revision_id`, no `id`/`version`); keep the original revision dict as the handle.
    assert patched.json()["revision_id"] == revision["id"]
    revision["version"] += 1  # lock_draft_revision_for_edit bumps the version; a later If-Match must use the new value
    if publish:
        published = await _request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
        assert published.status_code == 200, published.text
    return revision


async def _instantiate(headers: dict, revision_id: str, *, tag: str, key: str | None = None):
    h = {**headers, **({"Idempotency-Key": key} if key else {})}
    return await _request(
        "POST", "/api/v1/power/pdus/instantiate", headers=h,
        json={"asset_tag": tag, "name": "conc-pdu", "catalog_model_revision_id": revision_id},
    )


async def _count(db_session, sql: str, **params) -> int:
    await db_session.rollback()  # end any stale snapshot; the test session only reads
    params = {k: uuid.UUID(v) if isinstance(v, str) and len(v) == 36 else v for k, v in params.items()}
    return (await db_session.execute(text(sql), params)).scalar_one()


async def test_concurrent_publish_of_one_pdu_revision_has_exactly_one_winner(make_user, db_session, per_request_client):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=False)

    results = await asyncio.gather(
        *[_request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers) for _ in range(5)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1, f"expected exactly one successful publish, got {statuses}"
    assert all(s == 409 for s in statuses if s != 200), f"losers must be a deterministic 409, got {statuses}"

    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'catalog.revision.publish' AND entity_id = :r",
        r=revision["id"],
    ) == 1, "exactly one publish audit row"
    assert await _count(
        db_session, "SELECT count(*) FROM pdu_outlet_template WHERE catalog_model_revision_id = :r", r=revision["id"]
    ) == 2, "outlet templates untouched by the losing publishes"
    # a PDU never gets a legacy bridge, even under a publish race
    assert await _count(
        db_session,
        "SELECT (SELECT count(*) FROM rack_model_revision WHERE bridged_from_catalog_revision_id = :r) "
        "+ (SELECT count(*) FROM equipment_model_revision WHERE bridged_from_catalog_revision_id = :r)",
        r=uuid.UUID(revision["id"]),
    ) == 0


async def _wait_for_single_waiter_blocked_by(engine, blocker_pid: int, timeout: float = 20.0) -> int:
    """Wait until exactly one backend is blocked *by `blocker_pid`* (pg_blocking_pids) and return its pid. Scoped to the
    blocking relationship, so unrelated lock waiters in the database are never counted; more than one waiter behind the
    same blocker, or no waiter before the timeout, fails closed. Polls database state, not a fixed sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with engine.connect() as conn:
            pids = [r[0] for r in await conn.execute(text(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND CAST(:b AS integer) = ANY(pg_blocking_pids(pid)) ORDER BY pid"
            ), {"b": blocker_pid})]
        assert len(pids) <= 1, f"expected one waiter blocked by pid {blocker_pid}, saw {pids}"
        if pids:
            return pids[0]
        assert asyncio.get_running_loop().time() < deadline, f"timed out: no backend blocked by pid {blocker_pid}"
        await asyncio.sleep(0.02)


async def test_waiter_detection_ignores_unrelated_lock_waiters():
    """Adversarial regression for _wait_for_single_waiter_blocked_by: an unrelated lock wait (advisory lock) must not be
    mistaken for a waiter on the held blocker, while a real waiter on that blocker is still found."""
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=6)
    tasks: list[asyncio.Task] = []
    try:
        async with engine.connect() as holder, engine.connect() as other, engine.connect() as unrelated, engine.connect() as target:
            key = uuid.uuid4().int % (2**31)
            other_pid = (await other.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key + 1})
            tasks.append(asyncio.create_task(unrelated.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})))
            unrelated_pid = await _wait_for_single_waiter_blocked_by(engine, other_pid)  # waits on `other`, not `holder`
            try:
                await _wait_for_single_waiter_blocked_by(engine, holder_pid, timeout=0.5)
            except AssertionError as exc:
                assert "timed out" in str(exc)
            else:
                raise AssertionError("unrelated waiter was attributed to the holder")
            tasks.append(asyncio.create_task(target.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key + 1})))
            assert await _wait_for_single_waiter_blocked_by(engine, holder_pid) != unrelated_pid
            await holder.execute(text("SELECT pg_advisory_unlock_all()"))
            await other.execute(text("SELECT pg_advisory_unlock_all()"))
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        for t in tasks:
            t.cancel()
        await engine.dispose()


@pytest.mark.parametrize("patch_first", [True, False], ids=["patch_wins_lock", "publish_wins_lock"])
async def test_publish_vs_outlet_replacement_in_both_lock_orders(make_user, db_session, per_request_client, patch_first):
    """An independent session holds the revision row lock; the intended winner's request is started and observed blocked
    on it (pg_stat_activity), then the loser is started and observed queued behind it, and only then is the lock
    released. PostgreSQL grants queued row locks FIFO, so each interleaving is forced rather than hoped for; if either
    operation stopped taking the revision row lock it would never appear as a waiter and the test would fail."""
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=False, outlets=2)
    rid = uuid.UUID(revision["id"])

    def patch():
        return _request(
            "PATCH", f"/api/v1/catalog/revisions/{revision['id']}/pdu",
            headers={**headers, "If-Match": str(revision["version"])},
            json={"outlets": [_outlet(n) for n in range(1, 5)]},
        )

    def publish():
        return _request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)

    first, second = (patch, publish) if patch_first else (publish, patch)
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=5)
    tasks: list[asyncio.Task] = []
    try:
        async with engine.connect() as blocker:
            await blocker.execute(text("SELECT id FROM catalog_model_revision WHERE id = :r FOR UPDATE"), {"r": rid})
            blocker_pid = (await blocker.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            try:
                tasks.append(asyncio.create_task(first()))
                first_pid = await _wait_for_single_waiter_blocked_by(engine, blocker_pid)
                tasks.append(asyncio.create_task(second()))
                # the second request must queue behind the first request's backend (FIFO), not merely be "some waiter"
                second_pid = await _wait_for_single_waiter_blocked_by(engine, first_pid)
                assert second_pid not in (first_pid, blocker_pid)
                assert await _wait_for_single_waiter_blocked_by(engine, blocker_pid) == first_pid
                assert not any(t.done() for t in tasks), "a request completed before the revision lock was released"
            finally:
                await blocker.rollback()  # release the lock; nothing was modified
        first_resp, second_resp = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    finally:
        for t in tasks:
            t.cancel()
        await engine.dispose()

    patch_resp, publish_resp = (first_resp, second_resp) if patch_first else (second_resp, first_resp)
    assert publish_resp.status_code == 200, publish_resp.text
    outlets = await _count(
        db_session, "SELECT count(*) FROM pdu_outlet_template WHERE catalog_model_revision_id = :r", r=revision["id"]
    )
    status = (await db_session.execute(
        text("SELECT lifecycle_status FROM catalog_model_revision WHERE id = :r"), {"r": revision["id"]}
    )).scalar_one()
    assert status == "published", f"revision must end published, got {status!r}"
    if patch_first:
        assert patch_resp.status_code == 200, patch_resp.text
        assert outlets == 4, "patch committed before publish: the published revision must carry all 4 outlets"
    else:
        assert patch_resp.status_code == 409, patch_resp.text
        assert outlets == 2, "patch rejected after publish: the published revision must keep its original 2 outlets"
    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'catalog.revision.publish' AND entity_id = :r",
        r=revision["id"],
    ) == 1


async def test_concurrent_instantiate_with_one_idempotency_key_creates_one_pdu_graph(
    make_user, db_session, per_request_client
):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=True, outlets=3)
    tag, key = f"PDU-IDEM-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())

    results = await asyncio.gather(*[_instantiate(headers, revision["id"], tag=tag, key=key) for _ in range(8)])
    statuses = [r.status_code for r in results]
    # Every request either wins (201), replays the cached 201, or is told to retry (503 still-processing); never a 409/500.
    assert all(s in (201, 503) for s in statuses), f"unexpected statuses {statuses}"
    assert statuses.count(201) >= 1
    assert len({r.json()["id"] for r in results if r.status_code == 201}) == 1, "one distinct PDU node id"

    retry = await _instantiate(headers, revision["id"], tag=tag, key=key)
    assert retry.status_code == 201 and retry.json()["id"] == results[statuses.index(201)].json()["id"]

    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 1
    asset_id = (await db_session.execute(text("SELECT id FROM managed_asset WHERE asset_tag = :t"), {"t": tag})).scalar_one()
    assert await _count(db_session, "SELECT count(*) FROM pdu WHERE id = :a", a=asset_id) == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet WHERE pdu_asset_id = :a", a=asset_id) == 3
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE owning_asset_id = :a", a=asset_id) == 3
    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate' AND entity_id = :a", a=asset_id
    ) == 1


async def test_concurrent_instantiate_with_same_tag_and_different_keys_leaves_no_partial_graph(
    make_user, db_session, per_request_client
):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=True, outlets=2)
    tag = f"PDU-TAG-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        *[_instantiate(headers, revision["id"], tag=tag, key=str(uuid.uuid4())) for _ in range(6)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1, f"exactly one instantiation may win the asset tag, got {statuses}"
    assert all(s == 409 for s in statuses if s != 201), f"losers must be a deterministic 409 (no 500), got {statuses}"

    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 1
    # Whole-database invariants: the losers rolled back completely - every PDU asset has its subtype row and exactly the
    # templated outlets, and no outlet or outlet node is orphaned.
    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_type = 'pdu'") == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu") == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet") == 2
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE node_type = 'pdu'") == 1
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE node_type = 'pdu_outlet'") == 2
    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate'"
    ) == 1
    assert await _count(
        db_session, "SELECT count(*) FROM outbox_event WHERE event_type = 'PDUInstantiated'"
    ) == 1


async def test_failure_after_graph_flush_rolls_back_every_row_and_a_retry_succeeds(
    make_user, db_session, per_request_client, monkeypatch
):
    """Fault injection AFTER the PDU graph has been flushed (the uniqueness-contention test above fails at the first
    ManagedAsset flush, before any subtype/node/outlet exists). The patched outbox writer first flushes and counts the
    request's own uncommitted rows - proving asset, pdu, nodes, outlets and the audit row really are in the transaction -
    then raises. An independent session must then see none of them, and a retry with the same key and tag must succeed
    (claim released, tag free). If the route committed early or skipped the rollback, the post-failure counts would be
    non-zero and this test would fail."""
    import app.api.v1.power as power_routes

    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=True, outlets=3)
    tag, key = f"PDU-FAULT-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
    in_flight: dict[str, int] = {}
    by_tag = "(SELECT id FROM managed_asset WHERE asset_tag = :t)"
    probes = {
        "asset": "SELECT count(*) FROM managed_asset WHERE asset_tag = :t",
        "pdu": f"SELECT count(*) FROM pdu WHERE id IN {by_tag}",
        "pdu_node": f"SELECT count(*) FROM power_node WHERE node_type = 'pdu' AND managed_asset_id IN {by_tag}",
        "outlet_node": f"SELECT count(*) FROM power_node WHERE node_type = 'pdu_outlet' AND owning_asset_id IN {by_tag}",
        "outlet": f"SELECT count(*) FROM pdu_outlet WHERE pdu_asset_id IN {by_tag}",
        "audit": f"SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate' AND entity_id IN {by_tag}",
    }

    async def failing_outbox(db, **kwargs):
        await db.flush()
        for name, sql in probes.items():
            in_flight[name] = (await db.execute(text(sql), {"t": tag})).scalar_one()
        raise RuntimeError("injected failure after graph flush")

    real_outbox = power_routes.write_outbox_event
    monkeypatch.setattr(power_routes, "write_outbox_event", failing_outbox)
    try:
        failed = await _instantiate(headers, revision["id"], tag=tag, key=key)
        assert failed.status_code == 500, failed.text
    except RuntimeError as exc:  # ASGITransport re-raises unhandled app exceptions
        assert "injected failure" in str(exc)
    monkeypatch.setattr(power_routes, "write_outbox_event", real_outbox)

    assert in_flight == {
        "asset": 1, "pdu": 1, "pdu_node": 1, "outlet_node": 3, "outlet": 3, "audit": 1,
    }, f"graph was not fully flushed before the injected failure: {in_flight}"

    # independent session (db_session is not the request's session): nothing survived
    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 0
    assert await _count(db_session, "SELECT count(*) FROM pdu") == 0
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet") == 0
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE node_type IN ('pdu', 'pdu_outlet')") == 0
    assert await _count(db_session, "SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate'") == 0
    assert await _count(db_session, "SELECT count(*) FROM outbox_event WHERE event_type = 'PDUInstantiated'") == 0

    # normal path intact: the same key and tag now succeed and produce the full graph
    retry = await _instantiate(headers, revision["id"], tag=tag, key=key)
    assert retry.status_code == 201, retry.text
    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet") == 3
    assert await _count(db_session, "SELECT count(*) FROM outbox_event WHERE event_type = 'PDUInstantiated'") == 1
