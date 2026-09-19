"""Independent additional concurrency reproductions (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md
§9). Extends tests/integration/test_phase8_concurrency.py's own genuine-concurrency
pattern (dedicated per-request engine/session, not a shared session) to scenarios that
file does not cover: simultaneous duplicate ingest delivery of the identical record.
Written by an independent auditor. No production code is modified by this file."""

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


async def test_concurrent_duplicate_ingest_of_identical_record_creates_exactly_one_device(
    make_user, db_session, per_request_client,
):
    """§10 explicit test item: "simultaneous duplicate requests". Ten genuinely
    concurrent ingest requests carrying the IDENTICAL record (same dedup_key, same
    payload) from the same collector must result in exactly one accepted write and
    exactly one DiscoveredDevice row -- the reused IdempotencyKey claim mechanism is
    expected to serialize this correctly (as it already does for other endpoints), and
    this is the first genuine concurrency proof of that for the collector ingest path
    specifically."""
    user = await make_user("audit-conc-ingest@example.com", "correct horse battery staple", "DCIM Manager")
    headers = await _login(user.email, "correct horse battery staple")

    collector_resp = await _request("POST", "/api/v1/collectors", json={"name": f"audit-ci-{uuid.uuid4().hex[:8]}", "collector_type": "central"}, headers=headers)
    assert collector_resp.status_code == 201
    collector = collector_resp.json()
    collector_id = uuid.UUID(collector["id"])

    cap_resp = await _request("POST", f"/api/v1/collectors/{collector_id}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    assert cap_resp.status_code == 204

    integ_resp = await _request(
        "POST", "/api/v1/integrations",
        json={"name": f"audit-ci-integ-{uuid.uuid4().hex[:8]}", "integration_type": "icmp", "target_host": "127.0.0.1"},
        headers=headers,
    )
    assert integ_resp.status_code == 201
    integration_id = integ_resp.json()["id"]

    assign_resp = await _request("POST", f"/api/v1/collectors/{collector_id}/assignments", json={"integration_id": integration_id}, headers=headers)
    assert assign_resp.status_code == 201

    dedup_key = uuid.uuid4().hex
    payload = {
        "batch_id": uuid.uuid4().hex,
        "records": [
            {
                "dedup_key": dedup_key, "integration_id": integration_id, "external_identifier": "10.0.0.200",
                "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
            }
        ],
    }
    raw_body = json.dumps(payload).encode()

    async def _send():
        # Fresh nonce+timestamp per attempt -- a real collector retries with fresh
        # signing material, not a byte-identical replay -- so every request reaches the
        # idempotency claim race genuinely, not just the auth/nonce-uniqueness race
        # already covered by test_concurrent_nonce_replay_only_one_request_succeeds.
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        sig = compute_signature(secret=collector["secret"], collector_id=collector_id, timestamp=ts, nonce=nonce, raw_body=raw_body)
        collector_headers = {
            "X-Collector-Id": str(collector_id), "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
            "X-Collector-Signature": sig, "Content-Type": "application/json",
        }
        return await _request("POST", f"/api/v1/collectors/{collector_id}/ingest", content=raw_body, headers=collector_headers)

    results = await asyncio.gather(*[_send() for _ in range(10)])
    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    outcome_statuses = [r.json()["results"][0]["status"] for r in results]
    assert outcome_statuses.count("accepted") == 1, f"expected exactly one 'accepted', got {outcome_statuses}"
    assert outcome_statuses.count("duplicate") == 9, f"expected the other nine as 'duplicate', got {outcome_statuses}"

    count = (
        await db_session.execute(text("SELECT count(*) FROM discovered_device WHERE external_identifier = '10.0.0.200'"))
    ).scalar_one()
    assert count == 1, f"expected exactly one discovered device row, found {count}"
