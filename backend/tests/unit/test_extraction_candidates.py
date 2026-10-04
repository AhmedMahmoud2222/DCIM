"""The candidate parser: what it reads, what it refuses to guess, and how it bounds hostile text."""

import time

import pytest

from app.application.catalog_documents.extraction import candidates as c
from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages


def run(text: str, names: list[str], *, page: int = 1, method: str = "native", confidence: float | None = None):
    return analyze_pages([PageText(page, text, method, confidence)], names)


def by_field(result, key: str, match: str | None = None):
    return [x for x in result.candidates if x.field_key == key and (match is None or x.model_match == match)]


SINGLE = """CX-100 Technical Specifications
Typical power consumption: 350 W
Maximum power: 1,200 W
Rated power: 800 W
Idle power  120 W
Power supply: 2 x 750 W redundant
Heat dissipation: 1195 BTU/hr
Airflow: 85 CFM
Airflow direction: front-to-back
Dimensions (H x W x D): 44 x 440 x 600 mm
Weight: 12.5 kg
Shipping weight: 18 kg
Height (U): 1U
Input voltage: 100-240 V AC
Frequency 50/60 Hz
Rated current: 6.5 A
Operating temperature: 10-35 °C
Operating humidity: 8-80 %
"""


def test_power_variants_stay_separate_fields_even_with_the_same_unit():
    result = run(SINGLE, ["CX-100"])
    values = {x.field_key: (x.value_numeric, x.unit) for x in result.candidates}
    assert values["power_typical_w"] == (350.0, "W")
    assert values["power_max_w"] == (1200.0, "W")
    assert values["power_rated_w"] == (800.0, "W")
    assert values["power_idle_w"] == (120.0, "W")
    assert values["psu_capacity_w"] == (750.0, "W")
    assert values["psu_quantity"] == (2.0, None)
    assert values["heat_dissipation"] == (1195.0, "BTU/hr")


def test_every_candidate_keeps_raw_value_raw_unit_exact_text_and_page():
    result = run(SINGLE, ["CX-100"], page=3)
    max_power = by_field(result, "power_max_w")[0]
    assert (max_power.raw_value, max_power.raw_unit) == ("1,200", "W")
    assert max_power.source_text == "Maximum power: 1,200 W"
    assert max_power.page_number == 3
    assert max_power.method == "native"
    assert 0 < max_power.confidence <= 1


def test_dimensions_with_stated_axis_order_are_split_and_without_it_are_not_guessed():
    ordered = run("CX-100 Technical Specifications\nDimensions (W x D x H): 440 x 600 x 44 mm", ["CX-100"])
    assert {x.field_key: x.value_numeric for x in ordered.candidates} == {"width": 440.0, "depth": 600.0, "height": 44.0}
    unordered = run("CX-100 Technical Specifications\nDimensions: 44 x 440 x 600 mm", ["CX-100"])
    assert [x.field_key for x in unordered.candidates] == ["dimensions_unordered"]
    only = unordered.candidates[0]
    assert only.value_numeric is None and only.value_text == "44 x 440 x 600" and "axis_order_unspecified" in only.flags


def test_units_are_never_converted_or_invented():
    result = run(
        "CX-100 Technical Specifications\nTypical power: 1.2 kW\nWeight: 25 lb\nOperating temperature: 50-95 °F\n"
        "Maximum power: 300\nWeight 3 stone\nHeat dissipation: 100 kg",
        ["CX-100"],
    )
    typical = by_field(result, "power_typical_w")[0]
    assert (typical.value_numeric, typical.unit) == (1.2, "kW")  # not 1200 W
    assert (by_field(result, "weight")[0].value_numeric, by_field(result, "weight")[0].unit) == (25.0, "lb")
    temperature = by_field(result, "operating_temperature")[0]
    assert (temperature.value_numeric, temperature.value_max, temperature.unit) == (50.0, 95.0, "°F")
    missing = by_field(result, "power_max_w")[0]
    assert missing.unit is None and "unit_missing" in missing.flags and missing.raw_unit == ""
    stone = by_field(result, "weight")[1]
    assert stone.unit is None and stone.raw_unit == "stone" and "unit_unrecognized" in stone.flags
    wrong = by_field(result, "heat_dissipation")[0]
    assert wrong.unit is None and "unit_dimension_mismatch" in wrong.flags


def test_unlabelled_power_and_unsupported_semantics():
    result = run("CX-100 Technical Specifications\nPower consumption: 100 W\nStandby power: 5 W\nPeak inrush: 90 A", ["CX-100"])
    assert [x.field_key for x in result.candidates] == ["power_unspecified_w"]  # standby / inrush are not guessed
    assert "semantics_unspecified" in result.candidates[0].flags


def test_slash_pair_label_maps_values_by_position_and_a_mismatch_yields_nothing():
    ok = run("CX-100 Technical Specifications\nTypical / Maximum power: 350 / 500 W", ["CX-100"])
    assert {x.field_key: x.value_numeric for x in ok.candidates} == {"power_typical_w": 350.0, "power_max_w": 500.0}
    assert all(x.unit == "W" for x in ok.candidates)
    bad = run("CX-100 Technical Specifications\nTypical / Maximum power: 350 W", ["CX-100"])
    assert bad.candidates == []


@pytest.mark.parametrize(
    ("token", "value", "flag"),
    [("1,200", 1200.0, None), ("1,5", 1.5, "decimal_comma"), ("1.200", 1.2, "number_format_ambiguous"), ("12.5", 12.5, None)],
)
def test_number_formats_flag_what_a_reader_in_another_locale_would_misread(token, value, flag):
    parsed, flags = c.parse_number(token)
    assert parsed == value
    assert (flag in flags) if flag else not flags


def test_absurd_numbers_are_dropped_not_stored():
    assert c.parse_number("9" * 400)[0] is None
    assert run("CX-100 Technical Specifications\nWeight: " + "9" * 300 + " kg", ["CX-100"]).candidates == []


# ------------------------------------------------------------------------------ multi-model evidence

TABLE = """Specification      CX-100     CX-200     CX-300
Typical power      350 W      520 W      700 W
Maximum power      500 W      800 W      1,100 W
Weight             12 kg      15 kg      20 kg
"""


def test_multi_model_table_assigns_each_value_to_its_own_column_only():
    result = run(TABLE, ["CX-200"])
    assert result.model_resolution == "multi_model_matched"
    assert result.identified_models == ["CX-100", "CX-200", "CX-300"]
    target = {x.field_key: x.value_numeric for x in result.candidates if x.model_match == "target"}
    assert target == {"power_typical_w": 520.0, "power_max_w": 800.0, "weight": 15.0}
    other = [x for x in result.candidates if x.model_match == "other"]
    assert {x.model_context for x in other} == {"CX-100", "CX-300"}
    assert all(x.method == "table" and "model_context_from_table_column" in x.flags for x in result.candidates)
    # a neighbour's number is never presented as the target's
    assert 350.0 not in [x.value_numeric for x in result.candidates if x.model_match == "target"]


def test_multi_model_sections_by_heading():
    text = "CX-100 Technical Specifications\nTypical power: 350 W\nCX-200 Technical Specifications\nTypical power: 520 W\n"
    result = run(text, ["CX-200"])
    assert [(x.model_match, x.value_numeric) for x in result.candidates] == [("other", 350.0), ("target", 520.0)]


def test_target_not_found_requires_review_and_never_attributes():
    result = run(TABLE, ["ZZ-900"])
    assert result.model_resolution == "target_not_found"
    assert all(x.model_match in {"other", "unattributed"} and x.model_match != "target" for x in result.candidates)


def test_no_model_evidence_leaves_every_value_unattributed():
    result = run("Typical power: 350 W\nWeight: 12 kg", ["CX-100"])
    assert result.model_resolution == "no_model_evidence"
    assert {x.model_match for x in result.candidates} == {"unattributed"}
    assert all("no_model_evidence" in x.flags for x in result.candidates)


def test_a_value_before_any_model_context_in_a_multi_model_document_is_unattributed():
    result = run("Typical power: 111 W\n" + TABLE, ["CX-200"])
    loose = [x for x in result.candidates if x.value_numeric == 111.0]
    assert loose and loose[0].model_match == "unattributed"
    assert "no_model_context_in_multi_model_document" in loose[0].flags


def test_ambiguous_target_requires_review():
    text = "X300 Technical Specifications\nTypical power: 100 W\nX300-S Technical Specifications\nTypical power: 90 W\n"
    result = run(text, ["X300 / X300-S"])
    assert result.model_resolution == "ambiguous_target"
    assert {x.model_match for x in result.candidates} == {"unattributed"}


def test_model_words_are_matched_exactly_not_by_prefix():
    # CX-1000 is a different model than CX-100.
    result = run("CX-1000 Technical Specifications\nTypical power: 999 W", ["CX-100"])
    assert result.model_resolution == "target_not_found"
    assert by_field(result, "power_typical_w")[0].model_match == "other"


# ------------------------------------------------------------------------------ conflicts and OCR


def test_conflicting_values_are_flagged_grouped_and_never_resolved():
    text = "CX-100 Technical Specifications\nTypical power: 350 W\nTypical power: 380 W\nWeight: 12 kg\n"
    result = run(text, ["CX-100"])
    power = by_field(result, "power_typical_w")
    assert len(power) == 2 and all("conflict" in x.flags for x in power)
    assert power[0].conflict_group_key == power[1].conflict_group_key is not None
    assert "conflict" not in by_field(result, "weight")[0].flags


def test_the_same_value_repeated_on_two_pages_is_evidence_not_a_conflict():
    pages = [PageText(1, "CX-100 Technical Specifications\nWeight: 12 kg", "native"), PageText(2, "Weight: 12 kg", "native")]
    result = analyze_pages(pages, ["CX-100"])
    weights = by_field(result, "weight")
    assert [w.page_number for w in weights] == [1, 2] and all("conflict" not in w.flags for w in weights)


def test_a_different_unit_for_the_same_quantity_is_still_a_conflict():
    result = run("CX-100 Technical Specifications\nWeight: 1 kg\nWeight: 2.2 lb", ["CX-100"])
    assert all("conflict" in x.flags for x in by_field(result, "weight"))


def test_ocr_text_is_tagged_and_scaled_by_recognition_confidence():
    high = run("CX-100 Technical Specifications\nWeight: 12 kg", ["CX-100"], method="ocr", confidence=95.0)
    low = run("CX-100 Technical Specifications\nWeight: 12 kg", ["CX-100"], method="ocr", confidence=40.0)
    assert high.candidates[0].method == "ocr" and low.candidates[0].method == "ocr"
    assert high.candidates[0].confidence > low.candidates[0].confidence
    assert "ocr_low_confidence" in low.candidates[0].flags and "ocr_low_confidence" not in high.candidates[0].flags


# ------------------------------------------------------------------------------ hostile text bounds


def test_overlong_lines_and_pathological_tables_are_bounded_and_fast():
    pathological = [
        "9" * 100_000,
        ("9," * 400) + " W",
        "Typical power: " + "1" * 5000 + " W",
        "  ".join(f"CX-{i}" for i in range(3000)),
        "\n".join(f"Typical power   {'  '.join(str(i) + ' W' for i in range(40))}" for _ in range(2000)),
        "(" * 20000,
        "a" * 50000 + ":" + " " * 50000,
    ]
    started = time.monotonic()
    result = analyze_pages([PageText(1, "\n".join(pathological), "native")], ["CX-100"])
    assert time.monotonic() - started < 10
    assert len(result.candidates) <= c.MAX_CANDIDATES
    assert len(result.identified_models) <= c.MAX_IDENTIFIED_MODELS
    assert any(w["code"] == "lines_too_long_skipped" for w in result.warnings)


def test_line_and_candidate_limits_report_warnings():
    text = "\n".join("Weight: 1 kg" for _ in range(c.MAX_LINES_PER_PAGE + 10))
    result = analyze_pages([PageText(1, text, "native")], ["CX-100"])
    assert any(w["code"] == "line_limit" for w in result.warnings)
    many = "\n".join(f"Weight: {i} kg" for i in range(1, 1500))
    limited = analyze_pages([PageText(1, many, "native")], ["CX-100"])
    assert len(limited.candidates) <= c.MAX_CANDIDATES and any(w["code"] == "candidate_limit_reached" for w in limited.warnings)


def test_the_parser_uses_no_network_or_filesystem_modules():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(c))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    }
    assert imported <= {"re", "dataclasses"}
