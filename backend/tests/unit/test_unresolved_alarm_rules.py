"""G8 behavior without a database; PostgreSQL race tests live in integration/."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application import alarm_service
from app.core.errors import ConflictError
from app.domain.alarm.models import AlarmRule
from app.domain.audit.models import AuditLog
from app.domain.outbox.models import OutboxEvent
from app.domain.telemetry.models import TelemetryReading


def rule(**overrides):
    data = dict(id=uuid.uuid4(), integration_id=uuid.uuid4(), metric="power_kw", unit=None,
                registry_version=None, threshold=Decimal("1500"), rule_type="threshold_high",
                enabled=True, name="legacy", updated_at=datetime.now(UTC))
    data.update(overrides)
    return AlarmRule(**data)


async def test_unresolved_rule_is_skipped_once_without_preventing_other_rules():
    unresolved = rule()
    resolved = rule(unit="kW", registry_version="1", threshold=Decimal("1.5"))
    result = MagicMock()
    result.scalars.return_value.all.return_value = [unresolved, resolved]
    empty = MagicMock()
    empty.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.add = MagicMock()
    db.execute.side_effect = [result, empty, empty, result, empty, empty]
    now = datetime.now(UTC)
    reading = TelemetryReading(id=uuid.uuid4(), integration_id=unresolved.integration_id,
                               external_identifier="device", metric="power_kw", unit="kW", value=2,
                               registry_version="1", occurred_at=now, received_at=now)
    await alarm_service.evaluate_reading(db, reading)
    await alarm_service.evaluate_reading(db, reading)
    events = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], OutboxEvent)]
    assert sum(event.event_type == "AlarmRuleEvaluationBlocked" for event in events) == 1
    assert sum(event.event_type == "AlarmOpened" for event in events) == 2
    audits = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog)]
    assert sum(audit.action == "alarm.rule.evaluation_blocked" for audit in audits) == 1
    assert unresolved.blocked_notified_at == now
    assert reading.value == 2
    assert "FOR UPDATE" in str(db.execute.call_args_list[0].args[0])


async def test_resolution_preserves_threshold_and_records_actor_before_after():
    resolver = getattr(alarm_service, "resolve_rule_unit", None)
    assert resolver is not None, "G8 operator resolution service is missing"
    legacy = rule()
    actor = uuid.uuid4()
    previous = legacy.updated_at
    db = AsyncMock()
    db.add = MagicMock()
    await resolver(db, rule=legacy, unit="W", reason="Approved source documentation", expected_updated_at=previous,
                   actor_user_id=actor)
    assert legacy.unit == "W"
    assert legacy.registry_version == "1"
    assert legacy.threshold == Decimal("1500")
    assert legacy.unit_resolved_by == actor
    assert legacy.updated_at > previous
    audits = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog)]
    assert len(audits) == 1
    assert audits[0].before["unit"] is None
    assert audits[0].after["unit"] == "W"
    db.add.reset_mock()
    await resolver(db, rule=legacy, unit="W", reason="Retry", expected_updated_at=previous, actor_user_id=actor)
    db.add.assert_not_called()
    with pytest.raises(ConflictError):
        await resolver(db, rule=legacy, unit="kW", reason="Change", expected_updated_at=legacy.updated_at, actor_user_id=actor)


@pytest.mark.parametrize("unit,reason,stale,status", [("kg", "Valid reason", False, 422),
                                                     ("W", "   ", False, 422),
                                                     ("W", "Valid reason", True, 409)])
async def test_resolution_rejects_invalid_or_stale_without_mutating(unit, reason, stale, status):
    resolver = getattr(alarm_service, "resolve_rule_unit", None)
    assert resolver is not None, "G8 operator resolution service is missing"
    legacy = rule()
    db = AsyncMock()
    db.add = MagicMock()
    with pytest.raises(Exception) as error:
        await resolver(db, rule=legacy, unit=unit, reason=reason,
                       expected_updated_at=legacy.updated_at - timedelta(seconds=1) if stale else legacy.updated_at,
                       actor_user_id=uuid.uuid4())
    assert error.value.status_code == status
    assert legacy.unit is None
    assert legacy.threshold == Decimal("1500")
    db.add.assert_not_called()


async def test_disable_records_one_transition_and_rejects_stale_update():
    disabler = getattr(alarm_service, "disable_rule", None)
    assert disabler is not None, "G8 operator disable service is missing"
    legacy = rule()
    db = AsyncMock()
    db.add = MagicMock()
    old = legacy.updated_at
    with pytest.raises(ConflictError):
        await disabler(db, rule=legacy, reason="Awaiting evidence", expected_updated_at=old - timedelta(seconds=1),
                       actor_user_id=uuid.uuid4())
    assert legacy.enabled
    await disabler(db, rule=legacy, reason="Awaiting evidence", expected_updated_at=old, actor_user_id=uuid.uuid4())
    assert not legacy.enabled
    events = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], OutboxEvent)]
    assert [event.event_type for event in events] == ["AlarmRuleDisabled"]
    db.add.reset_mock()
    await disabler(db, rule=legacy, reason="Retry", expected_updated_at=old, actor_user_id=uuid.uuid4())
    db.add.assert_not_called()
