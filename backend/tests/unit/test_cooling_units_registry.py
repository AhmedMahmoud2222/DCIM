"""Issue #105: canonical airflow / velocity / pressure units and the new metric registry entries."""

from decimal import Decimal

import pytest

from app.domain.telemetry.registry import (
    METRIC_REGISTRY,
    UnitDimensionMismatch,
    convert_to_canonical,
    convert_to_presentation,
    convert_value,
)


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        ("3600", "m3/h", "m3/s", "1.00000000"),
        ("1", "m3/s", "m3/h", "3600.00000000"),
        ("1000", "L/s", "m3/s", "1.00000000"),
        ("1000", "CFM", "m3/s", "0.47194744"),
        ("200", "ft/min", "m/s", "1.01600000"),
        ("3.6", "km/h", "m/s", "1.00000000"),
        ("1", "kPa", "Pa", "1000.00000000"),
        ("1", "inH2O", "Pa", "249.08891000"),
    ],
)
def test_conversions_are_exact_to_the_registry_precision(value, source, target, expected):
    assert str(convert_value(Decimal(value), source, target)) == expected


def test_new_metrics_have_canonical_and_presentation_units():
    assert METRIC_REGISTRY["airflow_m3_s"].canonical_unit == "m3/s"
    assert METRIC_REGISTRY["airflow_m3_s"].presentation_unit == "m3/h"
    assert METRIC_REGISTRY["airflow_velocity_m_s"].canonical_unit == "m/s"
    assert METRIC_REGISTRY["differential_pressure_pa"].canonical_unit == "Pa"
    assert METRIC_REGISTRY["supply_air_temperature_c"].dimension == "temperature"
    assert METRIC_REGISTRY["cooling_output_kw"].canonical_unit == "kW"


def test_ingest_conversion_keeps_the_raw_source_value_and_unit():
    canonical = convert_to_canonical("airflow_m3_s", Decimal("7200"), "m3/h")
    assert (canonical.value, canonical.unit, canonical.raw_value, canonical.raw_unit) == (Decimal("2.00000000"), "m3/s", Decimal("7200"), "m3/h")
    assert convert_to_canonical("differential_pressure_pa", Decimal("0.5"), "inwc").unit == "Pa"
    assert convert_to_canonical("airflow_m3_s", Decimal("1000"), "cfm").value == Decimal("0.47194744")


def test_presentation_conversion_round_trips_airflow():
    shown = convert_to_presentation("airflow_m3_s", Decimal("2"))
    assert (shown.value, shown.unit) == (Decimal("7200.00000000"), "m3/h")


@pytest.mark.parametrize(("metric", "unit"), [("airflow_m3_s", "Pa"), ("differential_pressure_pa", "m3/s"), ("airflow_velocity_m_s", "kW"), ("temperature_c", "m/s")])
def test_a_unit_of_the_wrong_dimension_is_rejected(metric, unit):
    with pytest.raises(UnitDimensionMismatch):
        convert_to_canonical(metric, Decimal("1"), unit)


def test_existing_metric_contracts_are_unchanged():
    assert METRIC_REGISTRY["temperature_c"].canonical_unit == "degC"
    assert METRIC_REGISTRY["humidity_percent"].canonical_unit == "%"
    assert convert_value(Decimal("212"), "degF", "degC") == Decimal("100.00000000")
