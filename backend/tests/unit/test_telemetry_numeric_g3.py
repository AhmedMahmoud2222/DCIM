# ruff: noqa: UP031  (JSON literals are written with %-formatting so the tests control the exact numeral bytes)
"""Issue #128 / G3: the source-number rules and the lossless wire parser, without a database."""

import random
from decimal import Decimal

import pytest

from app.application.telemetry_service import exact_decimal
from app.domain.telemetry.numeric import (
    MAX_STORABLE,
    InvalidTelemetryValue,
    quantize_derived,
    quantize_source,
)
from app.domain.telemetry.registry import convert_to_canonical
from app.domain.telemetry.wire import WireNumberError, exact_number, loads_exact, prepare_records

D = Decimal


@pytest.mark.parametrize("raw,expected", [
    ("1.000000005", "1.00000000"),    # tie, preceding digit even -> down
    ("1.000000015", "1.00000002"),    # tie, preceding digit odd  -> up
    ("2.000000025", "2.00000002"),
    ("-1.000000005", "-1.00000000"),
    ("-1.000000015", "-1.00000002"),
    ("-2.000000025", "-2.00000002"),
    ("0.000000005", "0.00000000"),
    ("0.000000015", "0.00000002"),
    ("1.0000000050001", "1.00000001"),  # past the tie -> up
    ("1234567890.12345678", "1234567890.12345678"),
    ("0.1", "0.10000000"),
    ("1e-9", "0.00000000"),
    ("-0", "-0.00000000"),
    ("12345678901234567890e-10", "1234567890.12345679"),
])
def test_source_value_is_rounded_once_half_even(raw, expected):
    got = quantize_source(D(raw))
    assert got == D(expected)
    assert got.as_tuple().exponent == -8  # exactly the 8-decimal storage contract
    assert format(got, "f") == expected


def test_the_documented_extremes_are_accepted_exactly():
    assert quantize_source(D("9999999999.99999999")) == MAX_STORABLE
    assert quantize_source(D("-9999999999.99999999")) == -MAX_STORABLE


@pytest.mark.parametrize("raw", [
    "9999999999.999999994",   # rounds DOWN into range, but the original is above the documented maximum
    "9999999999.999999995",
    "10000000000",
    "-9999999999.999999994",
    "1e10",
    "1E+400",
    "1e-101",                 # pathological exponent
    "1e101",
    "NaN", "Infinity", "-Infinity",
])
def test_out_of_range_originals_are_rejected_even_when_rounding_would_pull_them_in(raw):
    with pytest.raises(InvalidTelemetryValue):
        quantize_source(D(raw))


def test_derived_values_use_the_same_rounding_after_the_range_check():
    assert quantize_derived(D("1.000000005"), "scaled value") == D("1.00000000")
    with pytest.raises(InvalidTelemetryValue):
        quantize_derived(D("10000000000"), "scaled value")


def test_exact_decimal_never_goes_through_a_float_for_text_and_integers():
    assert exact_decimal("1234567890.12345678") == D("1234567890.12345678")
    assert exact_decimal(12345678901234567890) == D(12345678901234567890)
    assert exact_decimal(0.1) == D("0.1")  # a float is taken at its shortest repr: documented limitation
    with pytest.raises(InvalidTelemetryValue):
        exact_decimal(True)
    with pytest.raises(InvalidTelemetryValue):
        exact_decimal("abc")


def _record(value_json: str) -> dict:
    document, lexemes = prepare_records(loads_exact(('{"records":[{"value":%s}]}' % value_json).encode()))
    return {"value": document["records"][0]["value"], "lexeme": lexemes.get(0)}


@pytest.mark.parametrize("literal,value,lexeme", [
    ("1234567890.12345678", D("1234567890.12345678"), "1234567890.12345678"),
    ("9999999999.99999999", D("9999999999.99999999"), "9999999999.99999999"),
    ("12345678901234567890", D(12345678901234567890), "12345678901234567890"),
    ("-0", D("-0"), "-0"),
    ("-0.0", D("-0.0"), "-0.0"),
    ("1E+3", D("1000"), "1E+3"),
    ("1.50", D("1.50"), "1.50"),
    ('"0.30000000000000004"', D("0.30000000000000004"), "0.30000000000000004"),
    ('"+.5"', D("0.5"), "+.5"),
])
def test_wire_numbers_keep_every_digit_and_their_exact_text(literal, value, lexeme):
    got = _record(literal)
    assert got["value"] == value and got["lexeme"] == lexeme
    assert got["value"].is_signed() == value.is_signed()


@pytest.mark.parametrize("literal", ["true", "false", "null", '"abc"', '""', '"0x10"', '"1_0"', '" 1"', "[1]", "{}",
                                      '"' + "1" * 65 + '"', "1." + "2" * 64])
def test_malformed_values_are_structural_errors(literal):
    with pytest.raises(WireNumberError):
        _record(literal) if literal != "null" else exact_number(None)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", '"NaN"', '"inf"', '"-Infinity"'])
def test_non_finite_values_pass_through_as_non_finite_for_a_per_record_rejection(literal):
    assert not _record(literal)["value"].is_finite()


def test_bad_json_and_pathological_exponents_are_refused_without_arithmetic():
    for body in (b"", b"{", b'{"records":[{"value":1e99999999999999999999}]}', b'{"records":[{"value":"1e99999999999999999999"}]}'):
        try:
            record = prepare_records(loads_exact(body))[0]["records"][0]["value"]
        except (WireNumberError, TypeError, KeyError):
            continue
        with pytest.raises(InvalidTelemetryValue):
            quantize_source(record)


def test_non_finite_numbers_inside_attributes_are_refused():
    with pytest.raises(WireNumberError):
        prepare_records(loads_exact(b'{"records":[{"value":1,"attributes":{"x":NaN}}]}'))
    document, _ = prepare_records(loads_exact(b'{"records":[{"value":1,"attributes":{"x":1.5,"y":7,"z":[2.5]}}]}'))
    assert document["records"][0]["attributes"] == {"x": 1.5, "y": 7, "z": [2.5]}


def test_canonical_is_reproducible_from_the_stored_raw_for_supported_values():
    """The G3 invariant: canonical == convert(stored raw, unit, scale). 4,000 random inputs across the registry."""
    rng = random.Random(128)
    cases = [("power_kw", "kW"), ("power_kw", "W"), ("temperature_c", "degF"), ("temperature_c", "K"),
             ("humidity_percent", "%"), ("availability", "1"), ("airflow_m3_s", "CFM"), ("differential_pressure_pa", "inH2O")]
    for _ in range(4000):
        metric, unit = rng.choice(cases)
        digits = rng.randint(1, 9)
        decimals = rng.randint(0, 12)
        raw = D(rng.randint(-10**digits, 10**digits)).scaleb(-decimals)
        scale = rng.choice([D(1), D("0.1"), D("10"), D("0.001"), D("2.5")])
        stored_raw = quantize_source(raw)
        canonical = convert_to_canonical(metric, stored_raw, unit, source_scale=scale).value
        # recomputation from ONLY persisted data (raw_value, raw_unit, source_scale)
        assert convert_to_canonical(metric, D(str(stored_raw)), unit, source_scale=scale).value == canonical
