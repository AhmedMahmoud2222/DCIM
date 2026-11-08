"""Versioned physical-unit and telemetry metric contracts.

Conversions are deliberately pure and Decimal based.  Persistence layers retain the
source value/unit and store the returned canonical value/unit as separate fields.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from types import MappingProxyType

from app.domain.telemetry.metric_semantics import V2_SEMANTICS, MetricSemantics

REGISTRY_VERSION = "1"
PRECISION = Decimal("0.00000001")


class UnknownMetric(ValueError):
    pass


class UnknownUnit(ValueError):
    pass


class UnitDimensionMismatch(ValueError):
    pass


class UnknownRegistryVersion(ValueError):
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
    semantics: MetricSemantics | None = None


@dataclass(frozen=True)
class CanonicalValue:
    value: Decimal
    unit: str
    raw_value: Decimal
    raw_unit: str
    source_scale: Decimal = Decimal("1")
    registry_version: str = REGISTRY_VERSION


@dataclass(frozen=True)
class PresentationValue:
    value: Decimal
    unit: str


UNITS: Mapping[str, UnitDefinition] = {
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
    "g": UnitDefinition("g", "mass", Decimal("0.001")),
    "lb": UnitDefinition("lb", "mass", Decimal("0.45359237")),
    # Issue #105: airflow volume, airflow velocity and differential pressure. Scales are exact
    # (1 CFM = 0.3048^3 / 60 m3/s; 1 ft/min = 0.3048 / 60 m/s) except the conventional inch of water
    # (ISO 80000-4: 249.08891 Pa), which is the conventional value rather than a temperature-specific one.
    "m3/s": UnitDefinition("m3/s", "volumetric_flow"),
    "m3/h": UnitDefinition("m3/h", "volumetric_flow", Decimal("0.0002777777777777777777777778")),
    "L/s": UnitDefinition("L/s", "volumetric_flow", Decimal("0.001")),
    "CFM": UnitDefinition("CFM", "volumetric_flow", Decimal("0.00047194744320")),
    "m/s": UnitDefinition("m/s", "velocity"),
    "ft/min": UnitDefinition("ft/min", "velocity", Decimal("0.00508")),
    "km/h": UnitDefinition("km/h", "velocity", Decimal("0.2777777777777777777777777778")),
    "Pa": UnitDefinition("Pa", "pressure"),
    "kPa": UnitDefinition("kPa", "pressure", Decimal("1000")),
    "inH2O": UnitDefinition("inH2O", "pressure", Decimal("249.08891")),
}

UNIT_ALIASES: Mapping[str, str] = {
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
    "cfm": "CFM",
    "l/s": "L/s",
    "fpm": "ft/min",
    "pascal": "Pa",
    "pascals": "Pa",
    "inwc": "inH2O",
    "inh2o": "inH2O",
}

METRIC_REGISTRY: Mapping[str, MetricDefinition] = {
    "temperature_c": MetricDefinition("temperature_c", "temperature", "degC", "degC"),
    "humidity_percent": MetricDefinition("humidity_percent", "ratio", "%", "%"),
    "power_kw": MetricDefinition("power_kw", "power", "kW", "kW"),
    "load_percent": MetricDefinition("load_percent", "ratio", "%", "%"),
    "availability": MetricDefinition("availability", "ratio", "1", "%"),
    # Issue #105 cooling / environment metrics. Added to registry version 1 because no existing key, unit or
    # stored value changes meaning; historical rows are untouched.
    "supply_air_temperature_c": MetricDefinition("supply_air_temperature_c", "temperature", "degC", "degC"),
    "return_air_temperature_c": MetricDefinition("return_air_temperature_c", "temperature", "degC", "degC"),
    "airflow_m3_s": MetricDefinition("airflow_m3_s", "volumetric_flow", "m3/s", "m3/h"),
    "airflow_velocity_m_s": MetricDefinition("airflow_velocity_m_s", "velocity", "m/s", "m/s"),
    "differential_pressure_pa": MetricDefinition("differential_pressure_pa", "pressure", "Pa", "Pa"),
    "cooling_output_kw": MetricDefinition("cooling_output_kw", "power", "kW", "kW"),
}


@dataclass(frozen=True)
class RegistrySnapshot:
    version: str
    units: Mapping[str, UnitDefinition]
    aliases: Mapping[str, str]
    metrics: Mapping[str, MetricDefinition]
    precision: Decimal
    rounding: str
    decimal_precision: int
    content_hash: str


def _plain(value: Decimal) -> str:
    # No normalize(): it rounds according to the ambient Decimal context.
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"", "-0"} else result


def snapshot_text(snapshot: RegistrySnapshot) -> str:
    """Canonical UTF-8/LF key=value encoding, sorted map keys, no final newline.

    Decimal values use plain notation without insignificant trailing zeroes.
    Alias spelling and metric names are case sensitive. All meaning-bearing fields,
    including presentation and the complete arithmetic context, enter the digest.
    """
    lines = ["dcim.registry-snapshot.v1", f"version={snapshot.version}",
             f"precision={_plain(snapshot.precision)}", f"rounding={snapshot.rounding}",
             f"decimal_precision={snapshot.decimal_precision}"]
    for key, unit in sorted(snapshot.units.items()):
        lines.extend((f"unit.{key}.symbol={unit.symbol}", f"unit.{key}.dimension={unit.dimension}",
                      f"unit.{key}.scale={_plain(unit.scale)}", f"unit.{key}.offset={_plain(unit.offset)}"))
    for key, alias in sorted(snapshot.aliases.items()):
        lines.append(f"alias.{key}={alias}")
    for key, metric in sorted(snapshot.metrics.items()):
        lines.extend((f"metric.{key}.key={metric.key}", f"metric.{key}.dimension={metric.dimension}",
                      f"metric.{key}.canonical_unit={metric.canonical_unit}",
                      f"metric.{key}.presentation_unit={metric.presentation_unit}"))
        if metric.semantics is not None:
            policy = metric.semantics
            minimum = _plain(policy.min_canonical) if policy.min_canonical is not None else "none"
            maximum = _plain(policy.max_canonical) if policy.max_canonical is not None else "none"
            lines.extend((f"metric.{key}.value_kind={policy.value_kind.value}",
                          f"metric.{key}.min_canonical={minimum}",
                          f"metric.{key}.max_canonical={maximum}",
                          f"metric.{key}.allowed_source_units={','.join(policy.allowed_source_units)}",
                          f"metric.{key}.aggregations={','.join(policy.aggregations)}",
                          f"metric.{key}.rule_types={','.join(policy.rule_types)}"))
    return "\n".join(lines)


def make_snapshot(
    version: str, units: Mapping[str, UnitDefinition], aliases: Mapping[str, str],
    metrics: Mapping[str, MetricDefinition], *, precision: Decimal = PRECISION,
    rounding: str = ROUND_HALF_EVEN, decimal_precision: int = 28,
) -> RegistrySnapshot:
    """Copy-on-write factory; its copied maps cannot be mutated through an input."""
    snapshot = RegistrySnapshot(version, MappingProxyType(dict(units)), MappingProxyType(dict(aliases)),
                                MappingProxyType(dict(metrics)), precision, rounding, decimal_precision, "")
    digest = hashlib.sha256(snapshot_text(snapshot).encode("utf-8")).hexdigest()
    return RegistrySnapshot(version, snapshot.units, snapshot.aliases, snapshot.metrics,
                            precision, rounding, decimal_precision, digest)


def assert_snapshot_hash(snapshot: RegistrySnapshot, expected: str) -> None:
    computed = hashlib.sha256(snapshot_text(snapshot).encode("utf-8")).hexdigest()
    if computed != expected or snapshot.content_hash != expected:
        raise RuntimeError(f"Registry snapshot {snapshot.version!r} differs from its frozen contract")


_V1 = make_snapshot("1", UNITS, UNIT_ALIASES, METRIC_REGISTRY)
# Pinned after capturing the exact shipped v1 definitions; changes require a new version.
V1_CONTENT_HASH = "d29e5d41a996e5b081583e87f8e6a270b4040b682432c16aaad7b0f22f9ac6d4"
assert_snapshot_hash(_V1, V1_CONTENT_HASH)
UNITS = _V1.units
UNIT_ALIASES = _V1.aliases
METRIC_REGISTRY = _V1.metrics
_V2 = make_snapshot("2", _V1.units, _V1.aliases,
                    {key: replace(metric, semantics=V2_SEMANTICS[key]) for key, metric in _V1.metrics.items()})
V2_CONTENT_HASH = "2f9bd2d613f32baed8322fc244d5f33d74437d0fb10f53bc047fff2ce8e8c147"
assert_snapshot_hash(_V2, V2_CONTENT_HASH)
REGISTRIES: Mapping[str, RegistrySnapshot] = MappingProxyType({"1": _V1, "2": _V2})


def registry_snapshot(version: str = "1") -> RegistrySnapshot:
    try:
        return REGISTRIES[version]
    except KeyError as error:
        raise UnknownRegistryVersion(f"Unknown unit registry version: {version!r}") from error


@dataclass(frozen=True)
class CatalogFieldContract:
    dimension: str
    canonical_unit: str


CATALOG_FIELD_CONTRACTS = {
    "power_rated_w": CatalogFieldContract("power", "W"),
    "power_typical_w": CatalogFieldContract("power", "W"),
    "power_max_w": CatalogFieldContract("power", "W"),
    "width": CatalogFieldContract("length", "mm"),
    "height": CatalogFieldContract("length", "mm"),
    "depth": CatalogFieldContract("length", "mm"),
    "weight": CatalogFieldContract("mass", "kg"),
    "shipping_weight": CatalogFieldContract("mass", "kg"),
}


def _registry(version: str) -> Mapping[str, MetricDefinition]:
    return registry_snapshot(version).metrics


def normalize_unit(unit: str, *, registry_version: str = "1") -> str:
    snapshot = registry_snapshot(registry_version)
    stripped = unit.strip()
    if stripped in snapshot.units:
        return stripped
    canonical = snapshot.aliases.get(stripped.lower())
    if canonical is None:
        raise UnknownUnit(f"Unknown physical unit: {unit!r}")
    return canonical


def convert_value(
    value: Decimal, source_unit: str, target_unit: str, *, registry_version: str = "1",
    target_registry_version: str | None = None,
) -> Decimal:
    snapshot = registry_snapshot(registry_version)
    target_snapshot = registry_snapshot(target_registry_version or registry_version)
    source = snapshot.units[normalize_unit(source_unit, registry_version=registry_version)]
    target = target_snapshot.units[normalize_unit(target_unit, registry_version=target_snapshot.version)]
    if source.dimension != target.dimension:
        raise UnitDimensionMismatch(
            f"Cannot convert {source.symbol} ({source.dimension}) to {target.symbol} ({target.dimension})."
        )
    with localcontext(Context(prec=snapshot.decimal_precision, rounding=snapshot.rounding)):
        base_value = (value + source.offset) * source.scale
        converted = (base_value / target.scale) - target.offset
        return converted.quantize(target_snapshot.precision, rounding=target_snapshot.rounding)


def validate_metric_unit(metric: str, source_unit: str, *, registry_version: str = REGISTRY_VERSION) -> str:
    definition = _registry(registry_version).get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    normalized = normalize_unit(source_unit, registry_version=registry_version)
    actual_dimension = registry_snapshot(registry_version).units[normalized].dimension
    if actual_dimension != definition.dimension:
        raise UnitDimensionMismatch(
            f"Metric {metric!r} requires dimension {definition.dimension}; unit {source_unit!r} is {actual_dimension}."
        )
    if definition.semantics is not None and normalized not in definition.semantics.allowed_source_units:
        raise UnitDimensionMismatch(f"Source unit {normalized!r} is not permitted for {metric!r}.")
    return normalized


def validate_metric_value(metric: str, value: Decimal, *, registry_version: str = REGISTRY_VERSION) -> None:
    definition = _registry(registry_version).get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    if definition.semantics is not None:
        definition.semantics.validate_value(value)


def validate_metric_rule(metric: str, rule_type: str, threshold: Decimal | None, unit: str,
                         *, registry_version: str = REGISTRY_VERSION) -> None:
    definition = _registry(registry_version).get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    validate_metric_unit(metric, unit, registry_version=registry_version)
    if definition.semantics is not None:
        if threshold is not None and not threshold.is_finite():
            raise ValueError("Threshold must be finite")
        canonical = (convert_value(threshold, unit, definition.canonical_unit, registry_version=registry_version)
                     if threshold is not None else None)
        definition.semantics.validate_rule(rule_type, canonical)


def convert_to_canonical(
    metric: str, value: Decimal, source_unit: str, *, source_scale: Decimal = Decimal("1"),
    registry_version: str = REGISTRY_VERSION,
) -> CanonicalValue:
    definition = _registry(registry_version).get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    normalized = validate_metric_unit(metric, source_unit, registry_version=registry_version)
    snapshot = registry_snapshot(registry_version)
    with localcontext(Context(prec=snapshot.decimal_precision, rounding=snapshot.rounding)):
        scaled = value * source_scale
    converted = convert_value(scaled, normalized, definition.canonical_unit, registry_version=registry_version)
    validate_metric_value(metric, converted, registry_version=registry_version)
    return CanonicalValue(
        value=converted,
        unit=definition.canonical_unit,
        raw_value=value,
        raw_unit=source_unit,
        source_scale=source_scale,
        registry_version=registry_version,
    )


def convert_to_presentation(
    metric: str, value: Decimal | float | int, source_unit: str | None = None, *,
    registry_version: str = REGISTRY_VERSION,
) -> PresentationValue:
    """Render a stored canonical value without mutating or reinterpreting its source provenance."""
    definition = _registry(registry_version).get(metric)
    if definition is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    unit = source_unit or definition.canonical_unit
    validate_metric_unit(metric, unit, registry_version=registry_version)
    return PresentationValue(
        value=convert_value(Decimal(str(value)), unit, definition.presentation_unit, registry_version=registry_version),
        unit=definition.presentation_unit,
    )


def convert_stored_canonical(
    metric: str, value: Decimal, unit: str, registry_version: str | None, *, target_registry_version: str = "1",
) -> Decimal:
    """Read an already canonical sample in a consumer's fixed snapshot.

    Legacy fixed-unit rows retain v1 interpretation. Never accept a free-text
    source unit here: these consumers require canonical stored values.
    """
    source = _registry(registry_version or "1").get(metric)
    target = _registry(target_registry_version).get(metric)
    if source is None or target is None:
        raise UnknownMetric(f"Unknown canonical metric: {metric!r}")
    if unit != source.canonical_unit:
        raise UnitDimensionMismatch("Stored reading unit is not canonical for its registry snapshot.")
    return convert_value(value, unit, target.canonical_unit,
                         registry_version=registry_version or "1", target_registry_version=target_registry_version)


def validate_catalog_candidate_unit(unit: str | None, dimension: str, *, registry_version: str = "1") -> str:
    """Executable handoff used when PR #109 applies a provenance-bearing extraction candidate."""
    if unit is None:
        raise UnknownUnit("Catalog extraction candidate has no unit.")
    normalized = normalize_unit(unit, registry_version=registry_version)
    actual_dimension = registry_snapshot(registry_version).units[normalized].dimension
    if actual_dimension != dimension:
        raise UnitDimensionMismatch(f"Catalog candidate requires dimension {dimension}; unit {unit!r} is {actual_dimension}.")
    return normalized


def convert_catalog_candidate(
    field_key: str, value: Decimal, unit: str | None, *, registry_version: str = "1",
) -> CanonicalValue:
    """#109 apply-boundary contract: validate an extracted field and retain its source provenance."""
    try:
        contract = CATALOG_FIELD_CONTRACTS[field_key]
    except KeyError as error:
        raise UnknownMetric(f"Catalog field has no unit contract: {field_key!r}") from error
    normalized = validate_catalog_candidate_unit(unit, contract.dimension, registry_version=registry_version)
    assert unit is not None
    return CanonicalValue(
        value=convert_value(value, normalized, contract.canonical_unit, registry_version=registry_version),
        unit=contract.canonical_unit,
        raw_value=value,
        raw_unit=unit,
        registry_version=registry_version,
    )
