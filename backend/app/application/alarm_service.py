"""Application service joining telemetry events to alarm lifecycle changes.

Telemetry models do not import this module: ingestion explicitly invokes it after a
reading has been inserted in the same transaction.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.telemetry.models import TelemetryReading


def condition_matches(rule_type: str, threshold: float | None, value: float) -> bool:
    if rule_type == "threshold_high":
        return value > float(threshold)  # threshold presence is DB-enforced
    if rule_type == "threshold_low":
        return value < float(threshold)
    if rule_type == "availability_unavailable":
        return value <= 0
    raise ValueError(f"Unsupported alarm rule type: {rule_type}")


def subject_for(reading: TelemetryReading) -> str:
    return f"{reading.integration_id}:{reading.external_identifier}:{reading.metric}"


async def evaluate_reading(db: AsyncSession, reading: TelemetryReading) -> None:
    """Open, retain, or clear alarms for one authoritative telemetry reading."""
    rules = (await db.execute(select(AlarmRule).where(
        AlarmRule.enabled.is_(True), AlarmRule.integration_id == reading.integration_id,
        AlarmRule.metric == reading.metric,
    ))).scalars().all()
    for rule in rules:
        subject_key = subject_for(reading)
        open_alarm = (await db.execute(select(Alarm).where(
            Alarm.rule_id == rule.id, Alarm.subject_key == subject_key,
            Alarm.status.in_(("ACTIVE", "ACKNOWLEDGED")),
        ).with_for_update())).scalar_one_or_none()
        matches = condition_matches(rule.rule_type, float(rule.threshold) if rule.threshold is not None else None,
                                   float(reading.value))
        if matches and open_alarm is None:
            alarm = Alarm(id=uuid.uuid4(), rule_id=rule.id, integration_id=reading.integration_id,
                telemetry_reading_id=reading.id, managed_asset_id=reading.managed_asset_id,
                subject_key=subject_key, status="ACTIVE", opened_at=reading.received_at,
                last_value=reading.value, details={"metric": reading.metric, "unit": reading.unit})
            db.add(alarm)
            await db.flush()
            await _record_transition(db, alarm, "alarm.open", "AlarmOpened")
        elif matches and open_alarm is not None:
            open_alarm.telemetry_reading_id = reading.id
            open_alarm.last_value = reading.value
        elif not matches and open_alarm is not None:
            open_alarm.status = "CLEARED"
            open_alarm.cleared_at = reading.received_at
            open_alarm.telemetry_reading_id = reading.id
            open_alarm.last_value = reading.value
            await _record_transition(db, open_alarm, "alarm.clear", "AlarmCleared")


async def acknowledge_alarm(db: AsyncSession, *, alarm: Alarm, actor_user_id: uuid.UUID) -> Alarm:
    if alarm.status == "ACTIVE":
        alarm.status = "ACKNOWLEDGED"
        alarm.acknowledged_at = datetime.now(UTC)
        alarm.acknowledged_by = actor_user_id
        await _record_transition(db, alarm, "alarm.acknowledge", "AlarmAcknowledged", actor_user_id)
    return alarm


async def _record_transition(db: AsyncSession, alarm: Alarm, action: str, event_type: str,
                             actor_user_id: uuid.UUID | None = None) -> None:
    await write_audit_log(db, actor_user_id=actor_user_id, action=action, entity_type="alarm", entity_id=alarm.id,
        request_id=None, correlation_id=None, after={"status": alarm.status, "rule_id": str(alarm.rule_id)})
    await write_outbox_event(db, event_type=event_type, aggregate_type="alarm", aggregate_id=alarm.id,
        payload={"alarm_id": str(alarm.id), "rule_id": str(alarm.rule_id), "status": alarm.status})
