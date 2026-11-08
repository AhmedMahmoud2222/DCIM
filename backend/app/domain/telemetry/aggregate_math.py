"""Exact daily-aggregate arithmetic (Issue #128 / G4-A).

Every stored telemetry value is a ``NUMERIC(18, 8)``: an exact integer multiple of 1e-8. Daily aggregates therefore sum
those values as Python integers in units of 1e-8, which is exact, unbounded and independent of the process-wide
``decimal`` context. The stored average is derived from the exact sum and count with one half-even division, so the
result for a set of samples never depends on how they were batched, ordered or merged.

``arithmetic_basis`` on ``daily_telemetry_aggregate``:

* ``NULL``            computed before G4-A with float arithmetic; ``sum_value`` is NULL; precision unverified.
* ``decimal_exact``   built only from stored ``Decimal`` samples; ``sum_value`` is the exact sum.
* ``legacy_seeded``   a legacy row that received new samples. ``sum_value`` was seeded as ``average * sample_count``
                      from the old rounded average (a fixed, labelled uncertainty; no numeric bound is claimed) and every
                      later sample is added exactly. ``legacy_sample_count`` is the count that existed at seeding time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SCALE = 8
UNIT = 10**SCALE
# NUMERIC(38, 8): 38 significant digits in total.
MAX_SUM_UNITS = 10**38 - 1
# daily_telemetry_aggregate.sample_count is a PostgreSQL INTEGER.
MAX_COUNT = 2**31 - 1

BASIS_EXACT = "decimal_exact"
BASIS_LEGACY_SEEDED = "legacy_seeded"
BASES = (BASIS_EXACT, BASIS_LEGACY_SEEDED)


class AggregateArithmeticError(ValueError):
    """An aggregate cannot be represented exactly (or a stored value breaks the NUMERIC(18, 8) contract)."""


def to_units(value: Decimal | int) -> int:
    """Exact integer count of 1e-8 units in ``value``; refuses anything that has more than eight decimals.

    Works on the decimal tuple with integer arithmetic only: ``Decimal`` operations such as ``scaleb`` round to the
    ambient context precision, which would make the result depend on process-wide state.
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise AggregateArithmeticError("aggregate values must be Decimal, never float")
    if isinstance(value, int):
        return value * UNIT
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # NaN / Infinity
        raise AggregateArithmeticError("aggregate value is not a finite multiple of 1e-8")
    coefficient = int("".join(map(str, digits))) if digits else 0
    shift = exponent + SCALE
    if shift >= 0:
        units = coefficient * 10**shift
    else:
        units, remainder = divmod(coefficient, 10**-shift)
        if remainder:
            raise AggregateArithmeticError("aggregate value is not a finite multiple of 1e-8")
    return -units if sign else units


def from_units(units: int) -> Decimal:
    """The ``NUMERIC(.., 8)`` Decimal for an integer count of 1e-8 units (always scale 8), built without context rounding."""
    return Decimal((1 if units < 0 else 0, tuple(int(char) for char in str(abs(units))), -SCALE))


def div_half_even(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded half-even to an integer. Symmetric about zero; exact for any size."""
    if denominator <= 0:
        raise AggregateArithmeticError("denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient & 1):
        quotient += 1
    return -quotient if numerator < 0 else quotient


@dataclass(frozen=True)
class AggregateState:
    """Exact aggregate of a set of samples."""

    sum_units: int
    count: int
    minimum: Decimal
    maximum: Decimal

    @property
    def average(self) -> Decimal:
        return from_units(div_half_even(self.sum_units, self.count))

    @property
    def sum_value(self) -> Decimal:
        return from_units(self.sum_units)


def _checked(sum_units: int, count: int) -> None:
    if count > MAX_COUNT:
        raise AggregateArithmeticError("daily aggregate sample_count would exceed the INTEGER column")
    if abs(sum_units) > MAX_SUM_UNITS:
        raise AggregateArithmeticError("daily aggregate sum would exceed NUMERIC(38, 8)")


def state_of(values: list[Decimal]) -> AggregateState:
    """Exact state of one non-empty group of stored samples."""
    if not values:
        raise AggregateArithmeticError("an aggregate needs at least one sample")
    total = sum(to_units(value) for value in values)
    _checked(total, len(values))
    return AggregateState(total, len(values), min(values), max(values))


def fold(base: AggregateState, extra: AggregateState) -> AggregateState:
    """Exact union of two disjoint sample sets. Associative and commutative."""
    total = base.sum_units + extra.sum_units
    count = base.count + extra.count
    _checked(total, count)
    return AggregateState(total, count, min(base.minimum, extra.minimum), max(base.maximum, extra.maximum))
