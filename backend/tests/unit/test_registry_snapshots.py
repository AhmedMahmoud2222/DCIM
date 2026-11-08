"""G10: historical conversions are isolated from mutation and global context."""
import subprocess
import sys
from dataclasses import replace
from decimal import Context, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType

import pytest

from app.domain.telemetry import registry
from app.domain.telemetry.contract import conversion_contract_hash


def test_v1_snapshot_is_deeply_immutable():
    snapshot = registry.registry_snapshot("1")
    for mapping in (snapshot.units, snapshot.aliases, snapshot.metrics, registry.REGISTRIES):
        with pytest.raises(TypeError):
            mapping["new"] = None
    assert registry.UNITS is snapshot.units


def test_v1_encoding_and_hash_match_shipped_golden_file():
    snapshot = registry.registry_snapshot("1")
    assert registry.snapshot_text(snapshot) == Path(registry.__file__).with_name("registry_v1.snapshot.txt").read_text(encoding="utf-8")
    assert snapshot.content_hash == registry.V1_CONTENT_HASH
    assert len(snapshot.content_hash) == 64


@pytest.mark.parametrize("source,normalized,value,expected", [
    ("°C", "degC", "20", "20.00000000"),
    ("°F", "degF", "68", "20.00000000"),
])
def test_shipped_degree_aliases_preserve_pre_snapshot_v1(source, normalized, value, expected):
    # Capture main's actual Unicode aliases, rather than freezing a mojibake copy.
    assert registry.normalize_unit(source, registry_version="1") == normalized
    assert registry.convert_to_canonical("temperature_c", Decimal(value), source,
                                         registry_version="1").value == Decimal(expected)


def test_conversions_ignore_hostile_global_decimal_context():
    with localcontext(Context(prec=6)):
        assert registry.convert_value(Decimal("1000000"), "CFM", "m3/s") == Decimal("471.94744320")
        assert registry.convert_to_canonical("power_kw", Decimal("1234567.12345678"), "W", source_scale=Decimal("3")).value == Decimal("3703.70137037")
        assert conversion_contract_hash("temperature_c", "degF", Decimal("1"), "1") == "86044c6d3f4ad702dfd5f5166342b1f57aa668050a9a1aaf547bfcf802ba320e"


def test_synthetic_v2_does_not_change_v1_and_propagates_through_public_helpers(monkeypatch):
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["CFM"] = replace(units["CFM"], scale=Decimal("0.00047"))
    v2 = registry.make_snapshot("2", units, v1.aliases, v1.metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))
    assert registry.convert_value(Decimal("1000000"), "CFM", "m3/s", registry_version="2") == Decimal("470.00000000")
    assert registry.convert_value(Decimal("1000000"), "CFM", "m3/s", registry_version="1") == Decimal("471.94744320")
    assert registry.convert_to_canonical("airflow_m3_s", Decimal("1000000"), "CFM", registry_version="2").value == Decimal("470.00000000")
    assert registry.convert_to_presentation("airflow_m3_s", Decimal("1000000"), "CFM", registry_version="2").value == Decimal("1692000.00000000")
    assert conversion_contract_hash("airflow_m3_s", "CFM", Decimal("1"), "2") != conversion_contract_hash("airflow_m3_s", "CFM", Decimal("1"), "1")


def test_unknown_registry_version_fails_before_conversion():
    with pytest.raises(registry.UnknownRegistryVersion):
        registry.convert_value(Decimal("1"), "W", "kW", registry_version="999")


def test_snapshot_copies_inputs_and_hash_detects_drift():
    original = registry.registry_snapshot("1")
    units = dict(original.units)
    copy = registry.make_snapshot("test", units, original.aliases, original.metrics)
    units["W"] = replace(units["W"], scale=Decimal("2"))
    assert copy.units["W"].scale == Decimal("1")
    with pytest.raises(RuntimeError, match="snapshot"):
        registry.assert_snapshot_hash(copy, "0" * 64)


def test_shipped_factor_drift_fails_closed_at_import(tmp_path):
    source = Path(registry.__file__).read_text(encoding="utf-8")
    source = source.replace('Decimal("0.00047194744320")', 'Decimal("0.00047")')
    drifted = tmp_path / "drifted_registry.py"
    drifted.write_text(source, encoding="utf-8")
    # The copied module still imports the v2 semantics package. Bootstrap the
    # backend root explicitly so this probe does not depend on PYTHONPATH.
    backend_root = Path(registry.__file__).resolve().parents[3]
    bootstrap = (
        "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
        "runpy.run_path(sys.argv[2], run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(backend_root), str(drifted)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "differs from its frozen contract" in result.stderr


def test_alarm_comparison_uses_both_reading_and_threshold_snapshot(monkeypatch):
    from types import SimpleNamespace

    from app.application.alarm_service import comparison_for
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["kW"] = replace(units["kW"], scale=Decimal("2000"))
    v2 = registry.make_snapshot("2", units, v1.aliases, v1.metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))
    reading = SimpleNamespace(metric="power_kw", unit="kW", value=Decimal("1"), registry_version="2")
    rule = SimpleNamespace(metric="power_kw", unit="kW", registry_version=None)
    assert comparison_for(rule, reading) == (Decimal("2.00000000"), "kW", None)


def test_catalog_candidate_conversion_uses_explicit_version(monkeypatch):
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["lb"] = replace(units["lb"], scale=Decimal("0.5"))
    v2 = registry.make_snapshot("2", units, v1.aliases, v1.metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))
    assert registry.convert_catalog_candidate("weight", Decimal("2"), "lb", registry_version="2").value == Decimal("1.00000000")
    assert registry.convert_catalog_candidate("weight", Decimal("2"), "lb", registry_version="1").value == Decimal("0.90718474")


def test_alarm_accepts_a_reading_unit_defined_only_in_its_source_snapshot(monkeypatch):
    from types import SimpleNamespace

    from app.application.alarm_service import comparison_for
    v1 = registry.registry_snapshot("1")
    units = dict(v1.units)
    units["MW"] = registry.UnitDefinition("MW", "power", Decimal("1000000"))
    metrics = dict(v1.metrics)
    metrics["power_kw"] = replace(metrics["power_kw"], canonical_unit="MW")
    v2 = registry.make_snapshot("2", units, v1.aliases, metrics)
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": v1, "2": v2}))
    reading = SimpleNamespace(metric="power_kw", unit="MW", value=Decimal("1"), registry_version="2")
    rule = SimpleNamespace(metric="power_kw", unit="kW", registry_version="1")
    assert comparison_for(rule, reading) == (Decimal("1000.00000000"), "kW", "1")
