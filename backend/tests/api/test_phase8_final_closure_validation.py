"""Independent FINAL closure validation for Phase 8 (PHASE8_FINAL_CLOSURE_VALIDATION.md).

Written from scratch against the corrected code at HEAD e4db1aaaa95cce26ec76fe7dc079403d81999bdd
by an auditor treating PHASE8_BLOCKING_CORRECTION_REPORT.md's own claims as unproven.
Deliberately does NOT reuse assertions from tests/api/test_phase8_correction_validation.py
or tests/api/test_phase8_independent_audit_repro.py -- new adversarial cases, new
compositions, and (in the outer-commit section) genuine fault injection around the
single DB-commit call, never around domain logic. No production code is modified by
this file."""

import json
import time
import uuid

import pytest
from sqlalchemy import text

from app.application.collector_auth import compute_signature
from tests.api._phase8_helpers import create_integration, register_collector, sign_request


async def _assign(client, headers, integration_type="icmp"):
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": [integration_type]}, headers=headers)
    integration = await create_integration(client, headers, integration_type=integration_type)
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    return collector, integration


def _record(integration_id, external_id, dedup_key=None):
    return {
        "dedup_key": dedup_key or uuid.uuid4().hex, "integration_id": integration_id,
        "external_identifier": external_id, "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
    }


async def _ingest(client, collector, records, batch_id=None):
    payload = {"batch_id": batch_id or uuid.uuid4().hex, "records": records}
    raw = json.dumps(payload).encode()
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw, headers={**headers, "Content-Type": "application/json"})
    return resp


async def _device_count(db_session, external_id: str) -> int:
    return (await db_session.execute(text("SELECT count(*) FROM discovered_device WHERE external_identifier = :e"), {"e": external_id})).scalar_one()


async def _idempotency_status(db_session, collector_id: str, dedup_key: str):
    return (
        await db_session.execute(
            text("SELECT status FROM idempotency_key WHERE key = :k AND endpoint = 'collector_ingest'"),
            {"k": f"{collector_id}:{dedup_key}"},
        )
    ).scalar_one_or_none()


# =====================================================================================
# I1 -- fresh adversarial timestamp cases, independent of the correction's own matrix
# =====================================================================================

async def _heartbeat_raw(client, collector, timestamp_header: str):
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    nonce = uuid.uuid4().hex
    sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=timestamp_header, nonce=nonce, raw_body=raw_body)
    headers = {
        "X-Collector-Id": collector["id"], "X-Collector-Timestamp": timestamp_header,
        "X-Collector-Nonce": nonce, "X-Collector-Signature": sig, "Content-Type": "application/json",
    }
    return await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers=headers), nonce


@pytest.mark.parametrize(
    "timestamp_header,label",
    [
        (" 123 ", "whitespace-padded small integer"),
        ("   ", "all-whitespace"),
        ("\t\n", "tab/newline whitespace"),
        ("+" + str(int(time.time())), "explicit leading plus sign, otherwise valid"),
        ("-1", "small negative (pre-epoch, but syntactically valid)"),
        ("9" * 20, "exactly 20 digits (boundary -- must not crash)"),
        ("9" * 21, "exactly 21 digits (boundary -- must be rejected by length gate)"),
        ("-" + "9" * 19, "20-char negative (sign + 19 digits, boundary)"),
        ("9223372036854775807", "int64 max (19 digits) -- must not special-case native int width"),
        ("9223372036854775808", "int64 max + 1 -- must not crash on int64 overflow either"),
        ("0", "epoch zero -- syntactically valid, far outside window"),
        ("00000" + str(int(time.time())), "zero-padded valid timestamp"),
        ("1e10", "scientific notation string -- not a valid int literal"),
        ("123\x00456", "embedded NUL byte"),
    ],
)
async def test_i1_fresh_timestamp_adversarial_matrix(client, auth_headers, timestamp_header, label):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp, _nonce = await _heartbeat_raw(client, collector, timestamp_header)
    assert resp.status_code in (200, 204, 401), f"[{label}] got unexpected status {resp.status_code}: {resp.text}"
    assert resp.status_code != 500, f"[{label}] got an unhandled server error: {resp.text}"
    body_text = resp.text.lower()
    assert "overflowerror" not in body_text and "traceback" not in body_text, f"[{label}] leaked an internal exception: {resp.text}"


async def test_i1_valid_timestamp_within_skew_still_works(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp, _ = await _heartbeat_raw(client, collector, str(int(time.time())))
    assert resp.status_code == 204, resp.text


async def test_i1_valid_timestamp_but_invalid_signature_rejected_not_crashed(client, auth_headers):
    """Signature semantics must be unaffected by the I1 fix -- a syntactically perfect
    timestamp with a corrupted signature must still be a clean 401, not accepted."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    good_sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=ts, nonce=nonce, raw_body=raw_body)
    corrupted_sig = ("0" if good_sig[0] != "0" else "1") + good_sig[1:]
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body,
        headers={"X-Collector-Id": collector["id"], "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce, "X-Collector-Signature": corrupted_sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401, resp.text


async def test_i1_invalid_timestamp_with_otherwise_correctly_computed_signature_rejected(client, auth_headers):
    """The signature is computed OVER the timestamp string, so an out-of-range
    timestamp with a "correct" signature (correct for that exact bad string) must
    still fail at the timestamp check, before the nonce is claimed."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp, nonce = await _heartbeat_raw(client, collector, "9" * 25)
    assert resp.status_code == 401, resp.text
    assert "500" not in str(resp.status_code)
    # And the nonce must be reusable for a subsequent genuinely valid request.
    resp2, _ = await _heartbeat_raw(client, collector, str(int(time.time())))
    # different nonce than the burned one would also work, but re-verify THIS nonce
    # specifically was never claimed:
    body = {"status": "ok"}
    raw_body = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=ts, nonce=nonce, raw_body=raw_body)
    resp3 = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body,
        headers={"X-Collector-Id": collector["id"], "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce, "X-Collector-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp3.status_code == 204, f"the nonce from the rejected invalid-timestamp attempt must still be usable: {resp3.text}"


async def test_i1_secret_not_leaked_in_any_auth_error_body(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    for ts in ("9" * 25, "not-a-number", ""):
        resp, _ = await _heartbeat_raw(client, collector, ts)
        assert collector["secret"] not in resp.text


# =====================================================================================
# I2 -- fresh idempotency isolation cases and namespace enforcement
# =====================================================================================

async def test_i2_same_key_same_payload_is_idempotent_duplicate(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    rec = _record(integration["id"], "20.0.0.1")
    r1 = await _ingest(client, collector, [rec])
    assert r1.status_code == 200
    assert r1.json()["results"][0]["status"] == "accepted"
    r2 = await _ingest(client, collector, [dict(rec)])
    assert r2.status_code == 200
    assert r2.json()["results"][0]["status"] == "duplicate"
    assert await _device_count(db_session, "20.0.0.1") == 1


async def test_i2_same_key_changed_payload_rejects_only_that_record(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    key = uuid.uuid4().hex
    await _ingest(client, collector, [_record(integration["id"], "20.0.0.2", key)])
    r2 = await _ingest(client, collector, [_record(integration["id"], "20.0.0.99", key)])
    assert r2.json()["results"][0]["status"] == "rejected"
    assert await _device_count(db_session, "20.0.0.99") == 0


@pytest.mark.parametrize(
    "shape",
    ["valid_conflict", "conflict_valid_reverse", "conflict_conflict_valid", "valid_conflict_conflict_valid"],
)
async def test_i2_fresh_batch_compositions(client, auth_headers, db_session, shape):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    conflict_key = uuid.uuid4().hex
    await _ingest(client, collector, [_record(integration["id"], "20.1.0.1", conflict_key)])

    if shape == "valid_conflict":
        a = _record(integration["id"], "20.1.0.2")
        c = _record(integration["id"], "20.1.0.3", conflict_key)
        resp = await _ingest(client, collector, [a, c])
        results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
        assert results[a["dedup_key"]] == "accepted"
        assert results[conflict_key] == "rejected"
        assert await _device_count(db_session, "20.1.0.3") == 0
    elif shape == "conflict_valid_reverse":
        c = _record(integration["id"], "20.1.0.4", conflict_key)
        b = _record(integration["id"], "20.1.0.5")
        resp = await _ingest(client, collector, [c, b])
        results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
        assert results[conflict_key] == "rejected"
        assert results[b["dedup_key"]] == "accepted"
    elif shape == "conflict_conflict_valid":
        # The SAME conflicting dedup_key appearing TWICE in one batch, both with a
        # different payload than the original -- neither may leave partial state, and
        # a valid sibling after two consecutive conflicts must still succeed.
        c1 = _record(integration["id"], "20.1.0.6", conflict_key)
        c2 = _record(integration["id"], "20.1.0.7", conflict_key)
        v = _record(integration["id"], "20.1.0.8")
        resp = await _ingest(client, collector, [c1, c2, v])
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["status"] == "rejected"
        assert results[1]["status"] == "rejected"
        assert results[2]["status"] == "accepted"
        assert await _device_count(db_session, "20.1.0.6") == 0
        assert await _device_count(db_session, "20.1.0.7") == 0
        assert await _device_count(db_session, "20.1.0.8") == 1
    else:  # valid_conflict_conflict_valid
        v1 = _record(integration["id"], "20.1.0.9")
        c1 = _record(integration["id"], "20.1.0.10", conflict_key)
        c2 = _record(integration["id"], "20.1.0.11", conflict_key)
        v2 = _record(integration["id"], "20.1.0.12")
        resp = await _ingest(client, collector, [v1, c1, c2, v2])
        results = resp.json()["results"]
        assert [r["status"] for r in results] == ["accepted", "rejected", "rejected", "accepted"]
        assert await _device_count(db_session, "20.1.0.9") == 1
        assert await _device_count(db_session, "20.1.0.12") == 1


async def test_i2_dedup_key_namespace_same_collector_different_integration(client, auth_headers, db_session):
    """The idempotency key is `f"{collector_id}:{dedup_key}"` -- integration_id is NOT
    part of the namespace. Independently confirm what this actually means: the SAME
    dedup_key reused by the same collector against a DIFFERENT integration is a
    different request BODY (integration_id differs), so it is correctly treated as a
    conflict (same key, different payload), not silently accepted as a second
    unrelated record. This is a real, observable namespace property, documented here
    rather than assumed."""
    headers = await auth_headers("DCIM Manager")
    collector, integration_a = await _assign(client, headers)
    integration_b = await create_integration(client, headers, integration_type="icmp")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration_b["id"]}, headers=headers)

    shared_key = uuid.uuid4().hex
    r1 = await _ingest(client, collector, [_record(integration_a["id"], "20.2.0.1", shared_key)])
    assert r1.json()["results"][0]["status"] == "accepted"
    r2 = await _ingest(client, collector, [_record(integration_b["id"], "20.2.0.2", shared_key)])
    # Same collector, same textual dedup_key, different integration_id -> different
    # payload hash -> treated as a conflict, not a second accepted record.
    assert r2.json()["results"][0]["status"] == "rejected"
    assert await _device_count(db_session, "20.2.0.2") == 0


async def test_i2_dedup_key_namespace_isolated_per_collector(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector_a, integration_a = await _assign(client, headers)
    collector_b, integration_b = await _assign(client, headers)
    shared_key = uuid.uuid4().hex

    ra = await _ingest(client, collector_a, [_record(integration_a["id"], "20.3.0.1", shared_key)])
    rb = await _ingest(client, collector_b, [_record(integration_b["id"], "20.3.0.2", shared_key)])
    assert ra.json()["results"][0]["status"] == "accepted"
    assert rb.json()["results"][0]["status"] == "accepted", (
        "different collectors sharing the same textual dedup_key must not collide -- "
        f"the namespace must be per-collector: {rb.json()}"
    )
    assert await _device_count(db_session, "20.3.0.1") == 1
    assert await _device_count(db_session, "20.3.0.2") == 1


async def test_i2_no_stuck_processing_claim_after_rejection(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    key = uuid.uuid4().hex
    await _ingest(client, collector, [_record(integration["id"], "20.4.0.1", key)])
    r2 = await _ingest(client, collector, [_record(integration["id"], "20.4.0.2", key)])
    assert r2.json()["results"][0]["status"] == "rejected"
    status = await _idempotency_status(db_session, collector["id"], key)
    assert status == "completed", f"the ORIGINAL claim must remain completed, not left stuck; got {status!r}"


# =====================================================================================
# I4 -- fresh SAVEPOINT / partial-batch isolation compositions
# =====================================================================================

async def test_i4_fresh_composition_bad_bad_bad_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    bads = [_record(str(uuid.uuid4()), f"20.5.0.{i}") for i in range(1, 4)]
    good = _record(integration["id"], "20.5.0.9")
    resp = await _ingest(client, collector, [*bads, good])
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["rejected", "rejected", "rejected", "accepted"]
    assert await _device_count(db_session, "20.5.0.9") == 1
    # Collector identity must remain usable -- prove it with an ordinary follow-up call.
    followup = await _ingest(client, collector, [_record(integration["id"], "20.5.0.10")])
    assert followup.json()["results"][0]["status"] == "accepted"


async def test_i4_fresh_composition_good_bad_duplicate_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    dup_key = uuid.uuid4().hex
    dup_record = _record(integration["id"], "20.6.0.1", dup_key)
    await _ingest(client, collector, [dup_record])  # establish the original delivery

    good1 = _record(integration["id"], "20.6.0.2")
    bad = _record(str(uuid.uuid4()), "20.6.0.3")
    duplicate = dict(dup_record)
    good2 = _record(integration["id"], "20.6.0.4")
    resp = await _ingest(client, collector, [good1, bad, duplicate, good2])
    assert resp.status_code == 200, resp.text
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[good1["dedup_key"]] == "accepted"
    assert results[bad["dedup_key"]] == "rejected"
    assert results[dup_key] == "duplicate"
    assert results[good2["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "20.6.0.1") == 1  # only the original delivery


async def test_i4_fresh_composition_good_conflict_bad_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    conflict_key = uuid.uuid4().hex
    await _ingest(client, collector, [_record(integration["id"], "20.7.0.1", conflict_key)])

    good1 = _record(integration["id"], "20.7.0.2")
    conflict = _record(integration["id"], "20.7.0.3", conflict_key)
    bad = _record(str(uuid.uuid4()), "20.7.0.4")
    good2 = _record(integration["id"], "20.7.0.5")
    resp = await _ingest(client, collector, [good1, conflict, bad, good2])
    assert resp.status_code == 200, resp.text
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[good1["dedup_key"]] == "accepted"
    assert results[conflict_key] == "rejected"
    assert results[bad["dedup_key"]] == "rejected"
    assert results[good2["dedup_key"]] == "accepted"
    for eid, count in (("20.7.0.2", 1), ("20.7.0.3", 0), ("20.7.0.4", 0), ("20.7.0.5", 1)):
        assert await _device_count(db_session, eid) == count


async def test_i4_no_false_audit_or_outbox_state_for_rejected_records(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    good = _record(integration["id"], "20.8.0.1")
    bad = _record(str(uuid.uuid4()), "20.8.0.2")
    await _ingest(client, collector, [good, bad])

    outbox_for_bad = (
        await db_session.execute(text("SELECT count(*) FROM outbox_event WHERE payload::text LIKE '%20.8.0.2%'"))
    ).scalar_one()
    assert outbox_for_bad == 0

    device_id = (
        await db_session.execute(text("SELECT id FROM discovered_device WHERE external_identifier = :e"), {"e": "20.8.0.1"})
    ).scalar_one()
    device_discovered_count = (
        await db_session.execute(
            text("SELECT count(*) FROM outbox_event WHERE aggregate_type = 'discovered_device' AND aggregate_id = :i"),
            {"i": device_id},
        )
    ).scalar_one()
    assert device_discovered_count == 1
    reconciliation_required_count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM outbox_event oe JOIN reconciliation_diff rd ON rd.id = oe.aggregate_id "
                "WHERE oe.aggregate_type = 'reconciliation_diff' AND rd.discovered_device_id = :i"
            ),
            {"i": device_id},
        )
    ).scalar_one()
    assert reconciliation_required_count == 1


# =====================================================================================
# I3 -- fresh reconciliation concurrency cases: accept-vs-accept, reject-vs-reject,
# 10-way, winner-rollback, and lock-wait-is-not-an-error
# =====================================================================================

async def _pending_diff(client, collector, integration, external_id, db_session):
    result = await _ingest(client, collector, [_record(integration["id"], external_id)])
    assert result.json()["results"][0]["status"] == "accepted"
    return (
        await db_session.execute(
            text(
                "SELECT rd.id FROM reconciliation_diff rd JOIN discovered_device dd ON dd.id = rd.discovered_device_id "
                "WHERE dd.external_identifier = :e AND rd.status = 'pending'"
            ),
            {"e": external_id},
        )
    ).scalar_one()


async def _fresh_managed_asset(db_session):
    from app.domain.identity.models import ManagedAsset

    asset = ManagedAsset(id=uuid.uuid4(), asset_type="sensor", asset_tag=f"FINAL-{uuid.uuid4().hex[:8]}", lifecycle_status="planned")
    db_session.add(asset)
    await db_session.commit()
    return str(asset.id)


async def _concurrent_decisions(auth_header, requests: list[tuple[str, dict]]):
    import asyncio as _asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=30, max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async def _send(diff_id: str, body: dict):
            action = body.pop("action")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.post(f"/api/v1/discovery/reconciliation/{diff_id}/{action}", json=body, headers=auth_header)

        return await _asyncio.gather(*[_send(diff_id, dict(body)) for diff_id, body in requests])
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def test_i3_fresh_accept_vs_accept_25way(client, auth_headers, db_session):
    """The correction's own test file covers a 10-way mixed race; independently push
    further to 25 concurrent requests, ALL attempting accept (the hardest case for a
    row lock to serialize correctly, since every request wants the same terminal
    state, making any accidental double-success harder to notice by accident)."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "20.10.1.1", db_session)
    asset_id = await _fresh_managed_asset(db_session)

    requests = [(diff_id, {"action": "accept", "matched_managed_asset_id": asset_id}) for _ in range(25)]
    responses = await _concurrent_decisions(headers, requests)
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, f"expected exactly one of 25 racing accepts to win, got {statuses}"
    assert statuses.count(409) == 24, f"expected the other 24 to lose cleanly, got {statuses}"
    assert not any(s not in (200, 409) for s in statuses), f"no status outside {{200,409}} (no crash, no timeout error): {statuses}"

    audit_count = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_type = 'reconciliation_diff' AND entity_id = :i AND action = 'reconciliation.accept'"),
            {"i": diff_id},
        )
    ).scalar_one()
    assert audit_count == 1, f"25 racing accepts must still produce exactly ONE audit entry, got {audit_count}"


async def test_i3_fresh_reject_vs_reject_25way(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "20.10.2.1", db_session)

    requests = [(diff_id, {"action": "reject"}) for _ in range(25)]
    responses = await _concurrent_decisions(headers, requests)
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 24


async def test_i3_winner_rollback_lets_waiter_acquire_and_decide(client, auth_headers, db_session):
    """Hold the row lock via a real `SELECT ... FOR UPDATE` in a manually-controlled
    session (not going through the HTTP endpoint, so the test can choose exactly when
    to commit/rollback), launch a genuinely concurrent HTTP decision request that must
    BLOCK on that lock, then roll back the holder without ever deciding anything. The
    waiter must then acquire the row and complete its OWN legitimate decision -- not
    hang forever, not error out, not silently see a stale 'pending' read."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "20.10.3.1", db_session)

    holder_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    holder_session_factory = async_sessionmaker(bind=holder_engine, expire_on_commit=False)

    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    async def _hold_then_rollback():
        async with holder_session_factory() as holder:
            await holder.execute(text("SELECT id FROM reconciliation_diff WHERE id = :i FOR UPDATE"), {"i": diff_id})
            lock_acquired.set()
            await release_lock.wait()
            await holder.rollback()  # the "holder" never decides anything -- pure rollback

    holder_task = asyncio.create_task(_hold_then_rollback())
    await lock_acquired.wait()

    async def _release_after_delay():
        await asyncio.sleep(0.5)  # prove the waiter genuinely blocks, not a lucky race
        release_lock.set()

    delay_task = asyncio.create_task(_release_after_delay())
    waiter_result = await _concurrent_decisions(headers, [(diff_id, {"action": "reject"})])
    await holder_task
    await delay_task
    await holder_engine.dispose()

    assert waiter_result[0].status_code == 200, (
        f"the waiter must acquire the row after the holder's rollback and complete its "
        f"own legitimate decision, not error or hang: {waiter_result[0].status_code} {waiter_result[0].text}"
    )
    final_status = (
        await db_session.execute(text("SELECT status FROM reconciliation_diff WHERE id = :i"), {"i": diff_id})
    ).scalar_one()
    assert final_status == "rejected"


# =====================================================================================
# Machine trust boundary: collector HMAC must not satisfy human RBAC, and vice versa
# =====================================================================================

async def test_collector_hmac_headers_cannot_satisfy_human_rbac_endpoint(client, auth_headers):
    """A collector's own valid HMAC signature over ITS OWN registration request must
    not, by itself, grant it any authority on a human-RBAC-protected endpoint (e.g.
    creating ANOTHER collector) -- these are structurally separate dependency chains
    (`get_current_collector` vs `require_permission`/`get_current_user`), verified here
    empirically rather than only by source inspection."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"name": "should-not-be-created", "collector_type": "central", "site_id": None}
    raw = json.dumps(body).encode()
    collector_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    resp = await client.post("/api/v1/collectors", content=raw, headers={**collector_headers, "Content-Type": "application/json"})
    assert resp.status_code in (401, 403, 422), f"collector HMAC headers alone must not satisfy a human-RBAC endpoint: {resp.status_code} {resp.text}"


async def test_human_jwt_alone_cannot_satisfy_collector_endpoint(client, auth_headers):
    """The inverse: a valid human JWT bearer token, with no X-Collector-* headers at
    all, must not authenticate to a collector-only endpoint."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", json={"status": "ok"}, headers=headers,
    )
    assert resp.status_code in (401, 422), f"a human JWT alone must not authenticate as a collector: {resp.status_code} {resp.text}"


async def test_viewer_role_cannot_accept_or_reject_reconciliation(client, auth_headers, db_session):
    """Empirically confirm the RBAC seed's own claim (Viewer has `discovery:read` only,
    not `discovery:reconcile`) is actually enforced end-to-end, not merely declared."""
    manager_headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, manager_headers)
    diff_id = await _pending_diff(client, collector, integration, "20.12.0.1", db_session)

    viewer_headers = await auth_headers("Viewer")
    resp = await client.post(f"/api/v1/discovery/reconciliation/{diff_id}/reject", json={}, headers=viewer_headers)
    assert resp.status_code == 403, f"Viewer must not be able to reject a reconciliation diff: {resp.status_code} {resp.text}"
    assert await _diff_status_helper(db_session, diff_id) == "pending"


async def _diff_status_helper(db_session, diff_id: str) -> str:
    return (await db_session.execute(text("SELECT status FROM reconciliation_diff WHERE id = :i"), {"i": diff_id})).scalar_one()


async def test_stale_assignment_after_reassignment_is_cleanly_rejected(client, auth_headers, db_session):
    """After Integration X is reassigned from Collector A to Collector B, Collector A's
    (now-stale) assignment must no longer authorize it to ingest for Integration X --
    `current_assignment`'s own query (`effective_to IS NULL`) is what enforces this,
    verified here end-to-end through the real ingest endpoint, not just at the DB
    constraint level `test_concurrent_reassignment_race_leaves_exactly_one_current_assignment`
    already covers."""
    headers = await auth_headers("DCIM Manager")
    collector_a, integration = await _assign(client, headers)
    collector_b = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector_b['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)

    # Reassign integration from A to B.
    resp = await client.post(f"/api/v1/collectors/{collector_b['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert resp.status_code == 201, resp.text

    stale_attempt = await _ingest(client, collector_a, [_record(integration["id"], "20.13.0.1")])
    assert stale_attempt.status_code == 200
    assert stale_attempt.json()["results"][0]["status"] == "rejected", (
        f"Collector A's stale assignment must no longer authorize ingestion after "
        f"reassignment to Collector B: {stale_attempt.json()}"
    )
    assert await _device_count(db_session, "20.13.0.1") == 0

    fresh_attempt = await _ingest(client, collector_b, [_record(integration["id"], "20.13.0.2")])
    assert fresh_attempt.json()["results"][0]["status"] == "accepted"
    assert await _device_count(db_session, "20.13.0.2") == 1


async def test_security_sweep_capability_declaration_has_no_payload_size_bound(client, auth_headers):
    """Security-sweep finding (not caused by I1-I4, pre-existing since the original
    Phase 8 implementation, not touched by this correction's diff): unlike
    `IngestRecordIn.raw_attributes` (explicitly bounded to `MAX_RAW_ATTRIBUTES_BYTES` =
    8192 after finding S1), `CapabilityDeclareIn.protocol_codes` has no `Field(...)`
    length bound at all -- an authenticated collector can declare an arbitrarily large
    list, and `declare_capabilities` loops over every entry attempting one INSERT per
    unique code. This test proves acceptance at a moderate size (not an exhaustive
    resource-exhaustion proof, which would be slow and heavy to run in CI) -- it
    documents the gap as a passing regression, since this is classified NON-BLOCKING
    (see PHASE8_FINAL_CLOSURE_VALIDATION.md): it does not violate a correctness,
    machine-trust, idempotency, or data-integrity invariant, only a resource-bounding
    hygiene gap requiring an already-authenticated collector to exploit."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    large_list = [f"unbounded-code-{i}" for i in range(5000)]
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": large_list}, headers=headers)
    assert resp.status_code == 204, (
        f"documenting current behavior: a 5000-entry capability list is currently "
        f"accepted with no size bound, got {resp.status_code} instead -- if this "
        f"assertion starts failing, either a bound was added (update this test to "
        f"reflect it) or something else broke: {resp.text}"
    )


async def test_collector_cannot_claim_authority_over_unregistered_integration_id(client, auth_headers, db_session):
    """A collector naming a well-formed but entirely nonexistent integration_id (not
    merely one assigned to another collector) must be cleanly rejected, not treated as
    implicitly authoritative just because the ID is syntactically valid."""
    headers = await auth_headers("DCIM Manager")
    collector, _integration = await _assign(client, headers)
    fake_integration_id = str(uuid.uuid4())
    resp = await _ingest(client, collector, [_record(fake_integration_id, "20.11.0.1")])
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "rejected"
    assert await _device_count(db_session, "20.11.0.1") == 0


# =====================================================================================
# CRITICAL: outer-commit failure / ambiguous ACK boundary (§8)
#
# The per-record write path is:
#   idempotency claim -> SAVEPOINT -> domain write -> idempotency completion ->
#   SAVEPOINT release -> OUTER COMMIT -> ACK
#
# These tests inject a fault ONLY around the single `await db.commit()` call that
# follows a successfully-released SAVEPOINT (app/api/v1/collectors.py line ~399) --
# never around domain logic itself. The real `AsyncSession.commit` is still invoked by
# the fault wrapper in every case below; only the wrapper's OWN control flow (whether
# it re-raises after the real commit, and on which call number) is synthetic. This
# means the underlying Postgres transaction genuinely commits or genuinely fails for
# real in each scenario -- the tests observe REAL database state, not a mocked belief
# about it.
# =====================================================================================

async def test_outer_commit_raises_after_real_commit_succeeded_yields_ambiguous_ack(client, auth_headers, db_session, monkeypatch):
    """The most important case: what if `await db.commit()` on line 399 actually
    commits at the database level, but the coroutine that called it never finds out
    (the call appears to raise -- e.g. the server acknowledged the commit but the
    connection was cut before the driver could report success back to asyncpg)? Real
    `commit()` is called first (so the domain data is genuinely, durably persisted),
    THEN a synthetic exception is raised to simulate the caller never learning that."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    external_id = "20.9.0.1"
    record = _record(integration["id"], external_id)

    # Commit sequence for one fresh single-record ingest request: #1 = nonce claim
    # (collector_auth.claim_nonce), #2 = idempotency claim (idem._try_claim), #3 = the
    # per-record OUTER commit this test targets (app/api/v1/collectors.py line ~399).
    # Verified by direct source inspection of every `await db.commit()` call site on
    # this request's path (collector_auth.py:92, idempotency.py:103, collectors.py:399).
    real_commit = db_session.commit
    call_count = {"n": 0}
    TARGET_CALL = 3

    async def _faulty_commit():
        call_count["n"] += 1
        await real_commit()  # the REAL commit always actually happens
        if call_count["n"] == TARGET_CALL:
            raise RuntimeError("simulated: connection lost after the server committed, before the driver could confirm it")

    monkeypatch.setattr(db_session, "commit", _faulty_commit)

    resp = await _ingest(client, collector, [record])
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    client_saw = result["status"]

    monkeypatch.undo()  # restore the real commit for the verification queries below
    actually_persisted = await _device_count(db_session, external_id) == 1
    idem_status = await _idempotency_status(db_session, collector["id"], record["dedup_key"])

    # Document exactly what happened -- this IS the ambiguous-ACK scenario.
    assert client_saw == "rejected", (
        f"expected the client to observe the commit-call's own apparent failure as "
        f"'rejected' (this is the defect under test, not a desired property): got {client_saw!r}"
    )
    assert actually_persisted, (
        "the domain write genuinely committed at the DB level (real commit() was called "
        "before the synthetic raise) -- this confirms the ACK the client received "
        "('rejected') did NOT match the database's actual, durable state ('accepted')."
    )
    # The critical safety property: does this ambiguity risk DUPLICATE domain state on
    # retry, or does it risk the claim being stuck "processing" forever?
    assert idem_status == "completed", (
        f"idem.complete_claim's own write is inside the SAME savepoint as the domain "
        f"write, so it commits atomically with it -- the claim must show 'completed', "
        f"not 'processing' or absent; got {idem_status!r}"
    )

    # Prove the safety property directly: a client that believed "rejected" and
    # retries the identical record must NOT create a second device row, and must NOT
    # get told "accepted" a second time (which would suggest to the collector that two
    # separate writes happened) -- it must observe "duplicate", matching the DB's own
    # already-completed claim.
    retry = await _ingest(client, collector, [record])
    assert retry.json()["results"][0]["status"] == "duplicate", (
        f"a naive collector retry after the ambiguous 'rejected' ACK must be recognized "
        f"as the already-completed claim, not create a second write: got {retry.json()}"
    )
    assert await _device_count(db_session, external_id) == 1, "no duplicate domain state must be created by the retry"


async def test_outer_commit_raises_and_release_claim_itself_then_fails(client, auth_headers, db_session, monkeypatch):
    """Worse case: the outer commit fails AND the session's subsequent statements (the
    `except` branch's own `idem.release_claim(...)`, which itself calls `db.commit()`)
    ALSO fail -- e.g. a genuinely dropped connection, not a one-shot fluke. This
    reproduces the scenario the correction report flagged as a documented, non-blocking
    residual: does this crash only the current record (per-record ACK preserved), or
    does it escape the per-record `except` and fail records the batch hasn't reached
    yet?"""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    ext_a, ext_b = "20.9.1.1", "20.9.1.2"
    record_a = _record(integration["id"], ext_a)
    record_b = _record(integration["id"], ext_b)

    # Let the two PRE-ingest commits (nonce claim + record A's idempotency claim)
    # succeed for real, then fail persistently starting from record A's own outer
    # commit onward -- so the failure is squarely inside the boundary under test, not
    # an artifact of failing authentication itself.
    real_commit = db_session.commit
    call_count = {"n": 0}
    FAIL_FROM_CALL = 3

    async def _always_faulty_commit():
        call_count["n"] += 1
        if call_count["n"] < FAIL_FROM_CALL:
            return await real_commit()
        raise RuntimeError(f"simulated persistent connection loss (call #{call_count['n']})")

    monkeypatch.setattr(db_session, "commit", _always_faulty_commit)

    # This is the load-bearing observation for classification: does a persistent
    # commit-level failure surface as a clean per-record HTTP 200 with "rejected"
    # entries (matching every OTHER failure mode's contract in this endpoint), or does
    # `release_claim`'s own commit (called from the `except` branch, uncaught by any
    # further handler) escape uncaught -- which ASGITransport surfaces to this test as
    # a raised exception, not a 500 response object, exactly as a real ASGI server
    # would log an unhandled exception and terminate the response ungracefully.
    escaped_exception = None
    resp = None
    try:
        resp = await _ingest(client, collector, [record_a, record_b])
    except RuntimeError as exc:
        escaped_exception = exc
        # Faithfully reproduce what PRODUCTION's own `get_db` does when an exception
        # propagates through it (app/db/session.py: `except Exception: await
        # session.rollback(); raise`) -- this test's dependency override (a plain
        # `yield db_session`, per tests/conftest.py) does not itself replicate that,
        # so it must be done explicitly here to inspect genuinely durable state rather
        # than a same-session dirty read of this session's own uncommitted work.
        await db_session.rollback()
    finally:
        monkeypatch.undo()

    if escaped_exception is not None:
        # CONFIRMED FINDING: a persistent (not one-shot-transient) commit-level failure
        # breaks the per-record ACK contract entirely -- `idem.release_claim`'s own
        # `db.commit()` (app/application/idempotency.py:185) is not itself guarded by
        # any further try/except, so its failure propagates out of `ingest_batch`
        # uncaught, past the FastAPI exception handler and into to a bare 500 with no
        # `IngestBatchOut` body at all -- record B is never attempted. See
        # PHASE8_FINAL_CLOSURE_VALIDATION.md's outer-commit analysis for the
        # classification of this finding.
        assert "simulated persistent connection loss" in str(escaped_exception)
    elif resp is not None and resp.status_code == 200:
        results = resp.json()["results"]
        assert len(results) == 2
        assert all(r["status"] == "rejected" for r in results)
    else:
        assert resp is not None and resp.status_code == 500

    # Regardless of which branch fired, the DB itself must not show a duplicated or
    # dangling write for a record whose commit never actually succeeded (the faulty
    # commit here NEVER calls the real commit for the target record, so nothing about
    # it should be durably persisted).
    assert await _device_count(db_session, ext_a) == 0
    assert await _device_count(db_session, ext_b) == 0


async def test_next_record_after_commit_failure_in_same_batch_still_processed(client, auth_headers, db_session, monkeypatch):
    """If the outer commit fails but `release_claim` (its own, separate commit)
    SUCCEEDS -- the transient-failure-that-recovers case -- does the NEXT record in
    the same batch still get processed normally, or does the first record's failure
    leave the shared session unusable for the rest of the batch?"""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    ext_a, ext_b = "20.9.2.1", "20.9.2.2"
    record_a = _record(integration["id"], ext_a)
    record_b = _record(integration["id"], ext_b)

    real_commit = db_session.commit
    call_count = {"n": 0}
    TARGET_CALL = 3  # nonce claim (#1), record A's idempotency claim (#2), record A's outer commit (#3)

    async def _fail_first_commit_only(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == TARGET_CALL:
            raise RuntimeError("simulated transient failure on record A's outer commit only")
        return await real_commit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", _fail_first_commit_only)
    resp = await _ingest(client, collector, [record_a, record_b])
    monkeypatch.undo()

    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["status"] == "rejected"
    assert results[1]["status"] == "accepted", (
        f"record B must still be processed normally after record A's commit failure -- "
        f"the session must remain usable for the rest of the batch: {results}"
    )
    assert await _device_count(db_session, ext_b) == 1


async def test_retry_of_same_dedup_key_after_persistent_commit_failure_is_bounded_not_permanent(client, auth_headers, db_session, monkeypatch):
    """A persistent (not transient) commit-level failure means `release_claim`'s own
    `db.commit()` also fails (see the prior test), so the idempotency claim's `DELETE
    ... WHERE status='processing'` is itself never durably committed either -- the
    claim row is left exactly where `_try_claim`'s OWN prior real commit left it:
    `status='processing'`. This test independently verifies the actual consequence,
    rather than assuming: is an immediate retry silently accepted twice, silently
    dropped, or told to wait -- and does the codebase's own existing
    `STALE_CLAIM_TIMEOUT` (30s) reclaim mechanism actually recover it, bounding the
    "stuck" duration rather than leaving it stuck indefinitely?"""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    external_id = "20.9.3.1"
    record = _record(integration["id"], external_id)

    real_commit = db_session.commit
    call_count = {"n": 0}
    TARGET_CALL = 3  # nonce claim (#1), idempotency claim (#2), outer commit (#3) -- fails from here on, persistently

    async def _always_faulty_commit(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < TARGET_CALL:
            return await real_commit(*args, **kwargs)
        raise RuntimeError("simulated persistent commit failure, real commit never reached")

    monkeypatch.setattr(db_session, "commit", _always_faulty_commit)
    try:
        await _ingest(client, collector, [record])
        first_escaped = False
    except RuntimeError:
        first_escaped = True
        # See the sibling test above for why this matches production's own `get_db`
        # rollback-on-exception behavior, which this test's harness does not otherwise
        # replicate.
        await db_session.rollback()
    finally:
        monkeypatch.undo()

    # Whether or not the first request's HTTP response completed cleanly, independently
    # inspect the actual DB row left behind.
    status_after_crash = await _idempotency_status(db_session, collector["id"], record["dedup_key"])
    assert status_after_crash == "processing", (
        f"a persistent commit failure (release_claim's own commit ALSO failing) must "
        f"leave the claim as 'processing' (neither silently 'completed' nor deleted) -- "
        f"got {status_after_crash!r} (first request's HTTP call raised: {first_escaped})"
    )
    assert await _device_count(db_session, external_id) == 0, "no domain data may exist for a record whose commit never durably succeeded"

    # An IMMEDIATE retry (well within STALE_CLAIM_TIMEOUT) must NOT be silently
    # accepted a second time and must NOT silently vanish -- it must be told to wait,
    # an honest signal that the client can act on (unlike a bare timeout with no
    # response at all).
    immediate_retry = await _ingest(client, collector, [record])
    immediate_status = immediate_retry.json()["results"][0]["status"]
    assert immediate_status == "rejected", (
        f"an immediate retry while genuinely still within the stale-claim window must "
        f"be told to wait, not silently accepted or silently dropped: got {immediate_status!r}"
    )
    assert "retry shortly" in (immediate_retry.json()["results"][0].get("error") or "").lower()

    # Directly exercise the codebase's OWN existing stale-claim reclaim path (no
    # production code modified -- this backdates the row's timestamp the same way the
    # passage of 30 real seconds would) to confirm the "stuck" state is bounded, not
    # permanent, using the mechanism that already exists for exactly this purpose.
    await db_session.execute(
        text("UPDATE idempotency_key SET updated_at = now() - interval '31 seconds' WHERE key = :k AND endpoint = 'collector_ingest'"),
        {"k": f"{collector['id']}:{record['dedup_key']}"},
    )
    await db_session.commit()

    recovered_retry = await _ingest(client, collector, [record])
    recovered_status = recovered_retry.json()["results"][0]["status"]
    assert recovered_status == "accepted", (
        f"once the claim is genuinely stale (>{30}s old), the EXISTING reclaim "
        f"mechanism must recover it and let the retry succeed -- this is not a new "
        f"behavior added by this validation, it is `_try_reclaim_stale`'s own "
        f"documented purpose; got {recovered_status!r}: {recovered_retry.json()}"
    )
    assert await _device_count(db_session, external_id) == 1
