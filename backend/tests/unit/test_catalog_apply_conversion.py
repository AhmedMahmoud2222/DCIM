"""Storage-boundary conversions, semantic separation and precision failures."""

import uuid
from decimal import Decimal

import pytest

from app.application.catalog_documents.extraction.apply import candidate_patch
from app.domain.catalog.designer_models import CatalogModelRevision
from app.domain.catalog.extraction_models import CatalogExtractionCandidate


def candidate(field, number=None, unit=None, text=None, **changes):
    values = dict(
        id=uuid.uuid4(), field_key=field, value_numeric=number, unit=unit, value_text=text, value_max=None,
        raw_value=str(number if number is not None else text), raw_unit=unit or "", flags=[],
        source_text="Datasheet evidence", page_number=1, method="native", confidence=0.9,
        model_match="target", model_context="CX-100",
    )
    values.update(changes)
    return CatalogExtractionCandidate(**values)


@pytest.mark.parametrize("field,value,unit,target,expected", [
    ("power_rated_w", 1.2, "kW", "rated_power_w", Decimal("1200.00")),
    ("power_typical_w", 25, "W", "typical_power_w", Decimal("25.00")),
    ("power_max_w", 2, "kW", "max_power_w", Decimal("2000.00")),
    ("width", 19, "in", "width_value", Decimal("482.600")),
    ("weight", 20, "lb", "weight_value", Decimal("9.072")),
    ("heat_dissipation", 1195, "BTU/hr", "heat_dissipation_btu_hr", Decimal("1195.00")),
    ("rack_units", 2, "U", "rack_unit_height", 2),
])
def test_supported_numeric_contracts(field, value, unit, target, expected):
    patch, evidence = candidate_patch(candidate(field, value, unit), CatalogModelRevision())
    assert patch[target] == expected
    assert evidence["source_unit"] == unit and evidence["source_text"] == "Datasheet evidence"
    assert evidence["applied_value"] == str(expected)


def test_exact_airflow_enum_mapping():
    patch, source = candidate_patch(candidate("airflow_direction", text="front-to-back"), CatalogModelRevision())
    assert patch == {"airflow_direction": "front_to_rear"}
    assert source["raw_value"] == "front-to-back" and source["conversion_contract"] == "catalog-fixed-v1"


@pytest.mark.parametrize("item", [
    candidate("power_typical_w", -0.001, "W"),
    candidate("weight", 0, "kg"),
    candidate("width", 0.00001, "mm"),
    candidate("width", 10000000, "mm"),
    candidate("weight", 1, None),
    candidate("weight", 1, "W"),
    candidate("weight", 1, "kg", value_max=2),
    candidate("weight", 1, "kg", flags=["number_format_ambiguous"]),
    candidate("shipping_weight", 1, "kg"),
    candidate("power_unspecified_w", 20, "W"),
    candidate("rack_units", 1.5, "U"),
    candidate("heat_dissipation", 10, "W"),
    candidate("airflow_direction", text="back-to-front"),
])
def test_unsupported_ambiguous_or_invalid_values_are_never_coerced(item):
    with pytest.raises(ValueError):
        candidate_patch(item, CatalogModelRevision())


def test_existing_values_without_unit_are_not_silently_relabelled():
    with pytest.raises(ValueError, match="no unit"):
        candidate_patch(candidate("width", 440, "mm"), CatalogModelRevision(height_value=2))


def test_authored_inches_are_preserved():
    patch, evidence = candidate_patch(candidate("width", 440, "mm"), CatalogModelRevision(dimension_unit="in", height_value=2))
    assert patch == {"dimension_unit": "in", "width_value": Decimal("17.323")}
    assert evidence["canonical_unit"] == "mm" and evidence["applied_unit"] == "in"
