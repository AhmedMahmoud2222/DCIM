"""Request-wide alarm lock order and collector ownership regression."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects.postgresql import dialect

from app.api.v1.telemetry import TelemetryBatchIn, ingest_collector_telemetry
from app.application import alarm_service
from app.domain.integration.models import Collector


def result(values):
    query = MagicMock()
    query.scalars.return_value.all.return_value = values
    query.scalars.return_value.__iter__.return_value = iter(values)
    return query


async def test_batch_prelocks_before_any_savepoint_and_preserves_response_order(monkeypatch):
    collector = Collector(id=uuid.uuid4())
    first, second, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    low, high = sorted([uuid.uuid4(), uuid.uuid4()])
    db = AsyncMock()
    db.info = {}
    db.begin_nested = MagicMock(return_value=AsyncMock())
    calls = []

    async def execute(stmt):
        calls.append(stmt)
        sql = str(stmt)
        if "collector_assignment.integration_id" in sql and "alarm_rule" not in sql:
            return result([first, second])
        if "FOR UPDATE" in sql:
            return result([low, high])
        return result([])

    async def assigned(_db, integration_id):
        return SimpleNamespace(collector_id=collector.id if integration_id != foreign else uuid.uuid4())

    async def ingest(_db, **kwargs):
        assert any("FOR UPDATE" in str(stmt) for stmt in calls), "No request-wide lock precedes record savepoints"
        assert db.info[alarm_service.BATCH_ALARM_RULE_IDS] == (low, high)
        return SimpleNamespace(duplicate=False, reading_id=uuid.uuid4())

    monkeypatch.setattr("app.application.collector_service.current_assignment", assigned)
    monkeypatch.setattr("app.api.v1.telemetry.ingest_reading", ingest)
    db.execute.side_effect = execute
    body = TelemetryBatchIn(records=[dict(integration_id=integration, dedup_key=str(index), external_identifier="device",
                                          source_identifier="power", occurred_at=datetime.now(UTC), value=1)
                                     for index, integration in enumerate([second, foreign, first])])
    outcome = await ingest_collector_telemetry(db, collector=collector, body=body)
    assert [(ack.dedup_key, ack.status) for ack in outcome.results] == [("0", "accepted"), ("1", "rejected"), ("2", "accepted")]
    assert outcome.results[1].error == "NOT_ASSIGNED"
    locking = next(stmt for stmt in calls if "FOR UPDATE" in str(stmt))
    sql = str(locking.compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
    assert "ORDER BY alarm_rule.id" in sql
    assert str(first) in sql and str(second) in sql
    assert str(foreign) not in sql
    assert str(collector.id) in sql
    assert "collector_assignment.effective_to IS NULL" in sql
    assert alarm_service.BATCH_ALARM_RULE_IDS not in db.info


async def test_batch_snapshot_excludes_newly_inserted_rule_from_later_record_locks():
    db = AsyncMock()
    key = getattr(alarm_service, "BATCH_ALARM_RULE_IDS", None)
    assert key is not None, "Request-wide alarm rule snapshot is missing"
    old = uuid.uuid4()
    db.info = {key: (old,)}
    db.execute.return_value = result([])
    # registry_version is a nullable column on TelemetryReading; the mock must
    # carry it so the merged main-side version-mismatch filter can run.
    reading = SimpleNamespace(integration_id=uuid.uuid4(), metric="power_kw", registry_version=None)
    await alarm_service.evaluate_reading(db, reading)
    sql = str(db.execute.call_args.args[0].compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
    assert str(old) in sql
    assert "alarm_rule.id IN" in sql


async def test_request_snapshot_cleans_up_after_failed_record(monkeypatch):
    manager = getattr(alarm_service, "lock_collector_batch_rules", None)
    assert manager is not None, "Request-wide alarm lock context is missing"
    db = AsyncMock()
    db.info = {}
    db.execute.side_effect = [result([uuid.uuid4()]), result([uuid.uuid4()])]
    with pytest.raises(RuntimeError, match="synthetic"):
        async with manager(db, collector_id=uuid.uuid4(), integration_ids=[uuid.uuid4()]):
            raise RuntimeError("synthetic record error")
    assert not db.info
