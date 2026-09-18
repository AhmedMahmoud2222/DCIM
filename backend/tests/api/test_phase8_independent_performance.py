"""Independent performance re-measurement (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md §19).
Does not trust PHASE8_IMPLEMENTATION_REPORT.md's "flat 4 queries" claim -- re-measures
from scratch using the same before_cursor_execute counting technique
tests/api/test_power.py established, applied independently to GET /collectors,
GET /integrations, and run_polling_cycle. Written by an independent auditor. No
production code is modified by this file."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event

from app.application.collector_service import declare_capabilities, register_collector
from app.domain.integration.models import CollectorHeartbeat
from app.domain.integration.models import Integration as IntegrationModel


async def _count_queries(db_engine, coro):
    count = 0

    def _cb(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _cb)
    try:
        await coro
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _cb)
    return count


async def test_independent_get_collectors_query_count_at_1_10_100_500(client, auth_headers, db_session, db_engine):
    headers = await auth_headers("DCIM Manager")
    counts = {}
    total = 0
    for n in (1, 9, 90, 400):  # cumulative additions reaching 1, 10, 100, 500 total
        for _ in range(n):
            collector, _secret = await register_collector(
                db_session, name=f"iperf-{uuid.uuid4().hex[:10]}", collector_type="central", site_id=None,
                version_string=None, actor_user_id=uuid.uuid4(), request_id=None, correlation_id=None,
            )
            db_session.add(
                CollectorHeartbeat(id=uuid.uuid4(), collector_id=collector.id, ts=datetime.now(UTC), queue_depth=0, status="ok")
            )
        total += n
        await db_session.commit()
        q = await _count_queries(db_engine, client.get("/api/v1/collectors", headers=headers))
        counts[total] = q
        print(f"\n[INDEPENDENT-PERF] GET /collectors at n={total}: {q} queries")

    assert counts[1] < 10, f"unexpectedly high even at n=1: {counts}"
    assert counts[500] <= counts[1] + 2, f"query count must stay flat, not grow with scale: {counts}"


async def test_independent_get_integrations_query_count_at_1_10_100_500(client, auth_headers, db_session, db_engine):
    headers = await auth_headers("DCIM Manager")
    counts = {}
    total = 0
    for n in (1, 9, 90, 400):
        for _ in range(n):
            db_session.add(
                IntegrationModel(
                    id=uuid.uuid4(), name=f"iperf-i-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
                    enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
                    poll_interval_seconds=60, version=1,
                )
            )
        total += n
        await db_session.commit()
        q = await _count_queries(db_engine, client.get("/api/v1/integrations", headers=headers))
        counts[total] = q
        print(f"\n[INDEPENDENT-PERF] GET /integrations at n={total}: {q} queries")

    assert counts[1] < 10, f"unexpectedly high even at n=1: {counts}"
    assert counts[500] <= counts[1] + 2, f"query count must stay flat, not grow with scale: {counts}"


async def test_independent_run_polling_cycle_query_count_does_not_grow_with_assigned_integrations(
    db_session, db_engine,
):
    """PHASE8_IMPLEMENTATION_REPORT.md §14 explicitly admits this specific measurement
    was "code-reviewed but not separately performance-tested" -- closing that gap here."""
    from app.application.collector_service import assign_integration, run_polling_cycle

    collector, _secret = await register_collector(
        db_session, name=f"iperf-poll-{uuid.uuid4().hex[:8]}", collector_type="central", site_id=None,
        version_string=None, actor_user_id=uuid.uuid4(), request_id=None, correlation_id=None,
    )
    await declare_capabilities(db_session, collector_id=collector.id, protocol_codes=["icmp"])
    await db_session.commit()

    from sqlalchemy import func, select

    from app.domain.integration.models import CollectorAssignment

    counts = {}
    for target_total in (1, 10, 50):
        existing = (
            await db_session.execute(
                select(func.count()).select_from(CollectorAssignment).where(CollectorAssignment.collector_id == collector.id)
            )
        ).scalar_one()
        for _ in range(target_total - existing):
            integ = IntegrationModel(
                id=uuid.uuid4(), name=f"iperf-poll-i-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
                enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
                poll_interval_seconds=60, version=1,
            )
            db_session.add(integ)
            await db_session.flush()
            await assign_integration(db_session, integration_id=integ.id, collector_id=collector.id, actor_user_id=uuid.uuid4())
        await db_session.commit()

        q = await _count_queries(db_engine, run_polling_cycle(db_session, collector_id=collector.id))
        counts[target_total] = q
        print(f"\n[INDEPENDENT-PERF] run_polling_cycle at {target_total} assigned integrations: {q} queries")

    # FINDING I5 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): unlike GET /collectors and
    # GET /integrations (genuinely flat after their N1 fix), run_polling_cycle's query
    # count grows roughly LINEARLY with assigned-integration count (~5-6 queries per
    # integration: ingest_discovery's own SELECT+INSERT+2x-outbox-INSERT per new
    # device, plus one Integration UPDATE flushed at final commit) -- contradicting
    # PHASE8_IMPLEMENTATION_REPORT.md §14's framing that this "mirrors the same,
    # already-measured [flat] pattern." Some of this is inherent (each newly observed
    # device genuinely needs its own row), so this is not treated as a blocking defect,
    # but the prior report's claim was inaccurate and is corrected here. This assertion
    # is a real regression guard (not a false-flat claim): it only fails if growth
    # becomes markedly WORSE than the currently-measured linear rate.
    per_integration_at_50 = (counts[50] - counts[1]) / 49
    per_integration_at_10 = (counts[10] - counts[1]) / 9
    assert per_integration_at_50 < per_integration_at_10 * 3, (
        f"run_polling_cycle's per-integration query cost got markedly worse at scale, not just linear: {counts}"
    )
