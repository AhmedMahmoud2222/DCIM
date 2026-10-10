"""Real ingestion/lifecycle proof for legacy and canonical alarm unit contracts."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import null, select, text

from app.api.v1.alarms import _out
from app.application.telemetry_service import ingest_reading
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


@pytest.mark.parametrize("metric,source_unit,mapping_version,rule_unit,rule_version,threshold,hot,cold,expected,presented,presentation_unit", [
    ("power_kw", "W", None, "kW", "1", "1.5", 2000, 1250, "1.25", 1.25, "kW"),
    ("temperature_c", "degF", None, "degC", "1", "30", 95, 77, "25", 25, "degC"),
    ("availability", "%", None, "1", "1", "0.99", 100, 98, "0.98", 98, "%"),
    ("power_kw", "W", "1", None, None, "1500", 2000, 1250, "1250", 1250, "W"),
    ("temperature_c", "degF", "1", None, None, "80", 95, 77, "77", 77, "degF"),
    ("availability", "%", "1", None, None, "99", 100, 98, "98", 98, "%"),
    ("power_kw", "W", "1", "W", None, "1500", 2000, 1250, "1250", 1250, "W"),
    ("power_kw", "kW", "1", "W", None, "1500", 2, 1.25, "1250", 1250, "W"),
    ("temperature_c", "degC", "1", "degF", None, "80", 35, 25, "77", 77, "degF"),
    ("availability", "1", "1", "%", None, "99", 1, 0.98, "98", 98, "%"),
    ("temperature_c", "degF", "1", "degF", None, "80", 95, 77, "77", 77, "degF"),
    ("availability", "%", "1", "%", None, "99", 100, 98, "98", 98, "%"),
])
async def test_ingestion_opens_and_clears_in_authored_rule_units(
    db_session, metric, source_unit, mapping_version, rule_unit, rule_version, threshold, hot, cold,
    expected, presented, presentation_unit,
):
    now = datetime.now(UTC)
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db_session.add_all([collector, integration])
    await db_session.flush()
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="sensor",
                                       canonical_metric=metric, unit=source_unit, scale=10,
                                       registry_version=mapping_version if mapping_version is not None else null())
    rule = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric=metric, rule_type="threshold_high",
                     threshold=Decimal(threshold), unit=rule_unit or source_unit, name="authored threshold")
    db_session.add_all([mapping, rule])
    await db_session.flush()
    # Match migrated rows: legacy versions stay NULL and unambiguous units are frozen,
    # as proven by test_units_metric_registry_migration; avoid ORM insertion defaults.
    # The mapping carries its version from construction: its first revision is immutable and is stamped from it.
    for table, row, version in [("alarm_rule", rule, rule_version)]:
        await db_session.execute(text(f"UPDATE {table} SET registry_version = :version WHERE id = :id"),
                                 {"id": row.id, "version": version})
        await db_session.refresh(row)
    for seconds, scaled_value in enumerate([hot, cold]):
        result = await ingest_reading(db_session, collector_id=collector.id, integration_id=integration.id,
                                      dedup_key=uuid.uuid4().hex, external_identifier="sensor-a", source_identifier="sensor",
                                      occurred_at=datetime.now(UTC) + timedelta(seconds=seconds), value=scaled_value / 10)
        assert not result.duplicate
        alarm = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalar_one()
        assert alarm.status == ("ACTIVE" if seconds == 0 else "CLEARED")
    assert Decimal(str(alarm.last_value)) == Decimal(expected)
    assert rule.threshold == Decimal(threshold)
    output = _out(alarm)
    assert output.presentation_value == presented
    assert output.presentation_unit == presentation_unit
    reading = await db_session.get(TelemetryReading, result.reading_id)
    assert reading.raw_value == Decimal(str(cold / 10))
    assert reading.raw_unit == source_unit
    assert reading.source_scale == 10
    assert reading.registry_version == mapping_version


async def test_batch_incompatible_legacy_units_reject_only_bad_record_and_roll_back_its_alarm_work(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.api.v1.telemetry import TelemetryBatchIn, ingest_collector_telemetry

    now = datetime.now(UTC)
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db_session.add_all([collector, integration])
    await db_session.flush()
    good = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="good",
                                    canonical_metric="temperature_c", unit="degF", scale=1, registry_version=null())
    bad = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="bad",
                                   canonical_metric="temperature_c", unit="W", scale=1, registry_version=null())
    canonical = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="temperature_c", rule_type="threshold_high",
                          threshold=Decimal("30"), unit="degC", name="canonical")
    db_session.add_all([good, bad, canonical])
    await db_session.flush()
    await db_session.refresh(good)
    await db_session.refresh(bad)
    assert good.registry_version is None and bad.registry_version is None

    async def assigned(_db, _integration_id):
        return SimpleNamespace(collector_id=collector.id)

    monkeypatch.setattr("app.application.collector_service.current_assignment", assigned)
    body = TelemetryBatchIn(records=[
        {"integration_id": integration.id, "source_identifier": source, "external_identifier": source,
         "dedup_key": source, "occurred_at": datetime.now(UTC), "value": 95}
        for source in ["bad", "good"]
    ])
    result = await ingest_collector_telemetry(db_session, collector=collector, body=body)
    assert [(ack.dedup_key, ack.status, ack.error) for ack in result.results] == [
        ("bad", "rejected", "INCOMPATIBLE_TELEMETRY_UNITS"), ("good", "accepted", None),
    ]
    readings = (await db_session.execute(select(TelemetryReading))).scalars().all()
    assert len(readings) == 1
    assert readings[0].dedup_key == "good"
    alarms = (await db_session.execute(select(Alarm))).scalars().all()
    assert len(alarms) == 1
    assert alarms[0].telemetry_reading_id == readings[0].id
    # Rejected inserts were rolled back, not silently persisted and ACKed duplicate on retry.
    retry = await ingest_collector_telemetry(db_session, collector=collector, body=body)
    assert [ack.status for ack in retry.results] == ["rejected", "duplicate"]
