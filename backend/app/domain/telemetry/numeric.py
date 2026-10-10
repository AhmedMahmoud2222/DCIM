"""Storable numeric range for telemetry values.

`telemetry_reading.value`/`raw_value`/`source_scale` are PostgreSQL ``NUMERIC(18, 8)``:
at most 18 significant digits, 8 of them fractional, so 10 integer digits. The permitted
magnitude is therefore ``|x| <= 9999999999.99999999`` after rounding to 8 decimals
(PostgreSQL rounds half away from zero). NaN and +/-Infinity are never storable.

The ingestion boundary validates against this range itself; a PostgreSQL
"numeric field overflow" must never be the validation mechanism.
"""

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

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


# Issue #128 / G3: a source value's decimal exponent is bounded before any arithmetic. The storable magnitude is
# below 10**10 and the resolution is 10**-8, so anything beyond 10**+/-100 is a malformed or hostile numeral, not data.
MAX_ABS_ADJUSTED_EXPONENT = 100
# The longest numeric lexeme accepted on the wire and kept as `raw_value_text` (and in a hold's `value_text`).
MAX_LEXEME_CHARS = 64


def quantize_source(value: Decimal, field: str = "source value") -> Decimal:
    """The single place a source value is rounded to the persisted 8-decimal contract (ROUND_HALF_EVEN).

    Rounding happens here, in application code, never in PostgreSQL (which would round ties half away from zero), so
    the stored raw value is exactly what every later conversion starts from. The ORIGINAL value must lie inside the
    documented source range; a value just above the maximum is rejected even when rounding would pull it back in.
    Digits beyond the eighth decimal are rounded, not refused; the exact text is kept separately as `raw_value_text`.
    """
    if not value.is_finite():
        raise InvalidTelemetryValue(f"Telemetry {field} must be a finite number.")
    if abs(value.adjusted()) > MAX_ABS_ADJUSTED_EXPONENT:
        raise InvalidTelemetryValue(f"Telemetry {field} has an unsupported exponent.")
    if abs(value) > MAX_STORABLE:
        raise InvalidTelemetryValue(f"Telemetry {field} is outside the storable range of +/-{MAX_STORABLE}.")
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def quantize_derived(value: Decimal, field: str) -> Decimal:
    """Round a derived quantity (a scaled legacy value) to the storage contract after the usual range check."""
    ensure_storable(value, field)
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
