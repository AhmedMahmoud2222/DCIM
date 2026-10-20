"""VSDX parser: supported subset and hostile packages (zip bomb, traversal, XXE/DTD, external relationships,
macros, deep groups, oversized XML, malformed/missing parts). In-process for precision; production runs it only
inside the sandboxed child."""

import io
import zipfile

import pytest

from app.application.spatial_import.limits import ParserLimits
from app.application.spatial_import.vsdx_parser import VsdxRejected, parse_vsdx
from tests import spatial_fixtures as fx

IN = fx.MM_PER_IN


def _rects(doc):
    return [e for e in doc.entities if e.kind == "rect"]


def test_rack_row_parses_in_inches_with_labels():
    doc = parse_vsdx(fx.rack_row_vsdx(3))
    assert doc.source_units == "in" and doc.units_trusted and doc.y_axis == "up"
    racks = [e for e in _rects(doc) if e.text]
    assert len(racks) == 3
    assert racks[0].width * IN == pytest.approx(600) and racks[0].height * IN == pytest.approx(1000)
    assert racks[0].text == "RACK-01"
    assert any(w["code"] == "vsdx_scale_undeclared" for w in doc.warnings)


def test_drawing_scale_is_applied_to_real_world_size():
    # 1 page inch = 10 real inches
    doc = parse_vsdx(fx.vsdx(fx.vsdx_shape(1, 1000, 1000, 100, 200), page_scale=(1, 10)))
    (rect,) = _rects(doc)
    assert rect.width * IN == pytest.approx(1000) and rect.height * IN == pytest.approx(2000)
    assert doc.notes["drawing_scale_declared"] is True


def test_rotation_and_group_transforms_are_composed():
    child = fx.vsdx_shape(2, 100, 100, 200, 100, label="C")  # local to the group frame
    group = fx.vsdx_shape(1, 1000, 2000, 400, 400, children=child, shape_type="Group", name="G")
    (rect,) = _rects(parse_vsdx(fx.vsdx(group)))
    # group local origin sits at pin - locpin = (1000-200, 2000-200) = (800, 1800) mm
    assert rect.cx * IN == pytest.approx(800 + 100) and rect.cy * IN == pytest.approx(1800 + 100)
    rotated = fx.vsdx_shape(1, 1000, 1000, 600, 1000, angle_deg=30)
    (r,) = _rects(parse_vsdx(fx.vsdx(rotated)))
    assert r.rotation_deg == pytest.approx(30, abs=1e-6)


def test_ninety_degree_rotation_is_reported_axis_aligned_with_swapped_sides():
    (r,) = _rects(parse_vsdx(fx.vsdx(fx.vsdx_shape(1, 1000, 1000, 600, 1000, angle_deg=90))))
    assert r.rotation_deg == 0 and r.width * IN == pytest.approx(1000) and r.height * IN == pytest.approx(600)


def test_foreign_shapes_are_ignored_and_reported():
    doc = parse_vsdx(fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1, shape_type="Foreign")))
    assert not doc.entities and any(w["code"] == "vsdx_foreign_ignored" for w in doc.warnings)


def test_embedded_objects_are_not_read_but_reported():
    doc = parse_vsdx(fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/embeddings/blob.bin": b"x" * 10}))
    assert any(w["code"] == "vsdx_embeddings_ignored" for w in doc.warnings)


# ---------------------------------------------------------------- hostile packages
def _expect(code, data, **kw):
    with pytest.raises(VsdxRejected) as exc:
        parse_vsdx(data, **kw)
    assert exc.value.code == code, exc.value.code


def test_not_a_zip_is_rejected():
    _expect("vsdx_malformed_package", b"PK\x03\x04" + b"\x00" * 100)


def test_truncated_zip_is_rejected():
    data = fx.rack_row_vsdx(2)
    _expect("vsdx_malformed_package", data[: len(data) // 2])


def test_corrupt_deflate_stream_is_rejected():
    data = bytearray(fx.rack_row_vsdx(2))
    header = data.index(b"visio/pages/page1.xml")  # local file header name of the page part
    extra_len = int.from_bytes(data[header - 2 : header], "little")
    start = header + len(b"visio/pages/page1.xml") + extra_len
    data[start : start + 24] = b"\xff" * 24
    with pytest.raises(VsdxRejected):
        parse_vsdx(bytes(data))


@pytest.mark.parametrize("missing", ["[Content_Types].xml", "visio/document.xml", "visio/pages/page1.xml"])
def test_missing_required_parts_are_rejected(missing):
    code = "vsdx_missing_part"
    _expect(code, fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), drop=[missing]))


def test_non_visio_ooxml_package_is_rejected():
    content_types = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/visio/document.xml" ContentType="text/plain"/></Types>'
    _expect("vsdx_not_visio", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), override={"[Content_Types].xml": content_types}))


@pytest.mark.parametrize(
    "name", ["../evil.xml", "visio/../../evil.xml", "/etc/passwd", "C:/evil.xml", "visio\\pages\\evil.xml", "a/../../b"]
)
def test_path_traversal_entry_names_are_rejected(name):
    _expect("vsdx_path_traversal", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={name: b"x"}))


def test_nul_byte_entry_name_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("visio/doc\x00.xml", b"x")
    with pytest.raises(VsdxRejected):  # Python truncates the name at the NUL, leaving no valid Visio package
        parse_vsdx(buffer.getvalue())


def test_duplicate_entry_names_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("visio/document.xml", b"<a/>")
        archive.writestr("Visio/Document.xml", b"<b/>")
    _expect("vsdx_duplicate_entry", buffer.getvalue())


def test_excessive_entry_count_is_rejected():
    extra = {f"visio/media/{i}.png": b"x" for i in range(600)}
    _expect("vsdx_too_many_entries", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra=extra))


def test_zip_bomb_single_entry_high_compression_ratio_is_rejected():
    bomb = b"\x00" * (30 * 1024 * 1024)  # ~30 KB compressed
    _expect("vsdx_entry_too_large", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/media/bomb.bin": bomb}))


def test_high_compression_ratio_below_the_size_cap_is_rejected():
    data = b"A" * (15 * 1024 * 1024)
    _expect("vsdx_compression_ratio", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/media/x.bin": data}))


def test_total_expanded_size_cap_is_enforced():
    limits = ParserLimits(max_total_uncompressed_bytes=2 * 1024 * 1024, max_compression_ratio=1e9)
    noise = bytes((i * 7919) % 251 for i in range(1_500_000))
    extra = {f"visio/media/{i}.bin": noise for i in range(3)}
    _expect("vsdx_package_too_large", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra=extra), limits=limits)


def test_header_lying_about_expanded_size_is_caught_while_streaming():
    """A forged local header claims a small size; the real stream is larger. Streaming enforcement still stops it."""
    data = bytearray(fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/media/liar.bin": b"B" * 200_000}))
    central = data.index(b"PK\x01\x02", data.index(b"liar.bin"))
    # central-directory uncompressed size is at offset +24; shrink it
    data[central + 24 : central + 28] = (10).to_bytes(4, "little")
    with pytest.raises(VsdxRejected):
        parse_vsdx(bytes(data), limits=ParserLimits())


def test_encrypted_entries_are_rejected():
    data = bytearray(fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1)))
    central = data.index(b"PK\x01\x02")
    flags = int.from_bytes(data[central + 8 : central + 10], "little") | 1
    data[central + 8 : central + 10] = flags.to_bytes(2, "little")
    _expect("vsdx_encrypted", bytes(data))


@pytest.mark.parametrize("payload", [
    '<!DOCTYPE x [<!ENTITY a "boom">]><PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>&a;</Shapes></PageContents>',
    '<!DOCTYPE x SYSTEM "http://evil.example/x.dtd"><PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"/>',
    '<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>&xxe;</Shapes></PageContents>',
])
def test_dtd_and_entities_are_forbidden(payload):
    _expect("vsdx_xml_forbidden", fx.vsdx(override={"visio/pages/page1.xml": payload.encode()}))


def test_billion_laughs_is_rejected_without_expansion():
    laughs = '<!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>&c;</Shapes></PageContents>'
    _expect("vsdx_xml_forbidden", fx.vsdx(override={"visio/pages/page1.xml": laughs.encode()}))


def test_malformed_xml_is_rejected():
    _expect("vsdx_xml_malformed", fx.vsdx(override={"visio/pages/page1.xml": b"<PageContents><Shapes></PageContents>"}))


def test_external_relationship_is_rejected():
    rels = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://x/hyperlink" Target="https://evil.example/x" TargetMode="External"/></Relationships>'
    _expect("vsdx_external_relationship", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/pages/_rels/page1.xml.rels": rels}))


def test_unc_and_scheme_relationship_targets_are_rejected():
    for target in ("\\\\host\\share\\x", "file:///etc/passwd", "http://evil.example/a"):
        rels = f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://x/y" Target="{target}"/></Relationships>'.encode()
        _expect("vsdx_external_relationship", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/_rels/document.xml.rels": rels}))


def test_relationship_escaping_the_package_is_rejected():
    rels = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://x/y" Target="../../../../etc/passwd"/></Relationships>'
    _expect("vsdx_path_traversal", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/_rels/document.xml.rels": rels}))


@pytest.mark.parametrize("name", ["visio/vbaProject.bin", "visio/activeX/activeX1.xml", "visio/macros/macro1.bin"])
def test_macro_and_activex_parts_are_rejected(name):
    _expect("vsdx_active_content", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={name: b"x"}))


def test_macro_enabled_content_type_is_rejected():
    content_types = fx._CT.replace("application/vnd.ms-visio.drawing.main+xml", "application/vnd.ms-visio.drawing.macroEnabled.main+xml").encode()
    _expect("vsdx_active_content", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), override={"[Content_Types].xml": content_types}))


def test_ole_relationship_type_is_rejected():
    rels = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="embeddings/o.bin"/></Relationships>'
    _expect("vsdx_active_content", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/_rels/document.xml.rels": rels}))


def test_deeply_nested_groups_are_rejected():
    inner = fx.vsdx_shape(999, 10, 10, 10, 10)
    for depth in range(20):
        inner = fx.vsdx_shape(100 + depth, 10, 10, 100, 100, children=inner, shape_type="Group")
    _expect("vsdx_shape_depth", fx.vsdx(inner), limits=ParserLimits(max_shape_depth=10))


def test_extremely_deep_xml_nesting_is_rejected_not_a_stack_overflow():
    deep = "<a>" * 5000 + "</a>" * 5000
    payload = f'<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main">{deep}</PageContents>'.encode()
    _expect("vsdx_xml_too_deep", fx.vsdx(override={"visio/pages/page1.xml": payload}))


def test_oversized_xml_element_count_is_rejected():
    many = "<Cell N='x' V='1'/>" * 5000
    payload = f'<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>{many}</Shapes></PageContents>'.encode()
    _expect("vsdx_xml_too_large", fx.vsdx(override={"visio/pages/page1.xml": payload}), limits=ParserLimits(max_xml_elements=1000))


def test_too_many_shapes_is_rejected():
    shapes = "".join(fx.vsdx_shape(i, i, i, 1, 1) for i in range(1, 400))
    _expect("vsdx_too_many_shapes", fx.vsdx(shapes), limits=ParserLimits(max_expanded_entities=100, max_entities=100))


def test_giant_shape_text_is_rejected():
    _expect("vsdx_text_too_long", fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1, label="A" * 5000)))


def test_huge_coordinates_are_rejected():
    shape = '<Shape ID="1" Type="Shape"><Cell N="PinX" V="1e30"/><Cell N="PinY" V="1"/><Cell N="Width" V="1"/><Cell N="Height" V="1"/></Shape>'
    _expect("vsdx_coordinate_out_of_range", fx.vsdx(shape))


def test_oversized_input_is_rejected_before_unzipping():
    _expect("vsdx_too_large", fx.rack_row_vsdx(2), limits=ParserLimits(max_input_bytes=100))
