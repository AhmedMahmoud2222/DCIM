import pytest

from app.application.svg_sanitizer import (
    MAX_ELEMENTS,
    MAX_NESTING_DEPTH,
    MAX_SVG_FILE_SIZE_BYTES,
    SvgRejected,
    sanitize_svg,
    validate_raster_image,
)


def _nested_group_svg(depth: int) -> bytes:
    """A single <rect> nested `depth` levels deep inside `depth` <g> elements."""
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        + b"<g>" * depth
        + b'<rect x="0" y="0" width="1" height="1" />'
        + b"</g>" * depth
        + b"</svg>"
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


# --------------------------------------------------------------------------- nested-SVG
# regression (PHASE2_NESTED_SVG_CORRECTION_REPORT.md): a deeply nested SVG (~960+ levels
# of <g>) previously crashed the (then-recursive) walk with an uncaught RecursionError.
# `_walk` is now iterative and enforces an explicit, deterministic MAX_NESTING_DEPTH.


def test_sanitize_svg_moderate_nesting_succeeds():
    """A legitimate, if unusually deeply grouped, floor plan (well under the limit) must
    still import normally — this control must not restrict real data-center floor plans."""
    result = sanitize_svg(_nested_group_svg(20))
    assert len(result.shapes) == 1
    assert result.shapes[0].shape_type == "rect"


def test_sanitize_svg_accepts_exactly_the_maximum_allowed_depth():
    result = sanitize_svg(_nested_group_svg(MAX_NESTING_DEPTH - 1))
    assert len(result.shapes) == 1


def test_sanitize_svg_rejects_one_level_beyond_the_maximum_allowed_depth():
    with pytest.raises(SvgRejected, match="depth limit"):
        sanitize_svg(_nested_group_svg(MAX_NESTING_DEPTH))


def test_sanitize_svg_rejects_the_independently_reported_red_team_depth_without_recursionerror():
    """The literal attack PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md
    independently reproduced: ~960-970 levels of nested <g>, previously an uncaught
    RecursionError. Must now be a clean, deterministic SvgRejected."""
    try:
        sanitize_svg(_nested_group_svg(970))
    except SvgRejected:
        pass
    else:
        pytest.fail("expected SvgRejected for 970-level nesting")


def test_sanitize_svg_rejects_extreme_nesting_depth_quickly_and_without_recursionerror():
    """Substantially beyond the previously reported depth (960+) — the iterative walk
    must reject this just as fast and just as cleanly, proving the fix is a genuine
    removal of Python call-stack depth as an attack surface, not a narrowly-tuned patch
    for one specific depth."""
    import time

    for depth in (20_000, 100_000):
        t0 = time.monotonic()
        with pytest.raises(SvgRejected, match="depth limit"):
            sanitize_svg(_nested_group_svg(depth))
        assert time.monotonic() - t0 < 2.0, f"depth {depth} took too long to reject"


def test_sanitize_svg_preserves_shape_order_after_switching_to_iterative_traversal():
    """The rewrite from a recursive to an iterative walk must not change the resulting
    shape order for any document that doesn't hit the new depth limit."""
    content = b"""<svg xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="1" width="1" height="1" />
        <g>
            <rect x="2" y="2" width="1" height="1" />
            <circle cx="3" cy="3" r="1" />
        </g>
        <rect x="4" y="4" width="1" height="1" />
    </svg>"""
    result = sanitize_svg(content)
    assert [(s.shape_type, s.x) for s in result.shapes] == [
        ("rect", 1.0), ("rect", 2.0), ("circle", 3.0), ("rect", 4.0),
    ]


def test_sanitize_svg_repeated_malicious_deep_svg_does_not_corrupt_subsequent_calls():
    """Running the malicious payload repeatedly must not leave any interpreter-level
    state (e.g. a raised-but-not-fully-unwound recursion) that corrupts a later, unrelated
    call in the same process."""
    for _ in range(5):
        with pytest.raises(SvgRejected, match="depth limit"):
            sanitize_svg(_nested_group_svg(5_000))
    # A completely normal file afterward must still succeed cleanly.
    result = sanitize_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="1" height="1"/></svg>')
    assert len(result.shapes) == 1


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
