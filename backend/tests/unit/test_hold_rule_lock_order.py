"""Hold releases use the collector rule-first lock hierarchy."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import telemetry
from app.application.alarm_service import BATCH_ALARM_RULE_IDS


def result(*, scalar=None, values=()):
    query = MagicMock()
    query.scalar_one_or_none.return_value = scalar
    query.scalars.return_value.all.return_value = list(values)
    return query


@pytest.mark.parametrize("fail", [False, True])
async def test_hold_release_locks_rules_before_hold_and_cleans_snapshot(monkeypatch, fail):
    now = datetime.now(UTC)
    integration, rule, hold_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hold = SimpleNamespace(
        id=hold_id, integration_id=integration, status="held", collector_id=uuid.uuid4(),
        dedup_key="held-key", external_identifier="sensor", source_identifier="power",
        occurred_at=now, value_text="1", attributes={},
    )
    db = AsyncMock()
    db.info = {}
    db.begin_nested = MagicMock(return_value=AsyncMock())
    statements = []

    async def execute(stmt):
        statements.append(stmt)
        if "alarm_rule" in str(stmt):
            return result(values=[rule])
        if "FOR UPDATE" in str(stmt):
            assert db.info[BATCH_ALARM_RULE_IDS] == (rule,)
            return result(scalar=hold)
        return result(scalar=integration)

    async def ingest(_db, **kwargs):
        assert db.info[BATCH_ALARM_RULE_IDS] == (rule,)
        assert kwargs["pinned_evidence"] == "operator_resolved"
        if fail:
            raise RuntimeError("synthetic release failure")
        return SimpleNamespace(reading_id=uuid.uuid4())

    db.execute.side_effect = execute
    monkeypatch.setattr(telemetry, "ingest_reading", ingest)
    monkeypatch.setattr(telemetry, "write_audit_log", AsyncMock())
    monkeypatch.setattr(telemetry, "_hold_out", lambda row: row)
    body = telemetry.ContractHoldResolveIn(mapping_revision_id=uuid.uuid4())
    ctx = SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()))
    if fail:
        with pytest.raises(RuntimeError, match="synthetic release failure"):
            await telemetry.resolve_contract_hold(hold_id, body, db, ctx)
        db.commit.assert_not_called()
    else:
        await telemetry.resolve_contract_hold(hold_id, body, db, ctx)
        db.commit.assert_awaited_once()
    locks = [stmt for stmt in statements if "FOR UPDATE" in str(stmt)]
    assert "alarm_rule" in str(locks[0]) and "ORDER BY alarm_rule.id" in str(locks[0])
    assert "telemetry_contract_hold" in str(locks[1])
    assert locks[1].get_execution_options()["populate_existing"] is True
    assert BATCH_ALARM_RULE_IDS not in db.info
