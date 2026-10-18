import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest

from app.application.power_history import _bucket_means
from app.domain.telemetry import registry


@pytest.fixture
def changed_power_snapshot(monkeypatch):
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["kW"] = replace(units["kW"], scale=Decimal("2000"))
    v2 = registry.make_snapshot("2", units, v1.aliases, v1.metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))


def test_stored_canonical_values_normalize_against_their_own_snapshot(changed_power_snapshot):
    assert registry.convert_stored_canonical("power_kw", Decimal("1"), "kW", "2") == Decimal("2.00000000")
    assert registry.convert_stored_canonical("power_kw", Decimal("1"), "kW", None) == Decimal("1.00000000")
    with pytest.raises(registry.UnitDimensionMismatch):
        registry.convert_stored_canonical("power_kw", Decimal("1"), "W", "2")


async def test_hourly_means_normalize_each_version_before_combining(changed_power_snapshot):
    asset = uuid.uuid4()
    rows = [(asset, "1", "kW", Decimal("1"), 1), (asset, "2", "kW", Decimal("1"), 1)]
    class Database:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: rows)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    result = await _bucket_means(Database(), [asset], start, start + timedelta(hours=1))
    assert result[asset] == (1.5, 2)


def test_environment_canonical_projection_accepts_new_canonical_unit(monkeypatch):
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["newC"] = replace(units["degC"], symbol="newC")
    metrics = dict(v1.metrics)
    metrics["temperature_c"] = replace(metrics["temperature_c"], canonical_unit="newC")
    v2 = registry.make_snapshot("2", units, v1.aliases, metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))
    assert registry.convert_stored_canonical("temperature_c", Decimal("20"), "newC", "2") == Decimal("20.00000000")
