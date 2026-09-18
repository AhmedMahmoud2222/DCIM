"""Genuine concurrency tests for Phase 8 (master prompt §20: "Where assignment/
registration/heartbeat state can race, use genuine concurrent database/session tests
rather than sequential simulations"). Mirrors
tests/integration/test_idempotency_concurrency.py's own pattern exactly: the shared
`client`/`db_session` fixtures bind every request to ONE session, which is fine for
sequential calls but cannot exercise a real race between independent connections. Each
concurrent request here gets its own fresh AsyncSession from a dedicated engine against
the same real PostgreSQL database every other test in this suite uses -- a true
concurrency test, not a mocked or sequential one."""

import asyncio
import json
import time
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.collector_auth import compute_signature
from app.main import app
from tests.conftest import TEST_DATABASE_URL


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


async def _login(email: str, password: str) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _request(method: str, path: str, **kwargs):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.request(method, path, **kwargs)


async def test_concurrent_duplicate_collector_name_registration_only_one_succeeds(
    make_user, db_session, per_request_client,
):
    """Ten genuinely concurrent registration requests for the exact same collector
    name -- the DB's own unique constraint on `collector.name` must let exactly one
    through, translated to a clean 409 for the rest (never a 500, never two rows)."""
    user = await make_user("conc-collector-1@example.com", "correct horse battery staple", "DCIM Manager")
    headers = await _login(user.email, "correct horse battery staple")
    name = f"race-collector-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        *[
            _request(
                "POST", "/api/v1/collectors",
                json={"name": name, "collector_type": "central", "site_id": None}, headers=headers,
            )
            for _ in range(10)
        ]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1, f"expected exactly one winner, got statuses {statuses}"
    assert statuses.count(409) == 9, f"expected the other nine to be a clean conflict, got statuses {statuses}"

    count = (await db_session.execute(text("SELECT count(*) FROM collector WHERE name = :n"), {"n": name})).scalar_one()
    assert count == 1


async def test_concurrent_reassignment_race_leaves_exactly_one_current_assignment(
    make_user, db_session, per_request_client,
):
    """Two active collectors both declare the `icmp` capability; twenty concurrent
    reassignment requests alternate between them for the SAME integration. Whatever
    interleaving actually happens, the partial unique index
    (`uq_collector_assignment_current_per_integration`) must guarantee the database
    ends up with exactly one currently-open assignment row for that integration --
    never zero, never two."""
    user = await make_user("conc-collector-2@example.com", "correct horse battery staple", "DCIM Manager")
    headers = await _login(user.email, "correct horse battery staple")

    collector_ids = []
    for _ in range(2):
        resp = await _request(
            "POST", "/api/v1/collectors", json={"name": f"race-c-{uuid.uuid4().hex[:8]}", "collector_type": "central"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        cid = resp.json()["id"]
        cap_resp = await _request(
            "POST", f"/api/v1/collectors/{cid}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers,
        )
        assert cap_resp.status_code == 204
        collector_ids.append(cid)

    integ_resp = await _request(
        "POST", "/api/v1/integrations",
        json={"name": f"race-i-{uuid.uuid4().hex[:8]}", "integration_type": "icmp", "target_host": "127.0.0.1"},
        headers=headers,
    )
    assert integ_resp.status_code == 201, integ_resp.text
    integration_id = integ_resp.json()["id"]

    results = await asyncio.gather(
        *[
            _request(
                "POST", f"/api/v1/collectors/{collector_ids[i % 2]}/assignments",
                json={"integration_id": integration_id}, headers=headers,
            )
            for i in range(20)
        ]
    )
    statuses = [r.status_code for r in results]
    assert all(s in (201, 409) for s in statuses), f"expected only 201 (won) or 409 (raced out), got {statuses}"
    assert statuses.count(201) >= 1, "at least one reassignment must have succeeded"

    open_count = (
        await db_session.execute(
            text("SELECT count(*) FROM collector_assignment WHERE integration_id = :i AND effective_to IS NULL"),
            {"i": integration_id},
        )
    ).scalar_one()
    assert open_count == 1, f"expected exactly one currently-open assignment row, found {open_count}"


async def test_concurrent_heartbeats_are_all_recorded_without_loss(make_user, db_session, per_request_client):
    """Heartbeats are append-only (no uniqueness constraint to race against) -- fifteen
    genuinely concurrent heartbeat requests from the same collector must all succeed
    and all be persisted, proving the append-only write path has no lost updates under
    concurrent connections."""
    user = await make_user("conc-collector-3@example.com", "correct horse battery staple", "DCIM Manager")
    headers = await _login(user.email, "correct horse battery staple")

    resp = await _request(
        "POST", "/api/v1/collectors", json={"name": f"race-hb-{uuid.uuid4().hex[:8]}", "collector_type": "central"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    collector = resp.json()
    collector_id = uuid.UUID(collector["id"])

    async def _send_heartbeat(i: int):
        body = {"queue_depth": i, "status": "ok"}
        raw_body = json.dumps(body).encode()
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        sig = compute_signature(secret=collector["secret"], collector_id=collector_id, timestamp=ts, nonce=nonce, raw_body=raw_body)
        collector_headers = {
            "X-Collector-Id": str(collector_id), "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
            "X-Collector-Signature": sig, "Content-Type": "application/json",
        }
        return await _request("POST", f"/api/v1/collectors/{collector_id}/heartbeat", content=raw_body, headers=collector_headers)

    results = await asyncio.gather(*[_send_heartbeat(i) for i in range(15)])
    assert all(r.status_code == 204 for r in results), [r.status_code for r in results]

    count = (
        await db_session.execute(text("SELECT count(*) FROM collector_heartbeat WHERE collector_id = :c"), {"c": str(collector_id)})
    ).scalar_one()
    assert count == 15


async def test_concurrent_nonce_replay_only_one_request_succeeds(make_user, db_session, per_request_client):
    """The single most safety-critical concurrency case in the collector trust
    boundary: the SAME signed request (identical timestamp, nonce, and signature) sent
    genuinely concurrently must have exactly one winner. This is the real race the
    `CollectorRequestNonce` unique-constraint-based atomic claim exists to close --
    a sequential replay test alone cannot prove it, since a sequential test can't rule
    out a check-then-insert gap that only shows up under true concurrency."""
    user = await make_user("conc-collector-4@example.com", "correct horse battery staple", "DCIM Manager")
    headers = await _login(user.email, "correct horse battery staple")

    resp = await _request(
        "POST", "/api/v1/collectors", json={"name": f"race-nonce-{uuid.uuid4().hex[:8]}", "collector_type": "central"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    collector = resp.json()
    collector_id = uuid.UUID(collector["id"])

    body = {"queue_depth": 1, "status": "ok"}
    raw_body = json.dumps(body).encode()
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    sig = compute_signature(secret=collector["secret"], collector_id=collector_id, timestamp=ts, nonce=nonce, raw_body=raw_body)
    collector_headers = {
        "X-Collector-Id": str(collector_id), "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
        "X-Collector-Signature": sig, "Content-Type": "application/json",
    }

    results = await asyncio.gather(
        *[
            _request("POST", f"/api/v1/collectors/{collector_id}/heartbeat", content=raw_body, headers=collector_headers)
            for _ in range(10)
        ]
    )
    statuses = [r.status_code for r in results]
    assert statuses.count(204) == 1, f"expected exactly one accepted heartbeat, got {statuses}"
    assert statuses.count(401) == 9, f"expected the other nine replays rejected, got {statuses}"

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM collector_request_nonce WHERE collector_id = :c AND nonce = :n"),
            {"c": str(collector_id), "n": nonce},
        )
    ).scalar_one()
    assert count == 1, "the nonce must be claimed exactly once, not once per racing request"
