import pytest

from app.application.svg_sanitizer import (
    MAX_ELEMENTS,
    MAX_SVG_FILE_SIZE_BYTES,
    SvgRejected,
    sanitize_svg,
    validate_raster_image,
)


def test_sanitize_svg_extracts_rect_circle_and_text_shapes():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="2" width="60" height="100" />
        <circle cx="10" cy="20" r="5" />
        <text x="0" y="0">Rack 1</text>
    </svg>"""
    result = sanitize_svg(content)
    types = sorted(s.shape_type for s in result.shapes)
    assert types == ["circle", "rect", "text"]
    assert result.objects_discovered == 3
    assert result.unsupported_object_count == 0


def test_sanitize_svg_strips_script_tag_and_does_not_recurse_into_it():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <script>alert(document.cookie); <rect x="1" y="1" width="1" height="1" /></script>
    </svg>"""
    result = sanitize_svg(content)
    assert result.shapes == []
    assert result.unsupported_object_count == 1
    assert any("script" in w for w in result.warnings)


def test_sanitize_svg_strips_event_handler_attributes():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="0" y="0" width="10" height="10" onload="evil()" onclick="alsoEvil()" />
    </svg>"""
    result = sanitize_svg(content)
    assert len(result.shapes) == 1
    assert any("onload" in w for w in result.warnings)
    assert any("onclick" in w for w in result.warnings)


def test_sanitize_svg_strips_external_href_references():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
        <a href="https://evil.example.com/exfil"><rect x="0" y="0" width="1" height="1" /></a>
        <a xlink:href="http://evil.example.com/other"><rect x="1" y="1" width="1" height="1" /></a>
    </svg>"""
    result = sanitize_svg(content)
    assert any("external reference" in w for w in result.warnings)
    assert len(result.shapes) == 2  # the rects inside the stripped <a> hrefs are still captured


def test_sanitize_svg_allows_internal_fragment_href():
    """A same-document fragment reference (#id) is not an external reference and must
    not be stripped or warned about."""
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <a href="#section-2"><rect x="0" y="0" width="1" height="1" /></a>
    </svg>"""
    result = sanitize_svg(content)
    assert not any("external reference" in w for w in result.warnings)


def test_sanitize_svg_rejects_dtd_declaration_xxe_attempt():
    content = (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<svg>&xxe;</svg>"
    )
    with pytest.raises(SvgRejected):
        sanitize_svg(content)


def test_sanitize_svg_rejects_malformed_xml():
    with pytest.raises(SvgRejected):
        sanitize_svg(b"<svg><rect x=1></svg")


def test_sanitize_svg_rejects_content_not_sniffed_as_svg():
    with pytest.raises(SvgRejected):
        sanitize_svg(b"just some plain text, not xml at all")


def test_sanitize_svg_rejects_oversized_content():
    oversized = b'<svg xmlns="http://www.w3.org/2000/svg">' + b"A" * (MAX_SVG_FILE_SIZE_BYTES + 1) + b"</svg>"
    with pytest.raises(SvgRejected, match="size limit"):
        sanitize_svg(oversized)


def test_sanitize_svg_caps_element_count_without_hanging():
    """A pathological, deeply-repeated-element file must not be fully walked — the walk
    stops at MAX_ELEMENTS and reports a warning rather than continuing indefinitely."""
    inner = "<rect x='0' y='0' width='1' height='1' />" * (MAX_ELEMENTS + 500)
    content = f'<svg xmlns="http://www.w3.org/2000/svg">{inner}</svg>'.encode()
    result = sanitize_svg(content)
    assert result.objects_discovered == MAX_ELEMENTS
    assert any("element count cap" in w for w in result.warnings)


def test_sanitize_svg_clamps_out_of_range_coordinates_rather_than_propagating_them():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="99999999999" y="-99999999999" width="10" height="10" />
    </svg>"""
    result = sanitize_svg(content)
    assert result.shapes[0].x == 0.0
    assert result.shapes[0].y == 0.0


def test_sanitize_svg_treats_nan_and_infinity_coordinates_as_default():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="NaN" y="Infinity" width="10" height="10" />
    </svg>"""
    result = sanitize_svg(content)
    assert result.shapes[0].x == 0.0
    assert result.shapes[0].y == 0.0


def test_sanitize_svg_strips_unit_suffixes_from_coordinates():
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="10px" y="20mm" width="30" height="40" />
    </svg>"""
    result = sanitize_svg(content)
    assert result.shapes[0].x == 10.0
    assert result.shapes[0].y == 20.0


def test_sanitize_svg_never_returns_raw_element_or_markup():
    """The Sanitized Intermediate Representation boundary: only SirShape values, no
    Element objects or raw string markup, cross out of the sanitizer."""
    content = b'<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="1" height="1" /></svg>'
    result = sanitize_svg(content)
    for shape in result.shapes:
        assert type(shape).__name__ == "SirShape"


def test_validate_raster_image_accepts_matching_png_magic_bytes():
    validate_raster_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, declared_format="png")


def test_validate_raster_image_rejects_mismatched_magic_bytes():
    with pytest.raises(SvgRejected):
        validate_raster_image(b"not a png at all", declared_format="png")


def test_validate_raster_image_rejects_unsupported_format():
    with pytest.raises(SvgRejected):
        validate_raster_image(b"\x89PNG\r\n\x1a\n", declared_format="gif")


def test_validate_raster_image_rejects_oversized_content():
    from app.application.svg_sanitizer import MAX_RASTER_FILE_SIZE_BYTES

    oversized = b"\xff\xd8\xff" + b"\x00" * MAX_RASTER_FILE_SIZE_BYTES
    with pytest.raises(SvgRejected, match="size limit"):
        validate_raster_image(oversized, declared_format="jpeg")
