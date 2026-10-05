from decimal import Decimal

import pytest


def test_temperature_round_trip_preserves_source_value_and_uses_canonical_celsius():
    from app.domain.telemetry.registry import convert_to_canonical, convert_value

    converted = convert_to_canonical("temperature_c", Decimal("77.0"), "degF")

    assert converted.value == Decimal("25.00000000")
    assert converted.unit == "degC"
    assert converted.raw_value == Decimal("77.0")
    assert converted.raw_unit == "degF"
    assert convert_value(converted.value, "degC", "degF") == Decimal("77.00000000")


def test_power_conversion_does_not_double_convert_canonical_input():
    from app.domain.telemetry.registry import convert_to_canonical

    converted = convert_to_canonical("power_kw", Decimal("1.25000000"), "kW")

    assert converted.value == Decimal("1.25000000")
    assert converted.raw_value == Decimal("1.25000000")
    assert converted.raw_unit == "kW"


def test_metric_rejects_unit_from_wrong_dimension():
    from app.domain.telemetry.registry import UnitDimensionMismatch, convert_to_canonical

    with pytest.raises(UnitDimensionMismatch, match="temperature_c.*kW"):
        convert_to_canonical("temperature_c", Decimal("12"), "kW")


def test_mapping_validation_accepts_legacy_alias_but_returns_registry_symbol():
    from app.domain.telemetry.registry import validate_metric_unit

    assert validate_metric_unit("temperature_c", "celsius") == "degC"


def test_registry_is_versioned_and_exposes_explicit_presentation_units():
    from app.domain.telemetry.registry import METRIC_REGISTRY, REGISTRY_VERSION

    assert REGISTRY_VERSION == "1"
    assert METRIC_REGISTRY["temperature_c"].canonical_unit == "degC"
    assert METRIC_REGISTRY["humidity_percent"].presentation_unit == "%"
    assert METRIC_REGISTRY["availability"].dimension == "ratio"


def test_canonical_availability_converts_to_explicit_percent_presentation():
    from app.domain.telemetry.registry import convert_to_presentation

    presented = convert_to_presentation("availability", Decimal("0.995"))

    assert presented.value == Decimal("99.50000000")
    assert presented.unit == "%"


def test_catalog_candidate_handoff_validates_real_parser_output_by_dimension():
    from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
    from app.domain.telemetry.registry import validate_catalog_candidate_unit

    result = analyze_pages(
        [PageText(1, "MX-1 Technical Specifications\nRated power: 1.2 kW\nWidth: 30 cm\nWeight: 4 lb", "native")],
        ["MX-1"],
    )
    dimensions = {"power_rated_w": "power", "width": "length", "weight": "mass"}

    assert len(result.candidates) == 3
    for candidate in result.candidates:
        assert validate_catalog_candidate_unit(candidate.unit, dimensions[candidate.field_key]) == candidate.unit


def test_sandboxed_catalog_extractor_declares_the_same_registry_contract_version():
    from app.application.catalog_documents.extraction.candidates import UNIT_REGISTRY_VERSION
    from app.domain.telemetry.registry import REGISTRY_VERSION

    assert UNIT_REGISTRY_VERSION == REGISTRY_VERSION
