"""The forbidden-name byte scan and stream payloads.

A stream payload is compressed or binary data. ASCII85 uses `/` and letters, and raw bytes can
hold anything, so `/JS` or `/AA` followed by a non-name byte turns up in benign payloads by
chance. The validator skips the bytes the parser read as stream data. Every hostile fixture
here tries to use that skip to hide a forbidden name and asserts the SECURE outcome.

Fixtures are benign: markers only, never a working payload."""

import base64
import io
import zlib

import pytest
from pypdf import PdfReader

from app.application.catalog_documents.pdf_validation import _FORBIDDEN_NAME, PdfRejected, validate_pdf
from tests.api._document_helpers import make_pdf
from tests.unit.test_pr70_pdf_hostile import _PAGES, _objstm_pdf

LIMITS = {"max_bytes": 5 * 1024 * 1024, "max_pages": 10}
SCRIPT = b"<</S/JavaScript/JS(/* benign marker */)>>"


def _stream(payload: bytes, entries: bytes = b"", *, length: int | None = None) -> bytes:
    declared = len(payload) if length is None else length
    return b"<<" + entries + b"/Length %d>>\nstream\n" % declared + payload + b"\nendstream"


def _classic_pdf(objects: dict[int, bytes], *, before: bytes = b"", after: bytes = b"") -> bytes:
    """PDF 1.4 with a classic cross-reference table. `before` and `after` are raw bytes written
    ahead of and behind the listed objects: the table does not mention them."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + before)
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    out += after
    xref_offset, size = len(out), max(objects) + 1
    out += b"xref\n0 %d\n" % size
    for number in range(size):
        out += b"%010d 00000 n \n" % offsets[number] if number in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_offset)
    return bytes(out)


def _page_pdf(content_stream: bytes, extra: dict[int, bytes] | None = None, **raw: bytes) -> bytes:
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>",
        4: content_stream,
        **(extra or {}),
    }
    return _classic_pdf(objects, **raw)


def _reject_code(content: bytes) -> str:
    with pytest.raises(PdfRejected) as caught:
        validate_pdf(content, **LIMITS)
    return caught.value.code


def _ascii85_flate_payload_spelling_a_forbidden_name() -> tuple[bytes, bytes]:
    """Searches page texts in a fixed order for one whose Flate + ASCII85 encoding spells a
    forbidden name. Deterministic for a given zlib; about one text in ten thousand qualifies."""
    for attempt in range(2_000_000):
        plain = b"BT /F1 12 Tf 72 720 Td (Datasheet %d) Tj ET" % attempt
        encoded = base64.a85encode(zlib.compress(plain)) + b"~>"
        if _FORBIDDEN_NAME.search(encoded):
            return plain, encoded
    raise AssertionError("no qualifying payload found")


def test_ascii85_payload_that_spells_a_forbidden_name_by_chance_is_accepted():
    plain, encoded = _ascii85_flate_payload_spelling_a_forbidden_name()
    content = _page_pdf(_stream(encoded, b"/Filter[/ASCII85Decode/FlateDecode]"))
    assert _FORBIDDEN_NAME.search(content)  # a whole-file byte scan rejects this benign file
    assert PdfReader(io.BytesIO(content)).pages[0].get_contents().get_data() == plain
    assert validate_pdf(content, **LIMITS).page_count == 1


def test_binary_payload_that_spells_forbidden_names_is_accepted():
    samples = b"\x00\xff/JS\x00\x80/AA \x01/XFA\xfe" * 4
    image = _stream(samples, b"/Type/XObject/Subtype/Image/Width 4/Height 15/ColorSpace/DeviceGray/BitsPerComponent 8")
    assert validate_pdf(_page_pdf(_stream(b"q Q"), {5: image}), **LIMITS).page_count == 1


def test_thousands_of_generated_reportlab_pdfs_are_all_accepted():
    """`make_pdf("unique")` draws a fresh UUID, so each ASCII85 content stream differs. Before the
    fix about 1 in 3000 of these was rejected as active content."""
    rejected = []
    for _ in range(3000):
        try:
            validate_pdf(make_pdf("unique"), **LIMITS)
        except PdfRejected as exc:
            rejected.append(exc.code)
    assert rejected == []


def test_control_plain_script_text_as_page_content_is_only_stream_data():
    """The skip covers what the parser read as stream data, and nothing executes page content
    as a dictionary. The hostile fixtures below reuse this payload so that each rejection is
    caused by what they add around it."""
    assert validate_pdf(_page_pdf(_stream(SCRIPT)), **LIMITS).page_count == 1


HIDDEN_OBJECT = b"9 0 obj\n" + SCRIPT + b"\nendobj\n"
SKIP_EVASIONS = {
    "unlisted_object_right_after_endstream": _page_pdf(_stream(b"q Q"), after=HIDDEN_OBJECT),
    "name_glued_to_endstream": _page_pdf(_stream(b"q Q") + b"/JS(/* benign marker */)"),
    "name_between_endstream_and_a_later_stream": _page_pdf(
        _stream(b"q Q"), {5: _stream(b"q Q")}, after=b"endstream\n" + SCRIPT + b"\nstream\n"
    ),
    "fake_stream_keyword_in_a_string": _page_pdf(
        _stream(b"q Q"), after=b"9 0 obj\n[(stream\n) " + SCRIPT + b" (\nendstream)]\nendobj\n"
    ),
    "fake_stream_keyword_in_a_string_of_a_listed_object": _page_pdf(
        _stream(b"q Q"), {5: b"[(stream\n) " + SCRIPT + b" (\nendstream)]"}
    ),
    "fake_stream_keyword_in_a_comment_copying_a_real_payload_later": _page_pdf(
        _stream(SCRIPT), after=b"9 0 obj %stream\n" + SCRIPT + b"\nendstream\nendobj\n"
    ),
    "fake_stream_keyword_in_a_comment_copying_a_real_payload_earlier": _page_pdf(
        _stream(SCRIPT), before=b"9 0 obj %stream\n" + SCRIPT + b"\nendstream\nendobj\n"
    ),
    "length_that_swallows_a_following_object": _page_pdf(
        _stream(b"q Q\nendstream\nendobj\n" + HIDDEN_OBJECT + b"%", length=len(b"q Q\nendstream\nendobj\n" + HIDDEN_OBJECT + b"%"))
    ),
    "object_header_inside_a_payload": _page_pdf(_stream(b"9 0 obj\n" + SCRIPT)),
    "object_header_inside_a_declared_flate_payload": _page_pdf(_stream(b"9 0 obj\n" + SCRIPT, b"/Filter/FlateDecode")),
    "payload_equal_to_the_tail_of_a_name_in_its_own_dictionary": _page_pdf(_stream(b"h", b"/Kind/Launch")),
    "payload_equal_to_the_tail_of_a_key_in_its_own_dictionary": _page_pdf(_stream(b"S 1", b"/JS 1")),
    "hex_escaped_name_after_endstream": _page_pdf(_stream(b"q Q"), after=b"9 0 obj\n<</J#53(/* benign marker */)>>\nendobj\n"),
}


@pytest.mark.parametrize("name", sorted(SKIP_EVASIONS))
def test_stream_payload_skip_cannot_hide_a_forbidden_name(name):
    assert _reject_code(SKIP_EVASIONS[name]) == "active_content"


_CATALOG = {1: b"<</Type/Catalog/Pages 2 0 R>>", **_PAGES}
OBJECT_STREAM_EVASIONS = {
    "member_without_a_cross_reference_entry": _objstm_pdf({**_CATALOG, 9: SCRIPT}, unlisted=frozenset({9})),
    "member_without_a_cross_reference_entry_uncompressed": _objstm_pdf(
        {**_CATALOG, 9: SCRIPT}, unlisted=frozenset({9}), compress=False
    ),
    "listed_member_uncompressed": _objstm_pdf({**_CATALOG, 9: SCRIPT}, compress=False),
    "hex_escaped_key": _objstm_pdf({**_CATALOG, 9: b"<</J#53(/* benign marker */)>>"}),
    "hex_escaped_key_without_a_cross_reference_entry": _objstm_pdf(
        {**_CATALOG, 9: b"<</J#53(/* benign marker */)>>"}, unlisted=frozenset({9})
    ),
    "forbidden_name_as_a_value": _objstm_pdf({**_CATALOG, 9: b"<</Kind/Launch>>"}),
    "forbidden_name_in_an_array": _objstm_pdf({**_CATALOG, 9: b"[/Fit [/EmbeddedFiles]]"}),
    "name_tree_key_below_the_catalog": _objstm_pdf({**_CATALOG, 9: b"<</Tree<</JavaScript 8 0 R>>>>"}),
}


@pytest.mark.parametrize("name", sorted(OBJECT_STREAM_EVASIONS))
def test_object_streams_are_scanned_for_forbidden_names(name):
    assert _reject_code(OBJECT_STREAM_EVASIONS[name]) == "active_content"


def test_control_uncompressed_object_stream_pdf_is_accepted():
    assert validate_pdf(_objstm_pdf(_CATALOG, compress=False), **LIMITS).page_count == 1
