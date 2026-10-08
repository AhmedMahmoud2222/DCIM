"""Pure-Python proof of the NUMERIC(18, 8) validation boundary (no database)."""

from decimal import Decimal

import pytest

from app.domain.telemetry.numeric import MAX_STORABLE, InvalidTelemetryValue, ensure_storable


def test_documented_range_matches_numeric_18_8():
    assert MAX_STORABLE == Decimal("9999999999.99999999")


@pytest.mark.parametrize("value", [
    Decimal("0"), Decimal("-0.000000001"), Decimal("1E-30"), Decimal("9999999999.99999999"), Decimal("-9999999999.99999999"),
    # Rounds to the maximum (not past it) at 8 decimals.
    Decimal("9999999999.999999994"),
])
def test_storable_values_pass_unchanged(value):
    assert ensure_storable(value, "x") is value


@pytest.mark.parametrize("value", [
    Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"),
    Decimal("10000000000"), Decimal("-10000000000"), Decimal("1E300"), Decimal("-1E300"), Decimal("1E+999999"),
    # Rounds up to 10**10 at 8 decimals, which PostgreSQL would reject as an overflow.
    Decimal("9999999999.999999995"), Decimal("-9999999999.999999995"),
])
def test_unstorable_values_raise(value):
    with pytest.raises(InvalidTelemetryValue):
        ensure_storable(value, "x")
