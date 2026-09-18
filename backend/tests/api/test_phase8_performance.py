"""Phase 8 performance measurement (master prompt §26): measured, not assumed, query
counts at 1/10/100/500 collectors with a representative integration/heartbeat/
assignment per collector. Mirrors tests/api/test_power.py's own
`_count_queries`/`before_cursor_execute` pattern exactly -- the same query-count-growth
regression style Phase 3 established for its own N+1 guard."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.application.collector_service import declare_capabilities, register_collector
from app.domain.integration.models import CollectorHeartbeat
from app.domain.integration.models import Integration as IntegrationModel


async def _count_queries(db_engine, coro):
    count = 0

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        await coro
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    return count


async def _build_collectors_with_heartbeats(db_session, n: int) -> None:
    """Each collector gets one declared ICMP capability, one integration assigned to
    it, and one recent heartbeat -- a representative, not minimal, fixture (a bare
    collector with no heartbeat short-circuits `classify_collector_health` before it
    ever reaches the per-row query this measurement is checking)."""
    for _i in range(n):
        collector, _secret = await register_collector(
            db_session, name=f"perf-collector-{uuid.uuid4().hex[:10]}", collector_type="central", site_id=None,
            version_string="1.0", actor_user_id=uuid.uuid4(), request_id=None, correlation_id=None,
        )
        await declare_capabilities(db_session, collector_id=collector.id, protocol_codes=["icmp"])
        db_session.add(
            CollectorHeartbeat(
                id=uuid.uuid4(), collector_id=collector.id, ts=datetime.now(UTC), queue_depth=0,
                cpu_pct=1.0, mem_pct=1.0, status="ok",
            )
        )
        integration = IntegrationModel(
            id=uuid.uuid4(), name=f"perf-integration-{uuid.uuid4().hex[:10]}", integration_type="icmp",
            site_id=None, enabled=True, target_host="127.0.0.1", target_port=None, config={},
            credential_ciphertext=None, poll_interval_seconds=60, version=1,
        )
        db_session.add(integration)
    await db_session.commit()


@pytest.mark.parametrize("n", [1, 10, 100, 500])
async def test_list_collectors_query_count_at_scale(client, auth_headers, db_session, db_engine, n):
    """Measured at each scale independently (not cumulative) so the numbers below are
    directly comparable per-n, not confounded by carryover rows from a previous
    parametrized case."""
    headers = await auth_headers("DCIM Manager")
    await _build_collectors_with_heartbeats(db_session, n)

    queries = await _count_queries(db_engine, client.get("/api/v1/collectors", headers=headers))
    print(f"\n[PHASE8-PERF] GET /collectors at n={n} collectors: {queries} queries")


async def test_list_collectors_query_count_does_not_grow_linearly(client, auth_headers, db_session, db_engine):
    """The actual regression guard: query count at 100 collectors must not be anywhere
    near 10x the query count at 10 -- a per-collector heartbeat lookup loop (the
    N+1 this test exists to catch) grows ~1:1 with collector count."""
    headers = await auth_headers("DCIM Manager")

    await _build_collectors_with_heartbeats(db_session, 10)
    queries_at_10 = await _count_queries(db_engine, client.get("/api/v1/collectors", headers=headers))

    await _build_collectors_with_heartbeats(db_session, 90)  # cumulative -- 100 total
    queries_at_100 = await _count_queries(db_engine, client.get("/api/v1/collectors", headers=headers))

    assert queries_at_10 < 50, f"unexpectedly high query count even at n=10: {queries_at_10}"
    assert queries_at_100 < queries_at_10 * 3, (
        f"query count grew from {queries_at_10} (n=10) to {queries_at_100} (n=100) -- "
        "this is the N+1 signature classify_collector_health's per-row call produces"
    )


async def test_list_integrations_query_count_does_not_grow_linearly(client, auth_headers, db_session, db_engine):
    """Same shape of guard for GET /integrations, whose `_to_out` calls
    `current_assignment` once per integration."""
    headers = await auth_headers("DCIM Manager")

    for _ in range(10):
        db_session.add(
            IntegrationModel(
                id=uuid.uuid4(), name=f"perf-i-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
                enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
                poll_interval_seconds=60, version=1,
            )
        )
    await db_session.commit()
    queries_at_10 = await _count_queries(db_engine, client.get("/api/v1/integrations", headers=headers))

    for _ in range(90):
        db_session.add(
            IntegrationModel(
                id=uuid.uuid4(), name=f"perf-i-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
                enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
                poll_interval_seconds=60, version=1,
            )
        )
    await db_session.commit()
    queries_at_100 = await _count_queries(db_engine, client.get("/api/v1/integrations", headers=headers))

    assert queries_at_10 < 50, f"unexpectedly high query count even at n=10: {queries_at_10}"
    assert queries_at_100 < queries_at_10 * 3, (
        f"query count grew from {queries_at_10} (n=10) to {queries_at_100} (n=100) -- "
        "this is the N+1 signature current_assignment's per-row call produces"
    )
