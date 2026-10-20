from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.alarm_service import comparison_for, condition_matches, evaluate_reading
from app.core.errors import ApiError
from app.domain.alarm.models import AlarmRule
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import UnitDimensionMismatch, UnknownRegistryVersion


@pytest.mark.parametrize("metric,rule_unit,rule_version,threshold,reading_unit,reading_version,value,raw,raw_unit,scale,expected", [
    ("power_kw", "kW", "1", "1.5", "W", None, "1250", None, None, None, "1.25"),
    ("temperature_c", "degC", "1", "30", "degF", None, "77", None, None, None, "25"),
    ("availability", "1", "1", "0.99", "%", None, "98", None, None, None, "0.98"),
    ("power_kw", "W", None, "1500", "kW", "1", "1.25", "125", "W", "10", "1250"),
    ("temperature_c", "degF", None, "80", "degC", "1", "25", "77", "degF", "1", "77"),
    ("availability", "%", None, "99", "1", "1", "0.98", "98", "%", "1", "98"),
    ("power_kw", None, None, "1500", "kW", "1", "1.25", "125", "W", "10", "1250"),
    ("temperature_c", None, None, "80", "degC", "1", "25", "77", "degF", "1", "77"),
    ("availability", None, None, "99", "1", "1", "0.98", "98", "%", "1", "98"),
    ("power_kw", None, None, "1500", "W", None, "1250", None, None, None, "1250"),
    ("power_kw", "kW", "1", "1.5", "kW", "1", "1.25", "125", "W", "10", "1.25"),
])
def test_comparison_preserves_authored_threshold_and_converts_exactly_once(
    metric, rule_unit, rule_version, threshold, reading_unit, reading_version, value, raw, raw_unit, scale, expected,
):
    # Unitless pre-migration fixtures acquire their unambiguous source unit during
    # upgrade. Keep every original comparison assertion, now against frozen units.
    rule_unit = rule_unit or raw_unit or reading_unit
    rule = AlarmRule(metric=metric, rule_type="threshold_high", threshold=Decimal(threshold), unit=rule_unit,
                     registry_version=rule_version)
    reading = TelemetryReading(metric=metric, value=Decimal(value), unit=reading_unit, registry_version=reading_version,
                               raw_value=Decimal(raw) if raw else None, raw_unit=raw_unit,
                               source_scale=Decimal(scale) if scale else None)
    compared, unit, version = comparison_for(rule, reading)
    assert compared == Decimal(expected)
    assert unit == (rule_unit or raw_unit or reading_unit)
    assert version == rule_version
    assert rule.threshold == Decimal(threshold)
    assert reading.value == Decimal(value)
    assert not condition_matches(rule.rule_type, rule.threshold, compared)
    assert condition_matches("threshold_low", rule.threshold, compared)


@pytest.mark.parametrize("unit,version,error", [("kg", "1", UnitDimensionMismatch), ("W", "future", UnknownRegistryVersion)])
def test_incompatible_or_unknown_registry_reading_cannot_reinterpret_threshold(unit, version, error):
    with pytest.raises(error):
        comparison_for(AlarmRule(metric="power_kw", unit="kW", registry_version="1"),
                       TelemetryReading(metric="power_kw", unit=unit, value=1, registry_version=version))


@pytest.mark.parametrize("version,raw,raw_unit,scale", [(None, None, None, None), ("1", 2000, "W", 1)])
def test_unresolved_legacy_rule_rejects_even_honest_raw_provenance(version, raw, raw_unit, scale):
    with pytest.raises(ApiError, match="explicit threshold unit"):
        comparison_for(AlarmRule(metric="power_kw", unit=None, registry_version=None),
                       TelemetryReading(metric="power_kw", unit="kW", value=2, registry_version=version,
                                        raw_value=raw, raw_unit=raw_unit, source_scale=scale))


def test_versioned_rule_cannot_infer_missing_unit():
    with pytest.raises(ValueError, match="explicit threshold unit"):
        comparison_for(AlarmRule(metric="power_kw", registry_version="1"),
                       TelemetryReading(metric="power_kw", unit="W", value=1000, registry_version=None))


async def test_lifecycle_updates_value_and_its_unit_metadata_together():
    rule = AlarmRule(metric="power_kw", unit="W", registry_version=None, threshold=2000, rule_type="threshold_high")
    alarm = MagicMock(telemetry_reading_id=None, details={"unit": "kW", "registry_version": "1"})
    rules = MagicMock()
    rules.scalars.return_value.all.return_value = [rule]
    existing = MagicMock()
    existing.scalar_one_or_none.return_value = alarm
    db = AsyncMock()
    db.execute.side_effect = [rules, existing]
    now = datetime.now(UTC)
    reading = TelemetryReading(metric="power_kw", unit="kW", value=3, registry_version="1",
                               raw_value=300, raw_unit="W", source_scale=10, occurred_at=now, received_at=now)
    await evaluate_reading(db, reading)
    assert alarm.last_value == 3000
    assert alarm.details["unit"] == "W"
    assert alarm.details["registry_version"] is None


@pytest.mark.parametrize("raw,raw_unit,scale", [(2000, "W", 1), (2, "kW", 1), (200, "W", 10)])
def test_source_unit_changes_cannot_reinterpret_a_migrated_legacy_threshold(raw, raw_unit, scale):
    rule = AlarmRule(metric="power_kw", unit="W", registry_version=None, threshold=Decimal("1500"))
    reading = TelemetryReading(metric="power_kw", unit="kW", value=Decimal("2"), registry_version="1",
                               raw_value=raw, raw_unit=raw_unit, source_scale=scale)
    value, unit, version = comparison_for(rule, reading)
    assert value == Decimal("2000")
    assert unit == "W"
    assert version is None
    assert condition_matches("threshold_high", rule.threshold, value)
    assert rule.threshold == Decimal("1500")


async def test_incompatible_units_are_rejected_explicitly_before_lifecycle_changes():
    rule = AlarmRule(metric="power_kw", unit="kW", registry_version="1", threshold=2, rule_type="threshold_high")
    rules = MagicMock()
    rules.scalars.return_value.all.return_value = [rule]
    existing = MagicMock()
    existing.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.side_effect = [rules, existing]
    reading = TelemetryReading(metric="power_kw", unit="kg", value=3, registry_version=None)
    with pytest.raises(ApiError) as result:
        await evaluate_reading(db, reading)
    assert result.value.status_code == 422
    assert isinstance(result.value.__cause__, UnitDimensionMismatch)
    db.flush.assert_not_awaited()
