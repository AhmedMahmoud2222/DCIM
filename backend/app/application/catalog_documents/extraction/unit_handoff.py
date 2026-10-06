"""Scalar-only parser → catalog apply contract for PR #109.

Ranges and text need an explicit owner decision; selecting their lower endpoint or
coercing their text into a number would silently discard extraction evidence.
"""

from dataclasses import dataclass
from decimal import Decimal

from typing import Protocol

from app.domain.telemetry.registry import REGISTRY_VERSION, convert_catalog_candidate


class ScalarCandidate(Protocol):
    field_key: str
    value_numeric: float | None
    value_max: float | None
    value_text: str | None
    unit: str | None
    raw_value: str
    raw_unit: str


@dataclass(frozen=True)
class CatalogScalarValue:
    value: Decimal
    unit: str
    raw_value: str
    raw_unit: str
    source_value: Decimal
    source_unit: str
    registry_version: str = REGISTRY_VERSION


def convert_extracted_catalog_candidate(candidate: ScalarCandidate) -> CatalogScalarValue:
    if candidate.value_max is not None:
        raise ValueError("Ranged catalog candidate requires an explicit range selection.")
    if candidate.value_numeric is None or candidate.value_text is not None:
        raise ValueError("Catalog apply requires a scalar numeric candidate, not text or a missing value.")
    source_value = Decimal(str(candidate.value_numeric))
    if not source_value.is_finite():
        raise ValueError("Catalog apply requires a finite numeric candidate.")
    converted = convert_catalog_candidate(candidate.field_key, source_value, candidate.unit)
    return CatalogScalarValue(
        value=converted.value,
        unit=converted.unit,
        raw_value=candidate.raw_value,
        raw_unit=candidate.raw_unit,
        source_value=source_value,
        source_unit=converted.raw_unit,
    )
