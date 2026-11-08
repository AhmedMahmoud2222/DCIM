"""Issue #128 / G4-A: exact, context-independent, sign-safe aggregate arithmetic."""

import random
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

import pytest

from app.domain.telemetry.aggregate_math import (
    MAX_COUNT,
    MAX_SUM_UNITS,
    AggregateArithmeticError,
    AggregateState,
    div_half_even,
    fold,
    from_units,
    state_of,
    to_units,
)

D = Decimal


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (1, 2, 0), (3, 2, 2), (5, 2, 2), (7, 2, 4),      # ties go to the even neighbour
        (-1, 2, 0), (-3, 2, -2), (-5, 2, -2), (-7, 2, -4),  # symmetric about zero
        (1, 3, 0), (2, 3, 1), (-1, 3, 0), (-2, 3, -1),
        (0, 5, 0), (10, 5, 2), (-10, 5, -2),
        (10**40 + 1, 2, 5 * 10**39), (10**40 + 3, 2, 5 * 10**39 + 2),  # beyond any float or default Decimal context
    ],
)
def test_division_is_half_even_and_sign_symmetric(numerator, denominator, expected):
    assert div_half_even(numerator, denominator) == expected


def test_division_rejects_a_non_positive_denominator():
    with pytest.raises(AggregateArithmeticError):
        div_half_even(1, 0)
    with pytest.raises(AggregateArithmeticError):
        div_half_even(1, -3)


@pytest.mark.parametrize("bad", [1.5, True, "1", None, D("0.000000001"), D("NaN"), D("Infinity")])
def test_to_units_refuses_floats_booleans_and_values_finer_than_1e_8(bad):
    with pytest.raises(AggregateArithmeticError):
        to_units(bad)


def test_units_round_trip_is_exact_with_scale_eight():
    for text in ("0", "-0.00000001", "9999999999.99999999", "-9999999999.99999999", "1.5", "123456789.12345678"):
        value = D(text)
        assert from_units(to_units(value)) == value
        assert from_units(to_units(value)).as_tuple().exponent == -8
    assert to_units(D("1E+3")) == 1000 * 10**8  # exponent notation is still an exact multiple


def test_arithmetic_ignores_the_process_wide_decimal_context():
    values = [D("9999999999.12345678")] * 3
    with localcontext() as context:
        context.prec = 6
        context.rounding = "ROUND_DOWN"
        state = state_of(values)
    assert state.average == D("9999999999.12345678")
    assert state.sum_value == D("29999999997.37037034")


def test_issue_case_a_identical_large_values_keep_average_min_and_max_exactly():
    state = state_of([D("9999999999.12345678")] * 3)
    assert (state.average, state.minimum, state.maximum) == (D("9999999999.12345678"),) * 3


def test_issue_case_f_a_tie_in_the_mean_does_not_inflate_later_sums():
    # 0 and 1e-8: the mean is a half-unit tie. The exact sum stays 1e-8 however many zeros follow.
    state = state_of([D("0"), D("0.00000001")])
    assert state.sum_units == 1 and state.average == D("0.00000000")  # tie to even
    for _ in range(10):
        state = fold(state, state_of([D("0")]))
    assert state.sum_units == 1 and state.count == 12 and state.average == D("0.00000000")


def test_negative_and_mixed_sign_days():
    state = state_of([D("-3"), D("-4")])
    assert state.average == D("-3.50000000")
    assert state_of([D("-1"), D("1")]).average == D("0.00000000")
    assert state_of([D("-0.00000001"), D("0")]).average == D("0.00000000")           # -0.5 unit ties to 0
    assert state_of([D("-0.00000003"), D("0")]).average == D("-0.00000002")          # -1.5 units ties to -2
    assert state_of([D("-0.00000005"), D("0")]).average == D("-0.00000002")          # -2.5 units ties to -2
    assert state_of([D("-0.00000007"), D("0")]).average == D("-0.00000004")          # -3.5 units ties to -4


def test_min_and_max_are_compared_as_decimals():
    state = state_of([D("1.00000001"), D("1.00000002"), D("0.99999999")])
    assert state.minimum == D("0.99999999") and state.maximum == D("1.00000002")


def test_an_empty_group_is_refused():
    with pytest.raises(AggregateArithmeticError):
        state_of([])


def test_sum_and_count_limits_fail_loudly_and_without_wrapping():
    huge = AggregateState(MAX_SUM_UNITS, 1, D("1"), D("1"))
    with pytest.raises(AggregateArithmeticError, match="NUMERIC"):
        fold(huge, state_of([D("0.00000001")]))
    full = AggregateState(0, MAX_COUNT, D("0"), D("0"))
    with pytest.raises(AggregateArithmeticError, match="INTEGER"):
        fold(full, state_of([D("0")]))
    fold(AggregateState(0, MAX_COUNT - 1, D("0"), D("0")), state_of([D("0")]))  # exactly at the limit is allowed
    # the documented column maximum times a realistic count still fits comfortably
    state_of([D("9999999999.99999999")] * 1000)


def _reference_average(values):
    with localcontext() as context:
        context.prec = 80
        return (sum(values) / len(values)).quantize(D("0.00000001"), rounding=ROUND_HALF_EVEN)


def test_property_average_equals_the_exact_reference_for_any_partition_and_order():
    rng = random.Random(128)
    for _ in range(300):
        size = rng.randint(1, 60)
        magnitude = rng.choice([1, 10**3, 10**8, 10**17])  # up to ~1e9 canonical units
        values = [from_units(rng.randint(-magnitude, magnitude)) for _ in range(size)]
        expected_average = _reference_average(values)
        expected_sum = sum(values)
        whole = state_of(values)
        assert (whole.average, whole.sum_value, whole.count, whole.minimum, whole.maximum) == (
            expected_average, expected_sum, size, min(values), max(values))
        shuffled = values[:]
        rng.shuffle(shuffled)
        cut_a, cut_b = sorted(rng.sample(range(size + 1), 2))
        parts = [part for part in (shuffled[:cut_a], shuffled[cut_a:cut_b], shuffled[cut_b:]) if part]
        folded = state_of(parts[0])
        for part in parts[1:]:
            folded = fold(folded, state_of(part))
        assert folded == whole  # identical state, hence identical average, whatever the batching or order
        late = state_of(parts[0])
        for value in [v for part in parts[1:] for v in part]:
            late = fold(late, state_of([value]))  # one late merge per sample
        assert late == whole

