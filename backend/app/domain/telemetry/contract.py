"""Deterministic identity of the source-to-canonical conversion a mapping revision pins (Issue #128 / G1).

`conversion_contract_text` is the canonical encoding and `conversion_contract_hash` is its SHA-256. The text is
a fixed sequence of `key=value` lines (LF separated, UTF-8, no trailing newline). Every number is rendered as a
plain, non-exponent Decimal with trailing zeros removed, so the same inputs always yield the same bytes.

Inputs, in order:
  format           literal `dcim.mapping-conversion.v1`; a change of this encoding must bump it
  metric           canonical metric key
  registry_version registry version string, or `legacy` for a pre-registry mapping
  source_unit      the source unit text exactly as stored on the revision
  source_scale     the per-mapping multiplier, 8 decimal places (the NUMERIC(18, 8) storage scale)
  and, for a registry-versioned mapping only, the *resolved* physical definitions that decide the result:
  source_symbol, source_dimension, source_unit_scale, source_unit_offset,
  canonical_unit, canonical_dimension, canonical_unit_scale, canonical_unit_offset,
  precision, rounding

A legacy mapping (no registry version) stores `source_value * source_scale` in the source unit and performs
no unit conversion; its encoding therefore omits the resolved definitions.

The hash detects drift: it is stamped when the revision is created and recomputed from the *current* registry at
ingest. A changed unit factor, offset, canonical unit, precision or rounding mode yields a different hash, and
ingest refuses the record instead of converting it under a different meaning. The hash does not replace
immutable conversion definitions (Issue #128 / G10): it only makes a silent change observable.
"""

import hashlib
from decimal import Decimal

from app.domain.telemetry import registry

FORMAT = "dcim.mapping-conversion.v1"
LEGACY_VERSION = "legacy"
SCALE_QUANTUM = Decimal("0.00000001")
ROUNDING = "ROUND_HALF_EVEN"


def _number(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def conversion_contract_text(
    metric: str, source_unit: str, source_scale: Decimal, registry_version: str | None
) -> str:
    lines = [
        FORMAT,
        f"metric={metric}",
        f"registry_version={registry_version if registry_version is not None else LEGACY_VERSION}",
        f"source_unit={source_unit}",
        f"source_scale={_number(Decimal(source_scale).quantize(SCALE_QUANTUM))}",
    ]
    if registry_version is not None:
        definition = registry._registry(registry_version).get(metric)
        if definition is None:
            raise registry.UnknownMetric(f"Unknown canonical metric: {metric!r}")
        source = registry.UNITS[registry.validate_metric_unit(metric, source_unit, registry_version=registry_version)]
        canonical = registry.UNITS[registry.normalize_unit(definition.canonical_unit)]
        lines += [
            f"source_symbol={source.symbol}",
            f"source_dimension={source.dimension}",
            f"source_unit_scale={_number(source.scale)}",
            f"source_unit_offset={_number(source.offset)}",
            f"canonical_unit={canonical.symbol}",
            f"canonical_dimension={canonical.dimension}",
            f"canonical_unit_scale={_number(canonical.scale)}",
            f"canonical_unit_offset={_number(canonical.offset)}",
            f"precision={_number(registry.PRECISION)}",
            f"rounding={ROUNDING}",
        ]
    return "\n".join(lines)


def conversion_contract_hash(
    metric: str, source_unit: str, source_scale: Decimal, registry_version: str | None
) -> str:
    text = conversion_contract_text(metric, source_unit, source_scale, registry_version)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def conversion_contract_hash_or_unresolved(
    metric: str, source_unit: str, source_scale: Decimal, registry_version: str | None
) -> str:
    """Hash for the migration seed: a mapping whose unit no longer resolves still gets a revision.

    The marker hash can never equal a recomputed one, so ingest through that revision is refused as drift
    rather than guessing a conversion.
    """
    try:
        return conversion_contract_hash(metric, source_unit, source_scale, registry_version)
    except (registry.UnknownMetric, registry.UnknownUnit, registry.UnitDimensionMismatch, registry.UnknownRegistryVersion):
        return hashlib.sha256(f"{FORMAT}\nunresolved\n{metric}\n{source_unit}".encode()).hexdigest()
