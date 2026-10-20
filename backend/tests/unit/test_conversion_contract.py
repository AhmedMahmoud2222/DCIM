"""Issue #128 / G1: the conversion contract hash is deterministic and detects any change of meaning."""

import hashlib
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

import pytest

from app.domain.telemetry import registry
from app.domain.telemetry.contract import (
    conversion_contract_hash,
    conversion_contract_hash_or_unresolved,
    conversion_contract_text,
)

FAHRENHEIT = ("temperature_c", "degF", Decimal("1"), "1")


def test_canonical_encoding_is_a_fixed_line_sequence():
    assert conversion_contract_text(*FAHRENHEIT).split("\n") == [
        "dcim.mapping-conversion.v1",
        "metric=temperature_c",
        "registry_version=1",
        "source_unit=degF",
        "source_scale=1",
        "source_symbol=degF",
        "source_dimension=temperature",
        "source_unit_scale=0.5555555555555555555555555556",
        "source_unit_offset=-32",
        "canonical_unit=degC",
        "canonical_dimension=temperature",
        "canonical_unit_scale=1",
        "canonical_unit_offset=0",
        "precision=0.00000001",
        "rounding=ROUND_HALF_EVEN",
    ]


def test_golden_hash_pins_the_encoding_and_the_registry():
    """If this fails, either the encoding changed (bump its format line) or a registry definition changed meaning."""
    expected = hashlib.sha256(conversion_contract_text(*FAHRENHEIT).encode()).hexdigest()
    assert conversion_contract_hash(*FAHRENHEIT) == expected
    assert expected == "86044c6d3f4ad702dfd5f5166342b1f57aa668050a9a1aaf547bfcf802ba320e"


def test_equal_numeric_values_encode_identically_whatever_their_representation():
    assert (
        conversion_contract_hash("power_kw", "W", Decimal("1"), "1")
        == conversion_contract_hash("power_kw", "W", Decimal("1.00000000"), "1")
        == conversion_contract_hash("power_kw", "W", Decimal("1E+0"), "1")
    )
    assert "source_scale=1000" in conversion_contract_text("power_kw", "W", Decimal("1E+3"), "1")


@pytest.mark.parametrize("other", [
    ("temperature_c", "degC", Decimal("1"), "1"),       # source unit
    ("temperature_c", "degF", Decimal("1.00000001"), "1"),  # source scale, smallest storable step
    ("supply_air_temperature_c", "degF", Decimal("1"), "1"),  # metric
    ("temperature_c", "degF", Decimal("1"), None),      # legacy (pre-registry) interpretation
])
def test_every_input_changes_the_hash(other):
    assert conversion_contract_hash(*other) != conversion_contract_hash(*FAHRENHEIT)


def test_a_pinned_revision_cannot_silently_drift_when_the_registry_changes(monkeypatch):
    stamped = conversion_contract_hash(*FAHRENHEIT)
    original = registry.registry_snapshot("1")
    with pytest.raises(TypeError):
        registry.UNITS["degF"] = replace(original.units["degF"], scale=Decimal("0.5555556"))
    for symbol, field, value in (("degF", "scale", Decimal("0.5555556")), ("degC", "offset", Decimal("1"))):
        units = dict(original.units)
        units[symbol] = replace(units[symbol], **{field: value})
        drifted = registry.make_snapshot("1", units, original.aliases, original.metrics)
        monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": drifted}))
        assert conversion_contract_hash(*FAHRENHEIT) != stamped
    drifted = registry.make_snapshot("1", original.units, original.aliases, original.metrics, precision=Decimal("0.001"))
    monkeypatch.setattr(registry, "REGISTRIES", MappingProxyType({"1": drifted}))
    assert conversion_contract_hash(*FAHRENHEIT) != stamped
    monkeypatch.undo()
    assert conversion_contract_hash(*FAHRENHEIT) == stamped


def test_legacy_encoding_has_no_resolved_definitions_and_is_stable_for_unknown_units():
    text = conversion_contract_text("temperature_c", "furlongs", Decimal("10"), None)
    assert text.split("\n")[2] == "registry_version=legacy"
    assert "source_symbol" not in text
    assert conversion_contract_hash("temperature_c", "furlongs", Decimal("10"), None) == hashlib.sha256(text.encode()).hexdigest()


def test_unresolvable_mapping_gets_a_marker_hash_that_never_matches_a_recomputation():
    marker = conversion_contract_hash_or_unresolved("temperature_c", "bananas", Decimal("1"), "1")
    assert len(marker) == 64
    with pytest.raises(registry.UnknownUnit):
        conversion_contract_hash("temperature_c", "bananas", Decimal("1"), "1")
