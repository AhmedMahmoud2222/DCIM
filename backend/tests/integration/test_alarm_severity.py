"""Alarm severity is explicit data, snapshotted when an alarm opens, and independent of workflow state."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.v1.alarms import AlarmRuleIn, _out, _rule_out
from app.application.alarm_service import acknowledge_alarm
from app.application.telemetry_service import ingest_reading
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping


async def _world(db_session, severity: str | None):
    now = datetime.now(UTC)
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="t", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db_session.add_all([collector, integration])
    await db_session.flush()
    db_session.add(IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="s",
                                            canonical_metric="temperature_c", unit="degC", scale=1))
    extra = {} if severity is None else {"severity": severity}
    rule = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="temperature_c", rule_type="threshold_high",
                     threshold=Decimal("30"), unit="degC", name="hot", **extra)
    db_session.add(rule)
    await db_session.flush()
    return collector, integration, rule


async def _open(db_session, collector, integration, value=35):
    await ingest_reading(db_session, collector_id=collector.id, integration_id=integration.id, dedup_key=uuid.uuid4().hex,
                         external_identifier="s", source_identifier="s", occurred_at=datetime.now(UTC), value=value)


@pytest.mark.parametrize("severity", ["critical", "warning", "info"])
async def test_alarm_snapshots_rule_severity_and_serialises_it(db_session, severity):
    collector, integration, rule = await _world(db_session, severity)
    await _open(db_session, collector, integration)
    alarm = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalar_one()
    assert alarm.severity == severity
    assert _out(alarm).severity == severity
    assert _rule_out(rule).severity == severity


async def test_rule_without_severity_defaults_to_warning(db_session):
    collector, integration, rule = await _world(db_session, None)
    await db_session.refresh(rule)
    assert rule.severity == "warning"
    await _open(db_session, collector, integration)
    alarm = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalar_one()
    assert alarm.severity == "warning"


async def test_severity_is_independent_of_workflow_state_and_later_rule_edits(db_session):
    collector, integration, rule = await _world(db_session, "critical")
    await _open(db_session, collector, integration)
    alarm = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalar_one()
    await acknowledge_alarm(db_session, alarm=alarm, actor_user_id=None)
    assert alarm.status == "ACKNOWLEDGED"
    assert alarm.severity == "critical"
    rule.severity = "info"
    await db_session.flush()
    assert alarm.severity == "critical"  # the open alarm keeps the severity it opened with
    await ingest_reading(db_session, collector_id=collector.id, integration_id=integration.id, dedup_key=uuid.uuid4().hex,
                         external_identifier="s", source_identifier="s", occurred_at=datetime.now(UTC) + timedelta(seconds=5), value=20)
    await db_session.refresh(alarm)
    assert alarm.status == "CLEARED"
    assert alarm.severity == "critical"


async def test_database_rejects_unknown_severity(db_session):
    _, integration, _ = await _world(db_session, "info")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="temperature_c",
                                     rule_type="threshold_high", threshold=Decimal("1"), unit="degC", name="x", severity="fatal"))
            await db_session.flush()


def test_rule_input_rejects_unknown_severity_and_defaults_to_warning():
    base = {"integration_id": str(uuid.uuid4()), "metric": "power_kw", "rule_type": "threshold_high", "threshold": 1, "name": "n"}
    assert AlarmRuleIn(**base).severity == "warning"
    with pytest.raises(ValueError):
        AlarmRuleIn(**base, severity="fatal")


async def test_rule_severity_edit_is_audited_and_does_not_rewrite_open_alarms(db_session):
    from app.application.alarm_service import update_rule

    collector, integration, rule = await _world(db_session, "warning")
    await _open(db_session, collector, integration)
    alarm = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalar_one()
    await db_session.commit()
    await db_session.refresh(rule)
    await update_rule(
        db_session, rule=rule, name=None, severity="critical", threshold=None, threshold_set=False, reason="Escalated",
        expected_updated_at=rule.updated_at, actor_user_id=None,
    )
    assert rule.severity == "critical"
    assert alarm.severity == "warning"
