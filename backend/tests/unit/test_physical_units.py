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


def test_source_scale_is_recorded_and_applied_before_one_unit_conversion():
    from app.domain.telemetry.registry import convert_to_canonical

    converted = convert_to_canonical("power_kw", Decimal("1250"), "W", source_scale=Decimal("1"))

    assert converted.value == Decimal("1.25000000")
    assert converted.raw_value == Decimal("1250")
    assert converted.raw_unit == "W"
    assert converted.source_scale == Decimal("1")


def test_unknown_registry_version_is_not_silently_interpreted_as_current():
    from app.domain.telemetry.registry import UnknownRegistryVersion, convert_to_presentation

    with pytest.raises(UnknownRegistryVersion):
        convert_to_presentation("power_kw", Decimal("1"), "kW", registry_version="future")


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


def test_presentation_boundary_accepts_numeric_values_returned_by_the_orm():
    from app.domain.telemetry.registry import convert_to_presentation

    presented = convert_to_presentation("availability", 0.995)

    assert presented.value == Decimal("99.50000000")


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


def test_catalog_candidate_handoff_converts_real_parser_output_to_apply_contract():
    from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
    from app.application.catalog_documents.extraction.unit_handoff import convert_extracted_catalog_candidate

    result = analyze_pages(
        [PageText(1, "MX-1 Technical Specifications\nRated power: 1.2 kW\nWidth: 30 cm\nWeight: 4 lb", "native")],
        ["MX-1"],
    )
    converted = {candidate.field_key: convert_extracted_catalog_candidate(candidate)
                 for candidate in result.candidates}

    assert converted["power_rated_w"].value == Decimal("1200.00000000")
    assert converted["width"].value == Decimal("300.00000000")
    assert converted["weight"].value == Decimal("1.81436948")
    for candidate in result.candidates:
        applied = converted[candidate.field_key]
        assert applied.raw_value == candidate.raw_value
        assert applied.raw_unit == candidate.raw_unit
        assert applied.source_value == Decimal(str(candidate.value_numeric))
        assert applied.source_unit == candidate.unit


def test_sandboxed_catalog_extractor_declares_the_same_registry_contract_version():
    from app.application.catalog_documents.extraction.candidates import UNIT_REGISTRY_VERSION
    from app.domain.telemetry.registry import REGISTRY_VERSION

    assert UNIT_REGISTRY_VERSION == REGISTRY_VERSION


def test_real_parser_grams_handoff_preserves_provenance():
    from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
    from app.application.catalog_documents.extraction.unit_handoff import convert_extracted_catalog_candidate

    candidate, = analyze_pages([PageText(1, "MX-1 Technical Specifications\nWeight: 500 g", "native")], ["MX-1"]).candidates
    assert candidate.unit == "g"
    applied = convert_extracted_catalog_candidate(candidate)
    assert applied.value == Decimal("0.50000000")
    assert applied.unit == "kg"
    assert applied.raw_value == candidate.raw_value == "500"
    assert applied.raw_unit == candidate.raw_unit == "g"


@pytest.mark.parametrize("text,reason", [
    ("Operating temperature: 10-35 °C", "Ranged"),
    ("Airflow direction: front to rear", "scalar numeric"),
])
def test_real_parser_range_and_text_are_not_silently_applied(text, reason):
    from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
    from app.application.catalog_documents.extraction.unit_handoff import convert_extracted_catalog_candidate

    candidate, = analyze_pages([PageText(1, f"MX-1 Technical Specifications\n{text}", "native")], ["MX-1"]).candidates
    with pytest.raises(ValueError, match=reason):
        convert_extracted_catalog_candidate(candidate)


def test_missing_numeric_candidate_is_rejected_without_coercing_raw_value():
    from dataclasses import replace

    from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
    from app.application.catalog_documents.extraction.unit_handoff import convert_extracted_catalog_candidate

    candidate, = analyze_pages([PageText(1, "MX-1 Technical Specifications\nWeight: 500 g", "native")], ["MX-1"]).candidates
    with pytest.raises(ValueError, match="scalar numeric"):
        convert_extracted_catalog_candidate(replace(candidate, value_numeric=None))


def test_grams_dimension_validation_and_unknown_unit_rejection():
    from app.domain.telemetry.registry import UnitDimensionMismatch, UnknownUnit, convert_catalog_candidate

    with pytest.raises(UnitDimensionMismatch):
        convert_catalog_candidate("width", Decimal("500"), "g")
    with pytest.raises(UnknownUnit):
        convert_catalog_candidate("weight", Decimal("500"), "stone")
