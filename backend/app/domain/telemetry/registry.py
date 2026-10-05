"""Versioned physical-unit and telemetry metric contracts.

Conversions are deliberately pure and Decimal based.  Persistence layers retain the
source value/unit and store the returned canonical value/unit as separate fields.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

REGISTRY_VERSION = "1"
PRECISION = Decimal("0.00000001")


class UnknownMetric(ValueError):
    pass


class UnknownUnit(ValueError):
    pass


class UnitDimensionMismatch(ValueError):
    pass


@dataclass(frozen=True)
class UnitDefinition:
    symbol: str
    dimension: str
    scale: Decimal = Decimal("1")
    offset: Decimal = Decimal("0")


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    dimension: str
    canonical_unit: str
    presentation_unit: str


@dataclass(frozen=True)
class CanonicalValue:
    value: Decimal
    unit: str
    raw_value: Decimal
    raw_unit: str
    registry_version: str = REGISTRY_VERSION


@dataclass(frozen=True)
class PresentationValue:
    value: Decimal
    unit: str


UNITS = {
    "degC": UnitDefinition("degC", "temperature"),
    "degF": UnitDefinition("degF", "temperature", Decimal("0.5555555555555555555555555556"), Decimal("-32")),
    "K": UnitDefinition("K", "temperature", Decimal("1"), Decimal("-273.15")),
    "%": UnitDefinition("%", "ratio", Decimal("0.01")),
    "1": UnitDefinition("1", "ratio"),
    "W": UnitDefinition("W", "power"),
    "kW": UnitDefinition("kW", "power", Decimal("1000")),
    "V": UnitDefinition("V", "voltage"),
    "A": UnitDefinition("A", "current"),
    "mm": UnitDefinition("mm", "length", Decimal("0.001")),
    "cm": UnitDefinition("cm", "length", Decimal("0.01")),
    "m": UnitDefinition("m", "length"),
    "in": UnitDefinition("in", "length", Decimal("0.0254")),
    "ft": UnitDefinition("ft", "length", Decimal("0.3048")),
    "kg": UnitDefinition("kg", "mass"),
    "lb": UnitDefinition("lb", "mass", Decimal("0.45359237")),
}

UNIT_ALIASES = {
    "c": "degC",
    "celsius": "degC",
    "°c": "degC",
    "f": "degF",
    "fahrenheit": "degF",
    "°f": "degF",
    "kelvin": "K",
    "percent": "%",
    "pct": "%",
    "ratio": "1",
    "watts": "W",
    "watt": "W",
    "kilowatts": "kW",
    "kilowatt": "kW",
    "volts": "V",
    "volt": "V",
    "amps": "A",
    "amp": "A",
}

METRIC_REGISTRY = {
    "temperature_c": MetricDefinition("temperature_c", "temperature", "degC", "degC"),
    "humidity_percent": MetricDefinition("humidity_percent", "ratio", "%", "%"),
    "power_kw": MetricDefinition("power_kw", "power", "kW", "kW"),
    "load_percent": MetricDefinition("load_percent", "ratio", "%", "%"),
    "availability": MetricDefinition("availability", "ratio", "1", "%"),
}


def normalize_unit(unit: str) -> str:
    stripped = unit.strip()
    if stripped in UNITS:
        return stripped
    canonical = UNIT_ALIASES.get(stripped.lower())
    if canonical is None:
        raise UnknownUnit(f"Unknown physical unit: {unit!r}")
    return canonical


def convert_value(value: Decimal, source_unit: str, target_unit: str) -> Decimal:
    source = UNITS[normalize_unit(source_unit)]
    target = UNITS[normalize_unit(target_unit)]
    if source.dimension != target.dimension:
        raise UnitDimensionMismatch(
            f"Cannot convert {source.symbol} ({source.dimension}) to {target.symbol} ({target.dimension})."
        )
    base_value = (value + source.offset) * source.scale
    converted = (base_value / target.scale) - target.offset
    return converted.quantize(PRECISION, rounding=ROUND_HALF_EVEN)


def validate_metric_unit(metric: str, source_unit: str) -> str:
    definition = METRIC_REGISTRY.get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    normalized = normalize_unit(source_unit)
    actual_dimension = UNITS[normalized].dimension
    if actual_dimension != definition.dimension:
        raise UnitDimensionMismatch(
            f"Metric {metric!r} requires dimension {definition.dimension}; unit {source_unit!r} is {actual_dimension}."
        )
    return normalized


def convert_to_canonical(metric: str, value: Decimal, source_unit: str) -> CanonicalValue:
    definition = METRIC_REGISTRY.get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    normalized = validate_metric_unit(metric, source_unit)
    return CanonicalValue(
        value=convert_value(value, normalized, definition.canonical_unit),
        unit=definition.canonical_unit,
        raw_value=value,
        raw_unit=source_unit,
    )


def convert_to_presentation(metric: str, value: Decimal, source_unit: str | None = None) -> PresentationValue:
    """Render a stored canonical value without mutating or reinterpreting its source provenance."""
    definition = METRIC_REGISTRY.get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    unit = source_unit or definition.canonical_unit
    validate_metric_unit(metric, unit)
    return PresentationValue(
        value=convert_value(value, unit, definition.presentation_unit),
        unit=definition.presentation_unit,
    )


def validate_catalog_candidate_unit(unit: str | None, dimension: str) -> str:
    """Executable handoff used when PR #109 applies a provenance-bearing extraction candidate."""
    if unit is None:
        raise UnknownUnit("Catalog extraction candidate has no unit.")
    normalized = normalize_unit(unit)
    actual_dimension = UNITS[normalized].dimension
    if actual_dimension != dimension:
        raise UnitDimensionMismatch(f"Catalog candidate requires dimension {dimension}; unit {unit!r} is {actual_dimension}.")
    return normalized
