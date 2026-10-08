"""Storable numeric range for telemetry values.

`telemetry_reading.value`/`raw_value`/`source_scale` are PostgreSQL ``NUMERIC(18, 8)``:
at most 18 significant digits, 8 of them fractional, so 10 integer digits. The permitted
magnitude is therefore ``|x| <= 9999999999.99999999`` after rounding to 8 decimals
(PostgreSQL rounds half away from zero). NaN and +/-Infinity are never storable.

The ingestion boundary validates against this range itself; a PostgreSQL
"numeric field overflow" must never be the validation mechanism.
"""

from decimal import ROUND_HALF_UP, Decimal

NUMERIC_PRECISION = 18
NUMERIC_SCALE = 8
_QUANTUM = Decimal(1).scaleb(-NUMERIC_SCALE)
# Exclusive bound: 10**(precision - scale). MAX_STORABLE is the largest storable magnitude.
_LIMIT = Decimal(10) ** (NUMERIC_PRECISION - NUMERIC_SCALE)
MAX_STORABLE = _LIMIT - _QUANTUM


class InvalidTelemetryValue(ValueError):
    """A telemetry number cannot be represented in the persisted NUMERIC(18, 8) columns."""


def ensure_storable(value: Decimal, field: str) -> Decimal:
    """Return ``value`` unchanged when finite and storable in NUMERIC(18, 8), else raise.

    ``field`` names the quantity (raw value, scaled value, canonical value) for the
    message only; messages never echo the offending number."""
    if not value.is_finite():
        raise InvalidTelemetryValue(f"Telemetry {field} must be a finite number.")
    # Compare before quantizing: quantize() itself raises on magnitudes beyond the
    # decimal context precision.
    if abs(value) >= _LIMIT:
        raise InvalidTelemetryValue(f"Telemetry {field} is outside the storable range of +/-{MAX_STORABLE}.")
    if abs(value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)) >= _LIMIT:
        raise InvalidTelemetryValue(f"Telemetry {field} is outside the storable range of +/-{MAX_STORABLE}.")
    return value
