"""G5 immutable v2 metric semantics.

Version 1 remains unchanged. New mappings may explicitly select v2; existing mappings remain version pinned.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType


class ValueKind(StrEnum):
    GAUGE = "gauge"
    BOUNDED_RATIO = "bounded_ratio"
    STATE = "state"
    COUNTER = "counter"


class ValueOutOfRange(ValueError):
    pass


@dataclass(frozen=True)
class MetricSemantics:
    value_kind: ValueKind
    min_canonical: Decimal | None
    max_canonical: Decimal | None
    allowed_source_units: tuple[str, ...]
    aggregations: tuple[str, ...] = ("avg", "min", "max", "last")
    rule_types: tuple[str, ...] = ("threshold_high", "threshold_low")

    def validate_value(self, value: Decimal) -> None:
        if not value.is_finite():
            raise ValueError("Nonfinite telemetry value")
        if ((self.min_canonical is not None and value < self.min_canonical)
                or (self.max_canonical is not None and value > self.max_canonical)
                or (self.value_kind is ValueKind.STATE and value not in (Decimal(0), Decimal(1)))):
            raise ValueOutOfRange("Value is outside the metric's versioned domain")

    def validate_rule(self, rule_type: str, canonical_threshold: Decimal | None) -> None:
        if rule_type not in self.rule_types:
            raise ValueError("Rule type is incompatible with this metric")
        if rule_type == "availability_unavailable":
            if canonical_threshold is not None:
                raise ValueError("State rule has no threshold")
        else:
            if canonical_threshold is None:
                raise ValueError("Threshold is required")
            self.validate_value(canonical_threshold)


def gauge(units: tuple[str, ...], minimum: str | None = "0") -> MetricSemantics:
    return MetricSemantics(ValueKind.GAUGE, Decimal(minimum) if minimum is not None else None, None, units)


_TEMPERATURE = gauge(("degC", "degF", "K"), "-273.15")
V2_SEMANTICS = MappingProxyType({
    "temperature_c": _TEMPERATURE,
    "supply_air_temperature_c": _TEMPERATURE,
    "return_air_temperature_c": _TEMPERATURE,
    "humidity_percent": MetricSemantics(ValueKind.BOUNDED_RATIO, Decimal(0), Decimal(100), ("%", "1")),
    "power_kw": gauge(("W", "kW")),
    "cooling_output_kw": gauge(("W", "kW")),
    "load_percent": gauge(("%", "1")),
    "availability": MetricSemantics(ValueKind.STATE, Decimal(0), Decimal(1), ("1", "%"),
                                    rule_types=("availability_unavailable",)),
    "airflow_m3_s": gauge(("m3/s", "m3/h", "L/s", "CFM")),
    "airflow_velocity_m_s": gauge(("m/s", "ft/min", "km/h")),
    "differential_pressure_pa": gauge(("Pa", "kPa", "inH2O"), None),
})
