"""DXF parser: supported subset and hostile input. The parser runs only in the sandboxed child in production;
these tests exercise it in-process for precision, and `test_spatial_import_sandbox.py` proves the isolation."""

import pytest

from app.application.spatial_import.dxf_parser import DxfRejected, parse_dxf
from app.application.spatial_import.limits import ParserLimits
from tests import spatial_fixtures as fx


def _kinds(doc):
    return [(e.kind, e.layer) for e in doc.entities]


def test_rack_row_parses_to_rectangles_and_labels_in_millimetres():
    doc = parse_dxf(fx.rack_row_dxf(3))
    assert doc.source_units == "mm" and doc.units_trusted and doc.y_axis == "up"
    rects = [e for e in doc.entities if e.kind == "rect"]
    assert len(rects) == 4  # room outline + 3 racks
    rack = next(e for e in rects if e.layer == "RACKS")
    assert (rack.width, rack.height) == (600, 1000) and rack.rotation_deg == 0
    assert {e.text for e in doc.entities if e.kind == "text"} == {"RACK-01", "RACK-02", "RACK-03"}
    assert doc.layers == ["WALLS", "RACKS", "TEXT"]


@pytest.mark.parametrize(("code", "unit"), [(1, "in"), (2, "ft"), (4, "mm"), (5, "cm"), (6, "m")])
def test_declared_units_are_mapped(code, unit):
    assert parse_dxf(fx.dxf(fx.rect_poly("1", "A", 0, 0, 10, 20), insunits=code)).source_units == unit


def test_undeclared_units_are_unitless_and_warned():
    doc = parse_dxf(fx.dxf(fx.rect_poly("1", "A", 0, 0, 10, 20), insunits=None))
    assert doc.source_units == "unitless" and not doc.units_trusted
    assert any(w["code"] == "dxf_units_undeclared" for w in doc.warnings)


def test_unsupported_unit_code_warns_but_does_not_trust():
    doc = parse_dxf(fx.dxf(fx.rect_poly("1", "A", 0, 0, 10, 20), insunits=14))  # decimetres
    assert not doc.units_trusted and any(w["code"] == "dxf_unit_unsupported" for w in doc.warnings)


def test_invalid_unit_code_is_rejected():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf("", insunits=99))
    assert exc.value.code == "dxf_invalid_units"


def test_rotated_rectangle_keeps_orientation():
    doc = parse_dxf(fx.dxf(fx.lwpoly("1", "R", [(0, 0), (707.1068, 707.1068), (0, 1414.2136), (-707.1068, 707.1068)])))
    (rect,) = [e for e in doc.entities if e.kind == "rect"]
    assert rect.rotation_deg == pytest.approx(45, abs=0.01)
    assert rect.width == pytest.approx(1000, abs=0.01) and rect.height == pytest.approx(1000, abs=0.01)


def test_non_rectangular_closed_polyline_is_a_polygon():
    doc = parse_dxf(fx.dxf(fx.lwpoly("1", "R", [(0, 0), (100, 0), (100, 50), (50, 100), (0, 100)])))
    assert _kinds(doc) == [("polygon", "R")]


def test_block_insert_is_flattened_with_translation_scale_and_rotation():
    blk = fx.block("RACKBLK", fx.rect_poly("b1", "0", 0, 0, 600, 1000))
    doc = parse_dxf(fx.dxf(fx.insert("9", "RACKS", "RACKBLK", 2000, 3000, rotation=90, scale=2.0), blocks=blk))
    (rect,) = [e for e in doc.entities if e.kind == "rect"]
    # 600 x 1000 scaled x2 = 1200 x 2000; rotated 90 degrees it spans 2000 in X and 1200 in Y (axis-aligned form)
    assert (rect.width, rect.height) == (pytest.approx(2000), pytest.approx(1200))
    assert rect.rotation_deg == 0
    # block centre (300, 500) -> x2 -> (600, 1000) -> rotate 90 -> (-1000, 600) -> translate (2000, 3000)
    assert (rect.cx, rect.cy) == (pytest.approx(1000), pytest.approx(3600))


def test_polyline_with_vertices_and_seqend_is_supported():
    body = "0\nPOLYLINE\n5\nP1\n8\nR\n70\n1\n"
    for x, y in ((0, 0), (500, 0), (500, 800), (0, 800)):
        body += f"0\nVERTEX\n8\nR\n10\n{x}\n20\n{y}\n"
    body += "0\nSEQEND\n"
    (rect,) = [e for e in parse_dxf(fx.dxf(body)).entities if e.kind == "rect"]
    assert (rect.width, rect.height) == (500, 800)


def test_mtext_formatting_codes_are_stripped():
    body = "0\nMTEXT\n5\nM1\n8\nT\n10\n0\n20\n0\n40\n50\n1\n{\\fArial|b1;RACK\\PA-01}\n"
    (label,) = [e for e in parse_dxf(fx.dxf(body)).entities if e.kind == "text"]
    assert label.text == "RACK A-01"


def test_unsupported_entities_are_counted_and_reported_not_silently_dropped():
    body = fx.rect_poly("1", "R", 0, 0, 10, 10) + "0\nSPLINE\n5\nS1\n8\nR\n0\nHATCH\n5\nH1\n8\nR\n"
    doc = parse_dxf(fx.dxf(body))
    assert doc.unsupported_object_count == 2
    assert any(w["code"] == "dxf_unsupported_entity" and "SPLINE" in w["detail"] for w in doc.warnings)


def test_lwpolyline_bulge_is_flagged_as_approximated():
    body = "0\nLWPOLYLINE\n5\nA\n8\nR\n90\n3\n70\n1\n10\n0\n20\n0\n42\n0.5\n10\n100\n20\n0\n10\n100\n20\n100\n"
    assert any(w["code"] == "dxf_bulge_approximated" for w in parse_dxf(fx.dxf(body)).warnings)


# ---------------------------------------------------------------- hostile input
def test_missing_eof_is_a_truncated_file():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.rect_poly("1", "R", 0, 0, 10, 10), eof=False))
    assert exc.value.code == "dxf_truncated"


def test_truncated_mid_section_is_rejected():
    data = fx.dxf(fx.rect_poly("1", "R", 0, 0, 10, 10))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(data[: data.index(b"ENDSEC", data.index(b"ENTITIES"))])
    assert exc.value.code == "dxf_truncated"


@pytest.mark.parametrize("garbage", [b"not a dxf at all\nreally\n", b"0\nSECTION\n2\nENTITIES\nxx\nyy\n0\nENDSEC\n0\nEOF\n"])
def test_malformed_group_codes_are_rejected(garbage):
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(garbage)
    assert exc.value.code == "dxf_malformed"


def test_missing_entities_section_is_rejected():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n")
    assert exc.value.code == "dxf_malformed"


def test_invalid_number_is_rejected():
    body = "0\nLINE\n5\nL\n8\nR\n10\nabc\n20\n0\n11\n1\n21\n1\n"
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(body))
    assert exc.value.code == "dxf_malformed"


@pytest.mark.parametrize("value", ["1e30", "inf", "nan", "-1e15"])
def test_huge_or_non_finite_coordinates_are_rejected(value):
    body = f"0\nLINE\n5\nL\n8\nR\n10\n{value}\n20\n0\n11\n1\n21\n1\n"
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(body))
    assert exc.value.code == "dxf_coordinate_out_of_range"


def test_excessive_entities_are_rejected():
    limits = ParserLimits(max_entities=50, max_expanded_entities=1000)
    body = "".join(fx.line(f"{i:x}", "R", 0, 0, i, i) for i in range(200))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(body), limits=limits)
    assert exc.value.code == "dxf_too_many_entities"


def test_recursive_block_reference_is_rejected():
    blocks = fx.block("A", fx.insert("a1", "0", "B", 0, 0)) + fx.block("B", fx.insert("b1", "0", "A", 0, 0))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.insert("9", "0", "A", 0, 0), blocks=blocks))
    assert exc.value.code == "dxf_recursive_block"


def test_self_referencing_block_is_rejected():
    blocks = fx.block("A", fx.insert("a1", "0", "A", 0, 0))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.insert("9", "0", "A", 0, 0), blocks=blocks))
    assert exc.value.code == "dxf_recursive_block"


def test_exponential_block_expansion_is_stopped_by_the_budget():
    """A chain of blocks each inserting the next ten times: 10^7 leaves from a few KB."""
    blocks = ""
    for level in range(7):
        inner = "".join(fx.insert(f"i{level}{n}", "0", f"L{level + 1}", 0, 0) for n in range(10))
        blocks += fx.block(f"L{level}", inner)
    blocks += fx.block("L7", fx.line("leaf", "0", 0, 0, 1, 1))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.insert("9", "0", "L0", 0, 0), blocks=blocks), limits=ParserLimits(max_expanded_entities=20_000))
    assert exc.value.code in {"dxf_expansion_limit", "dxf_too_many_entities", "dxf_too_large"}


def test_block_nesting_beyond_the_depth_limit_is_rejected():
    blocks = "".join(fx.block(f"N{i}", fx.insert(f"x{i}", "0", f"N{i + 1}", 0, 0)) for i in range(12))
    blocks += fx.block("N12", fx.line("leaf", "0", 0, 0, 1, 1))
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.insert("9", "0", "N0", 0, 0), blocks=blocks), limits=ParserLimits(max_block_depth=8))
    assert exc.value.code == "dxf_block_depth"


def test_giant_text_is_rejected():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.text("1", "T", 0, 0, "A" * 5000)))
    assert exc.value.code == "dxf_text_too_long"


def test_too_many_layers_is_rejected():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf("", layers=[f"L{i}" for i in range(600)]))
    assert exc.value.code == "dxf_too_many_layers"


def test_polyline_with_too_many_points_is_rejected():
    pts = [(i, i % 7) for i in range(1000)]
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(fx.lwpoly("1", "R", pts)))
    assert exc.value.code == "dxf_too_many_points"


def test_oversized_input_is_rejected_before_parsing():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(b"0\n" * 10, limits=ParserLimits(max_input_bytes=5))
    assert exc.value.code == "dxf_too_large"


def test_too_many_lines_is_rejected():
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(b"0\nSECTION\n" * 50, limits=ParserLimits(max_text_lines=40))
    assert exc.value.code == "dxf_too_large"


def test_lwpolyline_vertex_array_mismatch_is_rejected():
    body = "0\nLWPOLYLINE\n5\nA\n8\nR\n70\n1\n10\n0\n20\n0\n10\n5\n"
    with pytest.raises(DxfRejected) as exc:
        parse_dxf(fx.dxf(body))
    assert exc.value.code == "dxf_malformed"


def test_binary_dxf_and_dwg_are_unsupported_formats():
    from app.application.spatial_import.formats import UnsupportedFormat, detect_format

    for data, code in ((b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 30, "binary_dxf_unsupported"), (b"AC1027" + b"\x00" * 40, "dwg_unsupported")):
        with pytest.raises(UnsupportedFormat) as exc:
            detect_format(data)
        assert exc.value.code == code


def test_cp1252_encoded_text_is_tolerated():
    data = fx.dxf(fx.text("1", "T", 0, 0, "Salle é")).replace("é".encode(), b"\xe9")
    (label,) = [e for e in parse_dxf(data).entities if e.kind == "text"]
    assert label.text == "Salle é"
