"""Independent performance re-measurement for PHASE8_FINAL_CLOSURE_VALIDATION.md
(master prompt: "Independently re-measure ... Report actual SQL query counts. Do not
repeat old numbers without measuring them."). Reuses the SAME `before_cursor_execute`
counting technique `tests/api/test_phase8_performance.py` established (there is only
one correct way to count real SQL statements -- reusing a correct measurement
technique is not the same as reusing a claimed result), but is a fresh, independently
written measurement against GET /collectors, GET /integrations, and
run_polling_cycle, printed for the closure report rather than assumed."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.application.collector_service import declare_capabilities, register_collector, run_polling_cycle
from app.domain.integration.models import CollectorAssignment, CollectorHeartbeat
from app.domain.integration.models import Integration as IntegrationModel


async def _count_queries(db_engine, coro):
    count = 0

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        result = await coro
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    return count, result


async def _seed_collectors(db_session, n: int):
    for _i in range(n):
        collector, _secret = await register_collector(
            db_session, name=f"final-perf-{uuid.uuid4().hex[:10]}", collector_type="central", site_id=None,
            version_string="1.0", actor_user_id=uuid.uuid4(), request_id=None, correlation_id=None,
        )
        await declare_capabilities(db_session, collector_id=collector.id, protocol_codes=["icmp"])
        db_session.add(
            CollectorHeartbeat(id=uuid.uuid4(), collector_id=collector.id, ts=datetime.now(UTC), queue_depth=0, cpu_pct=1.0, mem_pct=1.0, status="ok")
        )
        integration = IntegrationModel(
            id=uuid.uuid4(), name=f"final-perf-int-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
            enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
            poll_interval_seconds=60, version=1,
        )
        db_session.add(integration)
    await db_session.commit()


@pytest.mark.parametrize("n", [1, 10, 100, 500])
async def test_final_get_collectors_query_count(client, auth_headers, db_session, db_engine, n):
    headers = await auth_headers("DCIM Manager")
    await _seed_collectors(db_session, n)
    count, resp = await _count_queries(db_engine, client.get("/api/v1/collectors", headers=headers))
    assert resp.status_code == 200
    assert len(resp.json()) == n
    print(f"\nFINAL_PERF GET /api/v1/collectors n={n} query_count={count}")


@pytest.mark.parametrize("n", [1, 10, 100, 500])
async def test_final_get_integrations_query_count(client, auth_headers, db_session, db_engine, n):
    headers = await auth_headers("DCIM Manager")
    for _i in range(n):
        integration = IntegrationModel(
            id=uuid.uuid4(), name=f"final-perf-only-int-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
            enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
            poll_interval_seconds=60, version=1,
        )
        db_session.add(integration)
    await db_session.commit()
    count, resp = await _count_queries(db_engine, client.get("/api/v1/integrations", headers=headers))
    assert resp.status_code == 200
    assert len(resp.json()) == n
    print(f"\nFINAL_PERF GET /api/v1/integrations n={n} query_count={count}")


@pytest.mark.parametrize("n", [1, 10, 50])
async def test_final_run_polling_cycle_query_count(db_session, db_engine, n):
    collector, _secret = await register_collector(
        db_session, name=f"final-poll-collector-{uuid.uuid4().hex[:10]}", collector_type="central", site_id=None,
        version_string="1.0", actor_user_id=uuid.uuid4(), request_id=None, correlation_id=None,
    )
    for _i in range(n):
        # 127.0.0.1 (loopback), not an unreachable address -- matching the conditions
        # the ORIGINAL I5 measurement used (tests/api/test_phase8_independent_performance.py),
        # so this re-measurement actually exercises the SUCCESS path (ingest_discovery
        # runs per integration), which is what I5 is about. A first draft of this file
        # used an unreachable TEST-NET-3 address and only measured the FAILURE path
        # (query_count flat at 3 regardless of n) -- corrected here after noticing the
        # numbers contradicted the original finding, rather than reporting the wrong
        # measurement as if it settled anything.
        integration = IntegrationModel(
            id=uuid.uuid4(), name=f"final-poll-int-{uuid.uuid4().hex[:10]}", integration_type="icmp", site_id=None,
            enabled=True, target_host="127.0.0.1", target_port=None, config={}, credential_ciphertext=None,
            poll_interval_seconds=60, version=1,
        )
        db_session.add(integration)
        await db_session.flush()
        db_session.add(
            CollectorAssignment(
                id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id,
                effective_from=datetime.now(UTC), effective_to=None,
            )
        )
    await db_session.commit()

    count, outcomes = await _count_queries(db_engine, run_polling_cycle(db_session, collector_id=collector.id))
    assert len(outcomes) == n
    print(f"\nFINAL_PERF run_polling_cycle n={n} query_count={count}")
