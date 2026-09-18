"""Independent reproduction scripts for PHASE8_INDEPENDENT_RED_TEAM_REPORT.md. Written
by an independent auditor (not the Phase 8 implementer) against the actual code at HEAD
fc5fd57e59f71836828decb66dcdfb669f44b47c. Each test reproduces one specific suspected
defect found by direct source inspection, not by trusting PHASE8_IMPLEMENTATION_REPORT.md's
claims. No production code is modified by this file."""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.collector_auth import compute_signature
from app.main import app
from tests.api._phase8_helpers import create_integration, register_collector, sign_request
from tests.conftest import TEST_DATABASE_URL


@pytest.mark.xfail(
    strict=True,
    reason="Finding I1 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): datetime.fromtimestamp() "
    "in verify_collector_request is not inside the timestamp try/except -- an "
    "out-of-range X-Collector-Timestamp raises an unhandled OverflowError (500), not a "
    "clean 401. Reproduces against app/application/collector_auth.py:120.",
)
async def test_I1_oversized_timestamp_header_crashes_instead_of_clean_401(client, auth_headers):
    """Independent finding I1: `verify_collector_request` parses `X-Collector-Timestamp`
    with `int(timestamp_header)` inside a try/except, but the SUBSEQUENT
    `datetime.fromtimestamp(ts, tz=UTC)` call is NOT inside that try/except. A timestamp
    header that parses as a valid (huge) integer but is out of `datetime`'s representable
    range raises an unhandled `OverflowError`, which is not a `CollectorAuthError` and is
    not caught anywhere in the call chain (`get_current_collector` only catches
    `CollectorAuthError`) -- it should reach FastAPI's generic 500 handler instead of a
    clean 401."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    huge_timestamp = "9" * 30  # parses fine as a Python int, but out of datetime's range
    nonce = uuid.uuid4().hex
    sig = compute_signature(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=huge_timestamp,
        nonce=nonce, raw_body=raw_body,
    )
    sig_headers = {
        "X-Collector-Id": collector["id"], "X-Collector-Timestamp": huge_timestamp,
        "X-Collector-Nonce": nonce, "X-Collector-Signature": sig, "Content-Type": "application/json",
    }
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers=sig_headers)
    # A well-behaved trust boundary must reject a malformed/out-of-range timestamp with a
    # clean 4xx, never a 500. This assertion documents the DESIRED behavior; if it fails,
    # the defect is confirmed (see report finding I1).
    assert resp.status_code in (400, 401, 422), f"expected a clean 4xx, got {resp.status_code}: {resp.text}"


@pytest.mark.xfail(
    strict=True,
    reason="Finding I2 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): idem.get_or_claim() in "
    "ingest_batch is called BEFORE the per-record try/except, so an IdempotencyConflict "
    "(same dedup_key reused with a different payload) is unhandled and crashes the WHOLE "
    "batch with a 500, instead of marking only that one record 'rejected' and letting "
    "valid sibling records survive. Reproduces against app/api/v1/collectors.py:326.",
)
async def test_I2_same_dedup_key_different_payload_crashes_whole_batch(client, auth_headers):
    """Independent finding I2: inside `ingest_batch`'s per-record loop,
    `idem.get_or_claim(...)` is called BEFORE the per-record `try:` block starts. When it
    raises `IdempotencyConflict` (the exact "same dedup_key + different payload" scenario
    the master prompt's §10 explicitly requires be tested), that exception is not caught
    by the per-record `except Exception` (which only wraps the code AFTER the claim is
    obtained) -- it propagates out of the whole `ingest_batch` endpoint, turning one
    malicious/buggy record into a hard failure for the ENTIRE batch, not just that one
    record. This directly contradicts the endpoint's own docstring and the master
    prompt's explicit partial-batch-isolation requirement ("valid siblings survive
    invalid records")."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    dedup_key = uuid.uuid4().hex
    # First delivery of this dedup_key -- claims and completes normally.
    first_payload = {
        "batch_id": uuid.uuid4().hex,
        "records": [
            {
                "dedup_key": dedup_key, "integration_id": integration["id"], "external_identifier": "10.0.0.1",
                "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
            }
        ],
    }
    raw1 = json.dumps(first_payload).encode()
    sig1 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw1)
    r1 = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw1, headers={**sig1, "Content-Type": "application/json"})
    assert r1.status_code == 200
    assert r1.json()["results"][0]["status"] == "accepted"

    # Second batch reuses the SAME dedup_key with a DIFFERENT payload, alongside a
    # legitimate, unrelated, valid second record in the SAME batch.
    good_key = uuid.uuid4().hex
    mixed_payload = {
        "batch_id": uuid.uuid4().hex,
        "records": [
            {
                "dedup_key": dedup_key, "integration_id": integration["id"], "external_identifier": "10.0.0.2",  # different!
                "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
            },
            {
                "dedup_key": good_key, "integration_id": integration["id"], "external_identifier": "10.0.0.3",
                "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
            },
        ],
    }
    raw2 = json.dumps(mixed_payload).encode()
    sig2 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw2)
    r2 = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw2, headers={**sig2, "Content-Type": "application/json"})
    # DESIRED behavior: 200, with the conflicting record marked "rejected" and the
    # legitimate sibling record marked "accepted" -- proving partial-batch isolation
    # actually holds even for an idempotency-hash conflict, not just for a DB-level
    # rejection. If this fails, the defect (whole-batch crash) is confirmed.
    assert r2.status_code == 200, f"expected isolated per-record handling, got a batch-wide failure: {r2.status_code} {r2.text}"
    results = {r["dedup_key"]: r["status"] for r in r2.json()["results"]}
    assert results.get(good_key) == "accepted", f"a valid sibling record must survive a conflicting one, got: {results}"


@pytest.mark.xfail(
    strict=True,
    reason="Finding I3 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): accept_reconciliation/"
    "reject_reconciliation fetch ReconciliationDiff via a plain db.get() with no row "
    "lock and no optimistic-concurrency version check, so two genuinely concurrent "
    "requests (one accept, one reject) on the same diff can both pass the 'pending' "
    "guard and both report 200 -- a lost-update race, not a deterministic single "
    "winner. Reproduces against app/application/discovery_service.py.",
)
async def test_I3_concurrent_accept_and_reject_race_on_same_diff(make_user, db_session):
    """Independent finding I3: `accept_reconciliation`/`reject_reconciliation` both
    fetch the `ReconciliationDiff` via a plain `db.get()` (no `SELECT ... FOR UPDATE`,
    no optimistic-concurrency version check) and then unconditionally overwrite
    `diff.status`. Two genuinely concurrent requests -- one accept, one reject -- on the
    SAME diff can both pass the `diff.status != "pending"` guard (both read "pending"
    before either commits) and both report success, with only the last committed write
    actually reflected in the database -- exactly the "accept vs reject race" scenario
    the master prompt's §13/§14 explicitly requires be tested. This uses genuinely
    separate DB sessions/connections (the `per_request_client` pattern from
    tests/integration/test_idempotency_concurrency.py), not a shared session, so it is a
    real concurrency test, not a sequential simulation."""

    from app.application.collector_service import declare_capabilities
    from app.application.collector_service import register_collector as register_collector_svc
    from app.domain.identity.models import ManagedAsset
    from app.domain.integration.models import Integration as IntegrationModel

    user = await make_user("audit-i3@example.com", "correct horse battery staple", "DCIM Manager")

    collector, _secret = await register_collector_svc(
        db_session, name=f"audit-i3-collector-{uuid.uuid4().hex[:8]}", collector_type="central", site_id=None,
        version_string=None, actor_user_id=user.id, request_id=None, correlation_id=None,
    )
    await declare_capabilities(db_session, collector_id=collector.id, protocol_codes=["icmp"])
    integration = IntegrationModel(
        id=uuid.uuid4(), name=f"audit-i3-integration-{uuid.uuid4().hex[:8]}", integration_type="icmp", site_id=None,
        enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
        poll_interval_seconds=60, version=1,
    )
    db_session.add(integration)
    asset = ManagedAsset(id=uuid.uuid4(), asset_type="sensor", asset_tag=f"AUDIT-I3-{uuid.uuid4().hex[:8]}", lifecycle_status="planned")
    db_session.add(asset)
    await db_session.commit()

    from app.application.discovery_service import ingest_discovery
    device, _is_new = await ingest_discovery(
        db_session, integration_id=integration.id, external_identifier="race-device-1",
        raw_attributes={}, correlation_id=None, causation_id=None,
    )
    await db_session.commit()

    from sqlalchemy import select

    from app.domain.integration.models import ReconciliationDiff
    diff = (
        await db_session.execute(select(ReconciliationDiff).where(ReconciliationDiff.discovered_device_id == device.id))
    ).scalar_one()
    diff_id = diff.id

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            login = await ac.post("/api/v1/auth/login", json={"email": user.email, "password": "correct horse battery staple"})
            assert login.status_code == 200
            auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

            async def _accept():
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac2:
                    return await ac2.post(
                        f"/api/v1/discovery/reconciliation/{diff_id}/accept",
                        json={"matched_managed_asset_id": str(asset.id)}, headers=auth,
                    )

            async def _reject():
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac2:
                    return await ac2.post(f"/api/v1/discovery/reconciliation/{diff_id}/reject", json={}, headers=auth)

            results = await asyncio.gather(_accept(), _reject())
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    statuses = [r.status_code for r in results]
    # DESIRED behavior: exactly one of the two racing decisions succeeds (200) and the
    # other is rejected as a conflict (409) -- deterministic, matching the "Already
    # Decided" guard's own intent. If BOTH return 200, the race is confirmed: two
    # human decisions both reported success while the database holds only one.
    assert statuses.count(200) == 1, f"expected exactly one racing decision to win, got statuses {statuses}"
    assert statuses.count(409) == 1, f"expected exactly one racing decision to lose as a clean conflict, got statuses {statuses}"
