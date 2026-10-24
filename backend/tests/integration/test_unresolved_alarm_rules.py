"""G8 real PostgreSQL/storage/race tests. Run only on dedicated dcim_test."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, null, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.telemetry import TelemetryBatchIn, ingest_collector_telemetry
from app.application.alarm_service import resolve_rule_unit
from app.application.telemetry_service import ingest_reading
from app.core.errors import ConflictError
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.audit.models import AuditLog
from app.domain.integration.models import Collector, CollectorAssignment, Integration
from app.domain.outbox.models import OutboxEvent
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


async def seed(db):
    now = datetime.now(UTC)
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db.add_all([collector, integration])
    await db.flush()
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="power",
                                       canonical_metric="power_kw", unit="W", scale=1, registry_version="1")
    rule = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="power_kw", rule_type="threshold_high",
                     threshold=Decimal("1500"), unit=None, registry_version=null(), name="unresolved legacy")
    db.add_all([mapping, rule])
    db.add(CollectorAssignment(id=uuid.uuid4(), integration_id=integration.id, collector_id=collector.id,
                               effective_from=now))
    await db.commit()
    await db.refresh(rule)
    assert rule.registry_version is None
    return collector, integration, rule


async def ingest(db, collector_id, integration_id, key):
    return await ingest_reading(db, collector_id=collector_id, integration_id=integration_id, dedup_key=key,
                                external_identifier="device", source_identifier="power", occurred_at=datetime.now(UTC), value=2000)


async def test_batch_accepts_unresolved_rule_and_other_rule_evaluates_once(db_session, monkeypatch):
    collector, integration, rule = await seed(db_session)
    explicit = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="power_kw", rule_type="threshold_high",
                         threshold=Decimal("1.5"), unit="kW", registry_version="1", name="canonical")
    db_session.add(explicit)
    await db_session.commit()

    async def assigned(_db, _integration):
        return SimpleNamespace(collector_id=collector.id)

    monkeypatch.setattr("app.application.collector_service.current_assignment", assigned)
    body = TelemetryBatchIn(records=[dict(integration_id=integration.id, source_identifier="power",
                                          external_identifier="device", dedup_key=f"sample-{index}",
                                          occurred_at=datetime.now(UTC), value=2000) for index in range(3)])
    response = await ingest_collector_telemetry(db_session, collector=collector, body=body)
    assert [(item.status, item.error) for item in response.results] == [("accepted", None)] * 3
    assert (await db_session.scalar(select(func.count()).select_from(TelemetryReading))) == 3
    assert (await db_session.scalar(select(func.count()).select_from(Alarm).where(Alarm.rule_id == rule.id))) == 0
    assert (await db_session.scalar(select(func.count()).select_from(Alarm).where(Alarm.rule_id == explicit.id))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(OutboxEvent).where(
        OutboxEvent.event_type == "AlarmRuleEvaluationBlocked"))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "alarm.rule.evaluation_blocked"))) == 1
    retry = await ingest_collector_telemetry(db_session, collector=collector, body=body)
    assert [item.status for item in retry.results] == ["duplicate"] * 3


async def test_concurrent_first_readings_emit_one_block_episode(db_session, db_engine):
    collector, integration, rule = await seed(db_session)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def worker(index):
        async with factory() as session:
            await ingest(session, collector.id, integration.id, f"parallel-{index}")
            await session.commit()

    await asyncio.gather(worker(1), worker(2))
    assert (await db_session.scalar(select(func.count()).select_from(TelemetryReading))) == 2
    assert (await db_session.scalar(select(func.count()).select_from(OutboxEvent).where(
        OutboxEvent.event_type == "AlarmRuleEvaluationBlocked", OutboxEvent.aggregate_id == rule.id))) == 1


async def test_concurrent_different_resolutions_have_one_winner_and_one_audit(db_session, db_engine, make_user):
    _, _, rule = await seed(db_session)
    actor = await make_user("resolver@example.com", "correct horse battery staple", "Administrator")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    original = rule.updated_at

    async def worker(unit):
        async with factory() as session:
            locked = (await session.execute(select(AlarmRule).where(AlarmRule.id == rule.id).with_for_update())).scalar_one()
            try:
                await resolve_rule_unit(session, rule=locked, unit=unit, reason="Approved evidence",
                                        expected_updated_at=original, actor_user_id=actor.id)
                await session.commit()
                return "resolved"
            except ConflictError:
                await session.rollback()
                return "conflict"

    assert sorted(await asyncio.gather(worker("W"), worker("kW"))) == ["conflict", "resolved"]
    assert (await db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "alarm.rule.resolve_unit", AuditLog.entity_id == rule.id))) == 1


async def test_resolution_uses_authored_unit_next_reading_without_replaying_gap(db_session, make_user):
    collector, integration, rule = await seed(db_session)
    actor = await make_user("unit-owner@example.com", "correct horse battery staple", "Administrator")
    await ingest(db_session, collector.id, integration.id, "blocked")
    await db_session.commit()
    locked = (await db_session.execute(select(AlarmRule).where(AlarmRule.id == rule.id).with_for_update()
                                      .execution_options(populate_existing=True))).scalar_one()
    await resolve_rule_unit(db_session, rule=locked, unit="W", reason="Approved source documentation",
                            expected_updated_at=locked.updated_at, actor_user_id=actor.id)
    await db_session.commit()
    assert (await db_session.scalar(select(func.count()).select_from(Alarm))) == 0
    await ingest(db_session, collector.id, integration.id, "after-resolution")
    await db_session.commit()
    alarm = (await db_session.execute(select(Alarm))).scalar_one()
    assert alarm.last_value == Decimal("2000")
    assert alarm.details["unit"] == "W"
    assert locked.threshold == Decimal("1500")


async def test_block_audit_outbox_failure_rolls_back_marker_and_reading(db_session, monkeypatch):
    collector, integration, rule = await seed(db_session)

    async def failed_outbox(*args, **kwargs):
        raise RuntimeError("Synthetic outbox write failure")

    monkeypatch.setattr("app.application.alarm_service.write_outbox_event", failed_outbox)
    with pytest.raises(RuntimeError, match="Synthetic outbox"):
        async with db_session.begin_nested():
            await ingest(db_session, collector.id, integration.id, "failed-event")
    await db_session.refresh(rule)
    assert rule.blocked_notified_at is None
    assert (await db_session.scalar(select(func.count()).select_from(TelemetryReading))) == 0
    assert (await db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "alarm.rule.evaluation_blocked"))) == 0


async def test_opposite_order_multigroup_batches_finish_without_deadlock_and_keep_ack_order(db_session, db_engine):
    collector, first, first_rule = await seed(db_session)
    second = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.2",
                          config={}, poll_interval_seconds=60)
    foreign = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.3",
                           config={}, poll_interval_seconds=60)
    db_session.add_all([second, foreign])
    await db_session.flush()
    second_rule = AlarmRule(id=uuid.uuid4(), integration_id=second.id, metric="temperature_c", rule_type="threshold_high",
                            threshold=30, unit=None, registry_version=null(), name="second unresolved")
    db_session.add_all([
        second_rule,
        IntegrationMetricMapping(id=uuid.uuid4(), integration_id=second.id, source_identifier="temperature",
                                 canonical_metric="temperature_c", unit="degC", scale=1, registry_version="1"),
        CollectorAssignment(id=uuid.uuid4(), integration_id=second.id, collector_id=collector.id,
                            effective_from=datetime.now(UTC)),
    ])
    await db_session.commit()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first_processed = asyncio.Event()
    second_attempted = asyncio.Event()
    # The two sessions really process opposite integration+metric orders. The
    # second starts its rule prelock only after the first has stored group A.
    # Old per-record locking then forms A->B / B->A; request-wide prelock waits
    # before group B inserts. Event-controlled instrumentation avoids sleeps.
    import app.api.v1.telemetry as telemetry_api

    original = telemetry_api.ingest_reading
    first_session = None
    second_session = None

    async def observed_ingest(session, **kwargs):
        result = await original(session, **kwargs)
        if session is first_session and kwargs["integration_id"] == first.id:
            first_processed.set()
            await second_attempted.wait()
        if session is second_session and kwargs["integration_id"] == second.id:
            # Under the original per-record implementation, B is now locked:
            # release A's worker only after that, forcing the A/B inversion.
            second_attempted.set()
        return result

    # Record a pending second-session rule-lock query before it blocks.
    from sqlalchemy import event

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if ("FOR UPDATE" in statement and "alarm_rule" in statement and "collector_assignment" in statement
                and first_processed.is_set()):
            # Fixed code blocks before processing B, while holding no rule locks;
            # release A's worker as soon as that request-wide lock is submitted.
            second_attempted.set()

    event.listen(db_engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    telemetry_api.ingest_reading = observed_ingest
    try:
        async def worker(label, order):
            nonlocal first_session, second_session
            async with factory() as session:
                if label == "AB":
                    first_session = session
                else:
                    second_session = session
                    await first_processed.wait()
                records = [dict(integration_id=integration, source_identifier=source, external_identifier="device",
                                dedup_key=f"{label}-{index}", occurred_at=datetime.now(UTC), value=value)
                           for index, (integration, source, value) in enumerate(order)]
                records.append(dict(integration_id=foreign.id, source_identifier="unknown", external_identifier="foreign",
                                    dedup_key=f"{label}-foreign", occurred_at=datetime.now(UTC), value=1))
                response = await ingest_collector_telemetry(session, collector=collector, body=TelemetryBatchIn(records=records))
                assert [ack.dedup_key for ack in response.results] == [record["dedup_key"] for record in records]
                assert [ack.status for ack in response.results] == ["accepted", "accepted", "rejected"]
                assert response.results[-1].error == "NOT_ASSIGNED"
                return response

        await asyncio.wait_for(asyncio.gather(
            worker("AB", [(first.id, "power", 2000), (second.id, "temperature", 35)]),
            worker("BA", [(second.id, "temperature", 35), (first.id, "power", 2000)]),
        ), timeout=15)
    finally:
        telemetry_api.ingest_reading = original
        event.remove(db_engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    assert (await db_session.scalar(select(func.count()).select_from(TelemetryReading))) == 4
    assert (await db_session.scalar(select(func.count()).select_from(OutboxEvent).where(
        OutboxEvent.event_type == "AlarmRuleEvaluationBlocked",
        OutboxEvent.aggregate_id.in_([first_rule.id, second_rule.id]),
    ))) == 2
