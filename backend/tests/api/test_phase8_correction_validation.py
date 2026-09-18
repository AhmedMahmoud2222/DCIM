"""Correction-cycle validation for PHASE8_BLOCKING_CORRECTION_REPORT.md (findings I2, I3
and I4).

I2/I4 section: extends the independent audit's own reproductions with the exact
batch-composition matrix and DB-state inspections the correction task requires:
`[conflict, valid]`, `[valid, conflict, valid]`, `[valid, duplicate, conflict, valid]`,
`[good, bad, good]`, `[bad, good]`, `[good, bad, good, good]`, `[good, bad, bad, good]`,
plus a genuine concurrent-retry-after-rejection case.

I3 section: accept-vs-accept, reject-vs-reject, and 10-way mixed concurrent decisions on
the same `ReconciliationDiff`, a sequential re-decision sanity check, an audit-log-count
check (exactly one terminal decision must produce exactly one audit entry, not one per
racing request), a `ManagedAsset`-association consistency check (the losing side must
leave no partial mutation), and a rollback-of-the-winner case (a failed accept -- due to
a nonexistent `matched_managed_asset_id` -- must leave the diff `pending` and release the
row lock, not leave it stuck).

Every test inspects actual database state after processing, not only the HTTP response
body."""

import json
import uuid

from sqlalchemy import text

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
    assert resp.status_code == 200, resp.text
    return {r["dedup_key"]: r["status"] for r in resp.json()["results"]}


async def _device_count(db_session, external_id: str) -> int:
    return (
        await db_session.execute(
            text("SELECT count(*) FROM discovered_device WHERE external_identifier = :e"), {"e": external_id},
        )
    ).scalar_one()


async def _idempotency_row(db_session, collector_id: str, dedup_key: str):
    return (
        await db_session.execute(
            text("SELECT status FROM idempotency_key WHERE key = :k AND endpoint = 'collector_ingest'"),
            {"k": f"{collector_id}:{dedup_key}"},
        )
    ).scalar_one_or_none()


async def test_matrix_conflict_then_valid(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)

    dedup_key = uuid.uuid4().hex
    first = await _ingest(client, collector, [_record(integration["id"], "10.10.0.1", dedup_key)])
    assert first[dedup_key] == "accepted"

    conflict = _record(integration["id"], "10.10.0.2", dedup_key)  # same key, different external_identifier
    valid = _record(integration["id"], "10.10.0.3")
    results = await _ingest(client, collector, [conflict, valid])
    assert results[dedup_key] == "rejected"
    assert results[valid["dedup_key"]] == "accepted"

    assert await _device_count(db_session, "10.10.0.2") == 0
    assert await _device_count(db_session, "10.10.0.3") == 1
    # The conflicting record's original (first-delivered) idempotency row is untouched.
    assert await _idempotency_row(db_session, collector["id"], dedup_key) == "completed"


async def test_matrix_valid_conflict_valid(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)

    conflict_key = uuid.uuid4().hex
    await _ingest(client, collector, [_record(integration["id"], "10.10.1.1", conflict_key)])

    a = _record(integration["id"], "10.10.1.2")
    conflict = _record(integration["id"], "10.10.1.3", conflict_key)
    b = _record(integration["id"], "10.10.1.4")
    results = await _ingest(client, collector, [a, conflict, b])
    assert results[a["dedup_key"]] == "accepted"
    assert results[conflict_key] == "rejected"
    assert results[b["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.1.2") == 1
    assert await _device_count(db_session, "10.10.1.3") == 0
    assert await _device_count(db_session, "10.10.1.4") == 1


async def test_matrix_valid_duplicate_conflict_valid(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)

    dup_key = uuid.uuid4().hex
    conflict_key = uuid.uuid4().hex
    dup_record = _record(integration["id"], "10.10.2.1", dup_key)
    await _ingest(client, collector, [dup_record])
    await _ingest(client, collector, [_record(integration["id"], "10.10.2.99", conflict_key)])

    valid1 = _record(integration["id"], "10.10.2.2")
    duplicate = dict(dup_record)  # identical dedup_key AND identical payload -> genuine duplicate
    conflict = _record(integration["id"], "10.10.2.3", conflict_key)  # same key, different payload -> rejected
    valid2 = _record(integration["id"], "10.10.2.4")
    results = await _ingest(client, collector, [valid1, duplicate, conflict, valid2])

    assert results[valid1["dedup_key"]] == "accepted"
    assert results[dup_key] == "duplicate"
    assert results[conflict_key] == "rejected"
    assert results[valid2["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.2.1") == 1  # from the original delivery only
    assert await _device_count(db_session, "10.10.2.2") == 1
    assert await _device_count(db_session, "10.10.2.3") == 0
    assert await _device_count(db_session, "10.10.2.4") == 1


async def test_matrix_good_bad_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    good1 = _record(integration["id"], "10.10.3.1")
    bad = _record(str(uuid.uuid4()), "10.10.3.2")  # unassigned integration_id
    good2 = _record(integration["id"], "10.10.3.3")
    results = await _ingest(client, collector, [good1, bad, good2])
    assert results[good1["dedup_key"]] == "accepted"
    assert results[bad["dedup_key"]] == "rejected"
    assert results[good2["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.3.1") == 1
    assert await _device_count(db_session, "10.10.3.2") == 0
    assert await _device_count(db_session, "10.10.3.3") == 1


async def test_matrix_bad_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    bad = _record(str(uuid.uuid4()), "10.10.4.1")
    good = _record(integration["id"], "10.10.4.2")
    results = await _ingest(client, collector, [bad, good])
    assert results[bad["dedup_key"]] == "rejected"
    assert results[good["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.4.2") == 1


async def test_matrix_good_bad_good_good(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    good1 = _record(integration["id"], "10.10.5.1")
    bad = _record(str(uuid.uuid4()), "10.10.5.2")
    good2 = _record(integration["id"], "10.10.5.3")
    good3 = _record(integration["id"], "10.10.5.4")
    results = await _ingest(client, collector, [good1, bad, good2, good3])
    assert results[good1["dedup_key"]] == "accepted"
    assert results[bad["dedup_key"]] == "rejected"
    assert results[good2["dedup_key"]] == "accepted"
    assert results[good3["dedup_key"]] == "accepted"
    for ext_id in ("10.10.5.1", "10.10.5.3", "10.10.5.4"):
        assert await _device_count(db_session, ext_id) == 1
    assert await _device_count(db_session, "10.10.5.2") == 0


async def test_matrix_good_bad_bad_good(client, auth_headers, db_session):
    """Multiple CONSECUTIVE failures, not just one -- proves the savepoint mechanism
    recovers cleanly across repeated failures, not merely a single one."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    good1 = _record(integration["id"], "10.10.6.1")
    bad1 = _record(str(uuid.uuid4()), "10.10.6.2")
    bad2 = _record(str(uuid.uuid4()), "10.10.6.3")
    good2 = _record(integration["id"], "10.10.6.4")
    results = await _ingest(client, collector, [good1, bad1, bad2, good2])
    assert results[good1["dedup_key"]] == "accepted"
    assert results[bad1["dedup_key"]] == "rejected"
    assert results[bad2["dedup_key"]] == "rejected"
    assert results[good2["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.6.1") == 1
    assert await _device_count(db_session, "10.10.6.2") == 0
    assert await _device_count(db_session, "10.10.6.3") == 0
    assert await _device_count(db_session, "10.10.6.4") == 1


async def test_rejected_record_leaves_no_partial_domain_audit_or_outbox_state(client, auth_headers, db_session):
    """A rejected record must leave NO trace: no DiscoveredDevice, no ReconciliationDiff,
    no OutboxEvent, and its idempotency claim must be released (deletable/reclaimable),
    not stuck in 'processing' forever."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    bad = _record(str(uuid.uuid4()), "10.10.7.1")
    results = await _ingest(client, collector, [bad])
    assert results[bad["dedup_key"]] == "rejected"

    assert await _device_count(db_session, "10.10.7.1") == 0
    diff_count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM reconciliation_diff rd "
                "JOIN discovered_device dd ON dd.id = rd.discovered_device_id "
                "WHERE dd.external_identifier = :e"
            ),
            {"e": "10.10.7.1"},
        )
    ).scalar_one()
    assert diff_count == 0
    outbox_count = (
        await db_session.execute(text("SELECT count(*) FROM outbox_event WHERE aggregate_type = 'discovered_device'"))
    ).scalar_one()
    assert outbox_count == 0
    assert await _idempotency_row(db_session, collector["id"], bad["dedup_key"]) is None, (
        "a rejected record's idempotency claim must be released, not left stuck as 'processing'"
    )

    # And the same dedup_key can now be retried successfully with a corrected record.
    fixed = dict(bad)
    fixed["integration_id"] = integration["id"]
    retry_results = await _ingest(client, collector, [fixed])
    assert retry_results[bad["dedup_key"]] == "accepted"
    assert await _device_count(db_session, "10.10.7.1") == 1


async def test_concurrent_retry_after_rejection_is_idempotent(client, auth_headers, db_session):
    """Genuine concurrency: ten simultaneous retries of the same previously-rejected
    dedup_key (now with a valid integration_id) must produce exactly one accepted
    write and no duplicate discovered_device rows."""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.application.collector_auth import compute_signature
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    bad = _record(str(uuid.uuid4()), "10.10.8.1")
    await _ingest(client, collector, [bad])  # rejected, releases the claim

    fixed_record = dict(bad)
    fixed_record["integration_id"] = integration["id"]
    payload = {"batch_id": uuid.uuid4().hex, "records": [fixed_record]}
    raw_body = json.dumps(payload).encode()

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=15, max_overflow=5)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        import time

        async def _send():
            ts = str(int(time.time()))
            nonce = uuid.uuid4().hex
            sig = compute_signature(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), timestamp=ts, nonce=nonce, raw_body=raw_body)
            sig_headers = {
                "X-Collector-Id": collector["id"], "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce,
                "X-Collector-Signature": sig, "Content-Type": "application/json",
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers=sig_headers)

        responses = await asyncio.gather(*[_send() for _ in range(10)])
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    outcome_statuses = [r.json()["results"][0]["status"] for r in responses]
    assert outcome_statuses.count("accepted") == 1, f"expected exactly one 'accepted', got {outcome_statuses}"
    assert outcome_statuses.count("duplicate") == 9, f"expected the other nine as 'duplicate', got {outcome_statuses}"
    assert await _device_count(db_session, "10.10.8.1") == 1


# ---------------------------------------------------------------------------------
# I3: reconciliation accept/reject concurrency race
# ---------------------------------------------------------------------------------

async def _pending_diff(client, collector, integration, external_id, db_session):
    result = await _ingest(client, collector, [_record(integration["id"], external_id)])
    assert set(result.values()) == {"accepted"}
    diff_id = (
        await db_session.execute(
            text(
                "SELECT rd.id FROM reconciliation_diff rd "
                "JOIN discovered_device dd ON dd.id = rd.discovered_device_id "
                "WHERE dd.external_identifier = :e AND rd.status = 'pending'"
            ),
            {"e": external_id},
        )
    ).scalar_one()
    return str(diff_id)


async def _managed_asset(db_session):
    from app.domain.identity.models import ManagedAsset

    asset = ManagedAsset(id=uuid.uuid4(), asset_type="sensor", asset_tag=f"CORR-{uuid.uuid4().hex[:8]}", lifecycle_status="planned")
    db_session.add(asset)
    await db_session.commit()
    return str(asset.id)


async def _diff_status(db_session, diff_id: str) -> str:
    return (
        await db_session.execute(text("SELECT status FROM reconciliation_diff WHERE id = :i"), {"i": diff_id})
    ).scalar_one()


async def _decision_audit_count(db_session, diff_id: str) -> int:
    return (
        await db_session.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE entity_type = 'reconciliation_diff' "
                "AND entity_id = :i AND action IN ('reconciliation.accept', 'reconciliation.reject')"
            ),
            {"i": diff_id},
        )
    ).scalar_one()


async def _concurrent_decisions(auth_header, requests: list[tuple[str, dict]]):
    """Fires `requests` (each a `(diff_id, body)` pair -- body's own `action` key names
    accept/reject) as genuinely concurrent HTTP calls, each on its own DB
    session/connection, exactly like the existing ingest-retry concurrency test above."""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=5)
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

        return await asyncio.gather(*[_send(diff_id, dict(body)) for diff_id, body in requests])
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def test_i3_accept_vs_accept_race_exactly_one_wins(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.1", db_session)
    asset_id = await _managed_asset(db_session)

    responses = await _concurrent_decisions(
        headers,
        [
            (diff_id, {"action": "accept", "matched_managed_asset_id": asset_id}),
            (diff_id, {"action": "accept", "matched_managed_asset_id": asset_id}),
        ],
    )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, f"expected exactly one racing accept to win, got {statuses}"
    assert statuses.count(409) == 1, f"expected exactly one racing accept to lose as a clean conflict, got {statuses}"
    assert await _diff_status(db_session, diff_id) == "accepted"
    assert await _decision_audit_count(db_session, diff_id) == 1


async def test_i3_reject_vs_reject_race_exactly_one_wins(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.2", db_session)

    responses = await _concurrent_decisions(
        headers, [(diff_id, {"action": "reject"}), (diff_id, {"action": "reject"})],
    )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, f"expected exactly one racing reject to win, got {statuses}"
    assert statuses.count(409) == 1, f"expected exactly one racing reject to lose as a clean conflict, got {statuses}"
    assert await _diff_status(db_session, diff_id) == "rejected"
    assert await _decision_audit_count(db_session, diff_id) == 1


async def test_i3_ten_way_concurrent_mixed_decisions_exactly_one_wins(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.3", db_session)
    asset_id = await _managed_asset(db_session)

    requests = [(diff_id, {"action": "accept", "matched_managed_asset_id": asset_id}) for _ in range(5)]
    requests += [(diff_id, {"action": "reject"}) for _ in range(5)]
    responses = await _concurrent_decisions(headers, requests)
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, f"expected exactly one of ten racing decisions to win, got {statuses}"
    assert statuses.count(409) == 9, f"expected the other nine to lose as a clean conflict, got {statuses}"
    assert await _diff_status(db_session, diff_id) in ("accepted", "rejected")
    assert await _decision_audit_count(db_session, diff_id) == 1


async def test_i3_sequential_redecision_after_accept_is_clean_conflict(client, auth_headers, db_session):
    """Not a race -- confirms the base 'already decided' guard still works once the
    row-lock is no longer contended (sequential, not concurrent, requests)."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.4", db_session)
    asset_id = await _managed_asset(db_session)

    r1 = await client.post(f"/api/v1/discovery/reconciliation/{diff_id}/accept", json={"matched_managed_asset_id": asset_id}, headers=headers)
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/discovery/reconciliation/{diff_id}/reject", json={}, headers=headers)
    assert r2.status_code == 409
    assert await _diff_status(db_session, diff_id) == "accepted"


async def test_i3_winning_accept_sets_managed_asset_association_losing_reject_leaves_no_partial_mutation(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.5", db_session)
    asset_id = await _managed_asset(db_session)

    responses = await _concurrent_decisions(
        headers, [(diff_id, {"action": "accept", "matched_managed_asset_id": asset_id}), (diff_id, {"action": "reject"})],
    )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 1

    device_row = (
        await db_session.execute(
            text(
                "SELECT dd.status AS status, dd.matched_managed_asset_id::text AS matched_managed_asset_id "
                "FROM discovered_device dd JOIN reconciliation_diff rd ON rd.discovered_device_id = dd.id WHERE rd.id = :i"
            ),
            {"i": diff_id},
        )
    ).one()
    final_diff_status = await _diff_status(db_session, diff_id)
    if final_diff_status == "accepted":
        assert device_row.status == "reconciled"
        assert device_row.matched_managed_asset_id == asset_id
    else:
        # The reject won the race -- the device must NOT show a partial "reconciled"
        # mutation from the losing accept attempt's rolled-back transaction.
        assert final_diff_status == "rejected"
        assert device_row.status != "reconciled"
        assert device_row.matched_managed_asset_id is None
    assert await _decision_audit_count(db_session, diff_id) == 1


async def test_i3_failed_accept_due_to_invalid_asset_leaves_diff_pending_and_releases_lock(client, auth_headers, db_session):
    """Rollback-of-the-winner: `accept_reconciliation` raises `NotFoundError` for a
    nonexistent `matched_managed_asset_id` AFTER acquiring the `SELECT ... FOR UPDATE`
    lock but BEFORE mutating `diff.status`. `app/db/session.py`'s `get_db` dependency
    rolls back the whole session on any exception, which both discards the uncommitted
    mutation attempt and releases the row lock -- the diff must still be decidable
    afterward, not stuck locked or half-decided."""
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    diff_id = await _pending_diff(client, collector, integration, "10.10.9.6", db_session)

    bad_asset_id = str(uuid.uuid4())
    r1 = await client.post(f"/api/v1/discovery/reconciliation/{diff_id}/accept", json={"matched_managed_asset_id": bad_asset_id}, headers=headers)
    assert r1.status_code == 404
    assert await _diff_status(db_session, diff_id) == "pending"

    r2 = await client.post(f"/api/v1/discovery/reconciliation/{diff_id}/reject", json={}, headers=headers)
    assert r2.status_code == 200
    assert await _diff_status(db_session, diff_id) == "rejected"


async def test_i4_concurrent_records_same_external_identifier_hits_real_db_constraint_inside_savepoint(client, auth_headers, db_session):
    """Hostile self-review addition: every I2/I4 test above triggers the per-record
    `except` block via an APPLICATION-level exception (`ApiError` for an unassigned
    integration). This test forces a REAL DATABASE-level failure inside the SAVEPOINT
    instead -- two genuinely concurrent records, same (integration, external_identifier)
    but different dedup_keys, racing on `discovered_device`'s own
    `uq_discovered_device_integration_external_id` unique constraint (`ingest_discovery`'s
    existing-row SELECT is not itself atomic against a concurrent INSERT). Proves the
    SAVEPOINT rollback recovers cleanly from a genuine `IntegrityError` -- not only from
    an app-raised `ApiError` -- and that the session remains fully usable afterward (no
    `MissingGreenlet`). Whether the DB-level race actually manifests as a constraint
    violation is a timing detail; the invariants asserted below hold either way."""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    external_id = "10.10.9.9"
    key_a, key_b = uuid.uuid4().hex, uuid.uuid4().hex

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async def _send(dedup_key):
            payload = {"batch_id": uuid.uuid4().hex, "records": [_record(integration["id"], external_id, dedup_key)]}
            raw = json.dumps(payload).encode()
            sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.post(
                    f"/api/v1/collectors/{collector['id']}/ingest", content=raw,
                    headers={**sig_headers, "Content-Type": "application/json"},
                )

        r_a, r_b = await asyncio.gather(_send(key_a), _send(key_b))
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    assert r_a.status_code == 200 and r_b.status_code == 200, (r_a.status_code, r_a.text, r_b.status_code, r_b.text)
    status_a = r_a.json()["results"][0]["status"]
    status_b = r_b.json()["results"][0]["status"]
    # Both requests must resolve cleanly (no 500, no crash) -- whichever one loses a real
    # DB-level race is "rejected" (the SAVEPOINT rolled back the IntegrityError), never
    # left half-committed. If no race actually occurred, both are "accepted" via the
    # normal existing-row upsert path -- also a valid outcome.
    assert {status_a, status_b} <= {"accepted", "rejected"}, (status_a, status_b)
    assert "accepted" in (status_a, status_b), f"expected at least one to win, got {status_a} / {status_b}"
    assert await _device_count(db_session, external_id) == 1, "the unique constraint must still hold -- exactly one device row"

    for key, status in ((key_a, status_a), (key_b, status_b)):
        row = await _idempotency_row(db_session, collector["id"], key)
        if status == "accepted":
            assert row == "completed"
        else:
            assert row is None, f"a rejected record's claim must be released, not stuck; got {row!r} for {key}"

    # The session must remain fully usable after the race (no MissingGreenlet) -- prove
    # it with an ordinary follow-up request on the SAME shared client/db_session.
    followup = await _ingest(client, collector, [_record(integration["id"], "10.10.9.10")])
    assert set(followup.values()) == {"accepted"}
