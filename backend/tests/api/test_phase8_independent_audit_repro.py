"""Independent reproduction scripts for PHASE8_INDEPENDENT_RED_TEAM_REPORT.md. Written
by an independent auditor (not the Phase 8 implementer) against the actual code at HEAD
fc5fd57e59f71836828decb66dcdfb669f44b47c. Each test reproduces one specific suspected
defect found by direct source inspection, not by trusting PHASE8_IMPLEMENTATION_REPORT.md's
claims. No production code is modified by this file."""

import asyncio
import json
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.collector_auth import compute_signature
from app.main import app
from tests.api._phase8_helpers import create_integration, register_collector, sign_request
from tests.conftest import TEST_DATABASE_URL


async def _heartbeat_with_raw_timestamp(client, collector, timestamp_header: str):
    """Signs a real heartbeat request but substitutes an arbitrary, possibly-invalid
    raw string for the timestamp header (the signature is still computed over that
    exact string, so a well-formed-but-out-of-window timestamp still signs correctly
    -- only a malformed one fails at parsing, never at signature verification)."""
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    nonce = uuid.uuid4().hex
    sig = compute_signature(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=timestamp_header,
        nonce=nonce, raw_body=raw_body,
    )
    sig_headers = {
        "X-Collector-Id": collector["id"], "X-Collector-Timestamp": timestamp_header,
        "X-Collector-Nonce": nonce, "X-Collector-Signature": sig, "Content-Type": "application/json",
    }
    return await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers=sig_headers), nonce


@pytest.mark.parametrize(
    "timestamp_header",
    [
        "9" * 30,  # extremely large positive -- previously OverflowError (Finding I1)
        "-" + "9" * 30,  # extremely large negative -- previously OverflowError (Finding I1)
        "not-a-number",  # non-integer
        "",  # empty
        "12.5",  # not a valid int literal
        "99999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999",  # 99 digits
    ],
)
async def test_I1_malformed_or_out_of_range_timestamp_rejected_cleanly(client, auth_headers, timestamp_header):
    """Finding I1 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md), CORRECTED: every one of these
    previously either crashed with an unhandled OverflowError/ValueError (a 500) or was
    inconsistently handled. `verify_collector_request` now compares entirely in integer
    epoch-seconds space and bounds the header's length before parsing, so every
    malformed or out-of-range value is rejected as a clean 401 -- never a 500, and with
    no traceback or internal detail in the response body."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp, _nonce = await _heartbeat_with_raw_timestamp(client, collector, timestamp_header)
    assert resp.status_code == 401, f"expected a clean 401, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "OverflowError" not in resp.text and "Traceback" not in resp.text
    assert "traceback" not in json.dumps(body).lower()


async def test_I1_stale_timestamp_still_rejected_cleanly(client, auth_headers):
    """A well-formed but too-old timestamp must still be rejected -- the I1 fix must not
    weaken the existing replay-window behavior for ordinary out-of-window values."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    stale = str(int(time.time()) - 10_000)
    resp, _nonce = await _heartbeat_with_raw_timestamp(client, collector, stale)
    assert resp.status_code == 401


async def test_I1_future_timestamp_still_rejected_cleanly(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    future = str(int(time.time()) + 10_000)
    resp, _nonce = await _heartbeat_with_raw_timestamp(client, collector, future)
    assert resp.status_code == 401


async def test_I1_valid_timestamp_within_skew_still_accepted(client, auth_headers):
    """Existing valid-request behavior must be unchanged by the I1 correction."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    valid = str(int(time.time()))
    resp, _nonce = await _heartbeat_with_raw_timestamp(client, collector, valid)
    assert resp.status_code == 204


async def test_I1_invalid_timestamp_does_not_consume_the_nonce(client, auth_headers, db_session):
    """A request rejected for a bad timestamp must never reach nonce claiming -- the
    same nonce must still be usable by a subsequent, validly-timestamped request."""
    from sqlalchemy import text

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    nonce = uuid.uuid4().hex

    bad_ts = "9" * 30
    bad_sig = compute_signature(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=bad_ts, nonce=nonce, raw_body=raw_body,
    )
    bad_resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body,
        headers={
            "X-Collector-Id": collector["id"], "X-Collector-Timestamp": bad_ts, "X-Collector-Nonce": nonce,
            "X-Collector-Signature": bad_sig, "Content-Type": "application/json",
        },
    )
    assert bad_resp.status_code == 401

    count = (
        await db_session.execute(text("SELECT count(*) FROM collector_request_nonce WHERE nonce = :n"), {"n": nonce})
    ).scalar_one()
    assert count == 0, "an invalid-timestamp request must never claim a nonce"

    good_ts = str(int(time.time()))
    good_sig = compute_signature(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=good_ts, nonce=nonce, raw_body=raw_body,
    )
    good_resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body,
        headers={
            "X-Collector-Id": collector["id"], "X-Collector-Timestamp": good_ts, "X-Collector-Nonce": nonce,
            "X-Collector-Signature": good_sig, "Content-Type": "application/json",
        },
    )
    assert good_resp.status_code == 204, "the same nonce must still be usable after an invalid-timestamp rejection"


async def test_I2_same_dedup_key_different_payload_no_longer_crashes_whole_batch(client, auth_headers):
    """Finding I2 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md), CORRECTED: `idem.get_or_claim`
    is now called inside its own try/except in `ingest_batch`, so an `IdempotencyConflict`
    (same dedup_key reused with a different payload) is caught and marks only that one
    record 'rejected' -- valid sibling records in the same batch are unaffected."""
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
    assert r2.status_code == 200, f"expected isolated per-record handling, got a batch-wide failure: {r2.status_code} {r2.text}"
    results = {r["dedup_key"]: r["status"] for r in r2.json()["results"]}
    assert results.get(dedup_key) == "rejected"
    assert results.get(good_key) == "accepted", f"a valid sibling record must survive a conflicting one, got: {results}"


async def test_I3_concurrent_accept_and_reject_race_no_longer_loses_a_decision(make_user, db_session):
    """Corrected behavior for Finding I3 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md):
    `accept_reconciliation`/`reject_reconciliation` now fetch the `ReconciliationDiff`
    via `SELECT ... FOR UPDATE`, so two genuinely concurrent requests -- one accept, one
    reject -- on the SAME diff are serialized on the row lock: the second request blocks
    until the first commits, then observes the now-committed non-"pending" status and
    gets a clean 409, instead of both racing past the "pending" guard. This uses
    genuinely separate DB sessions/connections (the `per_request_client` pattern from
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
    # Corrected behavior: exactly one of the two racing decisions succeeds (200) and the
    # other is rejected as a clean conflict (409) -- deterministic, matching the
    # "Already Decided" guard's own intent, with the SELECT ... FOR UPDATE row lock as
    # the actual point of serialization.
    assert statuses.count(200) == 1, f"expected exactly one racing decision to win, got statuses {statuses}"
    assert statuses.count(409) == 1, f"expected exactly one racing decision to lose as a clean conflict, got statuses {statuses}"
