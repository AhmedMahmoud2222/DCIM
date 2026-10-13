"""Application service joining telemetry events to alarm lifecycle changes.

Telemetry models do not import this module: ingestion explicitly invokes it after a
reading has been inserted in the same transaction.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import REGISTRY_VERSION, convert_value, validate_metric_unit


class AlarmUnitCompatibilityError(ApiError):
    """An explicit 422 when telemetry cannot preserve an alarm threshold's units."""


def condition_matches(rule_type: str, threshold: Decimal | float | None, value: Decimal | float) -> bool:
    if rule_type == "threshold_high":
        if threshold is None:
            raise ValueError("threshold rule lacks a threshold")
        return value > threshold
    if rule_type == "threshold_low":
        if threshold is None:
            raise ValueError("threshold rule lacks a threshold")
        return value < threshold
    if rule_type == "availability_unavailable":
        return value <= 0
    raise ValueError(f"Unsupported alarm rule type: {rule_type}")


def subject_for(reading: TelemetryReading) -> str:
    return f"{reading.integration_id}:{reading.external_identifier}:{reading.metric}"


def comparison_for(rule: AlarmRule, reading: TelemetryReading) -> tuple[Decimal, str, str | None]:
    """Compare stored readings in the fixed threshold unit, without scaling again.

    Migration pins only unambiguous legacy units. Unresolved units must be decided
    explicitly; current raw source units cannot establish an old threshold's unit.
    """
    if rule.metric != reading.metric:
        raise ValueError("Alarm rule and reading metrics differ.")
    if rule.registry_version is not None and rule.unit is None:
        raise ValueError("Versioned alarm rule lacks an explicit threshold unit.")
    if rule.unit is not None:
        # Validate both dimensions and registry versions before comparing. A legacy
        # explicit unit uses the active physical contract; its threshold is unchanged.
        version = rule.registry_version if rule.registry_version is not None else REGISTRY_VERSION
        validate_metric_unit(rule.metric, rule.unit, registry_version=version)
        validate_metric_unit(rule.metric, reading.unit, registry_version=version)
        if reading.registry_version is not None:
            validate_metric_unit(reading.metric, reading.unit, registry_version=reading.registry_version)
        return convert_value(Decimal(str(reading.value)), reading.unit, rule.unit), rule.unit, rule.registry_version
    raise AlarmUnitCompatibilityError(status_code=422, title="Unresolved legacy alarm unit",
                   detail=f"Alarm rule {rule.id} requires an explicit threshold unit before evaluation.")



async def evaluate_reading(db: AsyncSession, reading: TelemetryReading) -> None:
    """Open, retain, or clear alarms for one authoritative telemetry reading."""
    rules = (
        (
            await db.execute(
                select(AlarmRule).where(
                    AlarmRule.enabled.is_(True),
                    AlarmRule.integration_id == reading.integration_id,
                    AlarmRule.metric == reading.metric,
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        subject_key = subject_for(reading)
        open_alarm = (
            await db.execute(
                select(Alarm)
                .where(
                    Alarm.rule_id == rule.id,
                    Alarm.subject_key == subject_key,
                    Alarm.status.in_(("ACTIVE", "ACKNOWLEDGED")),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        try:
            comparison_value, comparison_unit, comparison_version = comparison_for(rule, reading)
        except ValueError as error:
            raise AlarmUnitCompatibilityError(status_code=422, title="Incompatible alarm telemetry units",
                           detail=f"Alarm rule {rule.id} cannot evaluate this metric/unit/registry combination.") from error
        matches = condition_matches(rule.rule_type, rule.threshold, comparison_value)
        comparison_details = {
            "metric": reading.metric,
            "unit": comparison_unit,
            "registry_version": comparison_version,
            "condition_occurred_at": reading.occurred_at.isoformat(),
            "central_received_at": reading.received_at.isoformat(),
        }
        # Collector delivery is at-least-once and may be out of occurrence order.
        # Never let an older reading overwrite a lifecycle decision made from a
        # newer observation.  The original occurred_at remains the operational
        # event time; received_at is retained in the reading/details for traceability.
        if open_alarm is not None and open_alarm.telemetry_reading_id is not None:
            previous = await db.get(TelemetryReading, open_alarm.telemetry_reading_id)
            if previous is not None and reading.occurred_at < previous.occurred_at:
                continue
        if matches and open_alarm is None:
            latest_cleared = (
                await db.execute(
                    select(Alarm)
                    .where(
                        Alarm.rule_id == rule.id,
                        Alarm.subject_key == subject_key,
                        Alarm.status == "CLEARED",
                        Alarm.cleared_at >= reading.occurred_at,
                    )
                    .order_by(Alarm.cleared_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest_cleared is not None:
                continue
            alarm = Alarm(
                id=uuid.uuid4(),
                rule_id=rule.id,
                integration_id=reading.integration_id,
                telemetry_reading_id=reading.id,
                managed_asset_id=reading.managed_asset_id,
                subject_key=subject_key,
                status="ACTIVE",
                opened_at=reading.occurred_at,
                last_value=comparison_value,
                details=comparison_details,
            )
            db.add(alarm)
            await db.flush()
            await _record_transition(db, alarm, "alarm.open", "AlarmOpened")
        elif matches and open_alarm is not None:
            open_alarm.telemetry_reading_id = reading.id
            open_alarm.last_value = comparison_value
            open_alarm.details = comparison_details
        elif not matches and open_alarm is not None:
            open_alarm.status = "CLEARED"
            open_alarm.cleared_at = reading.occurred_at
            open_alarm.telemetry_reading_id = reading.id
            open_alarm.last_value = comparison_value
            open_alarm.details = comparison_details
            await _record_transition(db, open_alarm, "alarm.clear", "AlarmCleared")


async def acknowledge_alarm(db: AsyncSession, *, alarm: Alarm, actor_user_id: uuid.UUID) -> Alarm:
    if alarm.status == "ACTIVE":
        alarm.status = "ACKNOWLEDGED"
        alarm.acknowledged_at = datetime.now(UTC)
        alarm.acknowledged_by = actor_user_id
        await _record_transition(db, alarm, "alarm.acknowledge", "AlarmAcknowledged", actor_user_id)
    return alarm


async def _record_transition(
    db: AsyncSession, alarm: Alarm, action: str, event_type: str, actor_user_id: uuid.UUID | None = None
) -> None:
    await write_audit_log(
        db,
        actor_user_id=actor_user_id,
        action=action,
        entity_type="alarm",
        entity_id=alarm.id,
        request_id=None,
        correlation_id=None,
        after={"status": alarm.status, "rule_id": str(alarm.rule_id)},
    )
    await write_outbox_event(
        db,
        event_type=event_type,
        aggregate_type="alarm",
        aggregate_id=alarm.id,
        payload={"alarm_id": str(alarm.id), "rule_id": str(alarm.rule_id), "status": alarm.status},
    )
