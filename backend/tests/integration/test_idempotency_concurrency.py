"""Finding H1 regression coverage (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
PHASE1_CORRECTION_REPORT.md): the original implementation's check-cache-then-write
sequence had no serialization between the check and the write, so genuinely concurrent
identical requests mostly received an incorrect 409 instead of the idempotent replay.

These tests deliberately do NOT use the `client`/`auth_headers` fixtures, because those
override `get_db` with one shared `db_session` for the whole test — fine for sequential
calls, but a single AsyncSession is not safe for concurrent use from multiple coroutines
and would not exercise the real per-request session/connection-pool behavior these tests
need to prove. Instead, each concurrent request gets its own fresh AsyncSession from a
dedicated engine (mirroring exactly how the app's own `get_db` behaves in production —
one session per request), against the same real PostgreSQL instance every other test in
this suite uses. This is a true concurrency test against a real database, not a mocked
one (per the correction prompt's §12)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.idempotency import hash_request_body
from app.main import app
from tests.conftest import TEST_DATABASE_URL


@pytest_asyncio.fixture
async def per_request_client(db_session):
    """Overrides get_db with a fresh session per call (a fresh connection from its own
    pool per request), so concurrent requests genuinely don't share a session — unlike
    the `client` fixture, which binds every request to one shared `db_session`."""
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


async def _post_managed_asset(headers: dict, idempotency_key: str, body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            "/api/v1/managed-assets", json=body, headers={**headers, "Idempotency-Key": idempotency_key}
        )


async def test_ten_concurrent_identical_requests_produce_exactly_one_resource(make_user, db_session, per_request_client):
    user = await make_user("conc-user-1@example.com", "correct horse battery staple", "Administrator")
    headers = await _login(user.email, "correct horse battery staple")

    key = str(uuid.uuid4())
    tag = f"CONC-{uuid.uuid4().hex[:8]}"
    body = {"asset_type": "rack", "asset_tag": tag}

    results = await asyncio.gather(*[_post_managed_asset(headers, key, body) for _ in range(10)])
    statuses = [r.status_code for r in results]

    assert statuses.count(201) == 10, f"expected all 10 requests to succeed idempotently, got {statuses}"
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, f"expected exactly one distinct asset id, got {ids}"

    count = (
        await db_session.execute(text("SELECT count(*) FROM managed_asset WHERE asset_tag = :t"), {"t": tag})
    ).scalar_one()
    assert count == 1

    audit_count = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'managed_asset.create' AND entity_id = :id"),
            {"id": str(ids.pop())},
        )
    ).scalar_one()
    assert audit_count == 1, "expected exactly one audit row, not one per concurrent request"


async def test_twenty_concurrent_identical_requests_produce_exactly_one_resource(make_user, db_session, per_request_client):
    user = await make_user("conc-user-2@example.com", "correct horse battery staple", "Administrator")
    headers = await _login(user.email, "correct horse battery staple")

    key = str(uuid.uuid4())
    tag = f"CONC20-{uuid.uuid4().hex[:8]}"
    body = {"asset_type": "rack", "asset_tag": tag}

    results = await asyncio.gather(*[_post_managed_asset(headers, key, body) for _ in range(20)])
    statuses = [r.status_code for r in results]

    assert statuses.count(201) == 20, f"expected all 20 requests to succeed idempotently, got {statuses}"
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1


async def test_same_key_different_payload_is_rejected_deterministically(make_user, db_session, per_request_client):
    user = await make_user("conc-user-3@example.com", "correct horse battery staple", "Administrator")
    headers = await _login(user.email, "correct horse battery staple")

    key = str(uuid.uuid4())
    first = await _post_managed_asset(headers, key, {"asset_type": "rack", "asset_tag": f"CONFLICT-A-{uuid.uuid4().hex[:6]}"})
    assert first.status_code == 201

    second = await _post_managed_asset(
        headers, key, {"asset_type": "rack", "asset_tag": f"CONFLICT-B-{uuid.uuid4().hex[:6]}"}
    )
    assert second.status_code == 422
    assert "different request body" in second.json()["detail"]


async def test_failed_first_request_does_not_permanently_poison_the_key(make_user, db_session, per_request_client):
    """A request that claims the key but then fails (here: the asset_tag collides with an
    existing, unrelated row, so the INSERT itself raises) must release the key so a retry
    with the same Idempotency-Key and a valid body can still succeed."""
    user = await make_user("conc-user-4@example.com", "correct horse battery staple", "Administrator")
    headers = await _login(user.email, "correct horse battery staple")

    existing_tag = f"EXISTING-{uuid.uuid4().hex[:6]}"
    pre = await _post_managed_asset(headers, str(uuid.uuid4()), {"asset_type": "rack", "asset_tag": existing_tag})
    assert pre.status_code == 201

    key = str(uuid.uuid4())
    failing = await _post_managed_asset(headers, key, {"asset_type": "rack", "asset_tag": existing_tag})
    assert failing.status_code == 409

    row = (
        await db_session.execute(text("SELECT status FROM idempotency_key WHERE key = :k"), {"k": key})
    ).scalar_one_or_none()
    assert row is None, "a failed claim must be released (deleted), not left stuck as 'processing'"

    retry_tag = f"RETRY-{uuid.uuid4().hex[:6]}"
    retry = await _post_managed_asset(headers, key, {"asset_type": "rack", "asset_tag": retry_tag})
    assert retry.status_code == 201, "the same key must be claimable again after the first attempt failed"


async def test_stale_processing_claim_is_reclaimed(make_user, db_session, per_request_client):
    """Simulates a claimant that crashed after claiming but before completing: manually
    back-dates a 'processing' row past STALE_CLAIM_TIMEOUT, then verifies a new request
    with the same key reclaims it and completes successfully rather than waiting forever
    or returning a false conflict."""
    user = await make_user("conc-user-5@example.com", "correct horse battery staple", "Administrator")
    headers = await _login(user.email, "correct horse battery staple")

    key = str(uuid.uuid4())
    tag = f"STALE-{uuid.uuid4().hex[:6]}"
    body = {"asset_type": "rack", "asset_tag": tag, "serial_number": None}
    request_hash = hash_request_body(body)
    stale_time = datetime.now(UTC) - timedelta(seconds=60)
    await db_session.execute(
        text(
            "INSERT INTO idempotency_key (id, key, endpoint, request_hash, status, response_status, "
            "response_body, expires_at, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :key, 'POST:/managed-assets', :hash, 'processing', NULL, NULL, "
            "now() + interval '1 day', :t, :t)"
        ),
        {"key": key, "hash": request_hash, "t": stale_time},
    )
    await db_session.commit()

    resp = await _post_managed_asset(headers, key, {"asset_type": "rack", "asset_tag": tag})
    assert resp.status_code == 201, resp.text
