"""Hostile review of PR #70's PDF validation. Each test asserts the SECURE outcome.

Fixtures are benign: markers and non-existent file names only, never a working payload. The
interesting property is *where* a forbidden dictionary sits: inside a Flate-compressed object
stream the raw-byte scan cannot see it, so the structural pass has to find it."""

import zlib

import pytest

from app.application.catalog_documents.pdf_validation import PdfRejected, validate_pdf, validate_pdf_isolated

MAX_BYTES, MAX_PAGES = 25 * 1024 * 1024, 100


def _objstm_pdf(objects: dict[int, bytes], root: int = 1) -> bytes:
    """PDF 1.5 whose dictionaries live in one compressed object stream, plus an xref stream."""
    nums = sorted(objects)
    offsets, running = [], 0
    for n in nums:
        offsets.append(f"{n} {running}")
        running += len(objects[n]) + 1
    header = (" ".join(offsets) + "\n").encode()
    stream = zlib.compress(header + b"".join(objects[n] + b"\n" for n in nums))
    stm, xref = max(nums) + 1, max(nums) + 2
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    stm_off = len(out)
    out += f"{stm} 0 obj\n<</Type/ObjStm/N {len(nums)}/First {len(header)}/Filter/FlateDecode/Length {len(stream)}>>\nstream\n".encode()
    out += stream + b"\nendstream\nendobj\n"
    xref_off = len(out)
    rows = bytearray()
    for n in range(xref + 1):
        if n in objects:
            rows += bytes([2]) + stm.to_bytes(4, "big") + nums.index(n).to_bytes(2, "big")
        elif n == stm:
            rows += bytes([1]) + stm_off.to_bytes(4, "big") + (0).to_bytes(2, "big")
        elif n == xref:
            rows += bytes([1]) + xref_off.to_bytes(4, "big") + (0).to_bytes(2, "big")
        else:
            rows += bytes([0]) + (0).to_bytes(4, "big") + (0).to_bytes(2, "big")
    xstream = zlib.compress(bytes(rows))
    out += f"{xref} 0 obj\n<</Type/XRef/Size {xref + 1}/W[1 4 2]/Root {root} 0 R/Filter/FlateDecode/Length {len(xstream)}>>\nstream\n".encode()
    out += xstream + b"\nendstream\nendobj\nstartxref\n" + str(xref_off).encode() + b"\n%%EOF\n"
    return bytes(out)


_PAGES = {2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>", 3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>"}
_OUTLINES = b"<</Type/Outlines/First 5 0 R/Last 5 0 R/Count 1>>"


def _with_outline_action(action: bytes) -> bytes:
    return _objstm_pdf(
        {1: b"<</Type/Catalog/Pages 2 0 R/Outlines 4 0 R>>", **_PAGES, 4: _OUTLINES,
         5: b"<</Title(B)/Parent 4 0 R/A" + action + b">>"}
    )


ACTIVE_CONTENT_IN_COMPRESSED_OBJECTS = {
    "outline_launch": _with_outline_action(b"<</S/Launch/F(harmless-marker.txt)>>"),
    "outline_javascript": _with_outline_action(b"<</S/JavaScript/JS(/* benign marker */)>>"),
    "outline_goto_remote": _with_outline_action(b"<</S/GoToR/F(other.pdf)/D[0/Fit]>>"),
    "outline_submit_form": _with_outline_action(b"<</S/SubmitForm/F(<</FS/URL/F(http://127.0.0.1:9/)>>)>>"),
    "page_less_form_field_calc_script": _objstm_pdf(
        {1: b"<</Type/Catalog/Pages 2 0 R/AcroForm<</Fields[4 0 R]/CO[4 0 R]>>>>", **_PAGES,
         4: b"<</FT/Tx/T(f)/V(1)/AA<</C<</S/JavaScript/JS(/* benign marker */)>>>>>>"}
    ),
}


def test_control_clean_compressed_object_stream_pdf_is_accepted():
    clean = _objstm_pdf({1: b"<</Type/Catalog/Pages 2 0 R>>", **_PAGES})
    assert validate_pdf(clean, max_bytes=MAX_BYTES, max_pages=MAX_PAGES).page_count == 1


@pytest.mark.parametrize("name", sorted(ACTIVE_CONTENT_IN_COMPRESSED_OBJECTS))
def test_active_content_hidden_in_compressed_object_streams_is_rejected(name):
    content = ACTIVE_CONTENT_IN_COMPRESSED_OBJECTS[name]
    assert b"/Launch" not in content and b"/JavaScript" not in content  # invisible to a raw byte scan
    with pytest.raises(PdfRejected) as rejected:
        validate_pdf_isolated(content, max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert rejected.value.code == "active_content"


def test_uri_links_stay_allowed_in_compressed_object_streams():
    """The PR deliberately accepts /URI (inert server-side); the stricter scan must not regress that."""
    content = _with_outline_action(b"<</S/URI/URI(https://example.com/product)>>")
    assert validate_pdf_isolated(content, max_bytes=MAX_BYTES, max_pages=MAX_PAGES).page_count == 1


def test_page_count_cannot_be_understated_by_the_page_tree():
    """/Count says 1 while /Kids lists far more pages than the cap allows."""
    kids = b" ".join(f"{n} 0 R".encode() for n in range(3, 3 + 400))
    objs = {1: b"<</Type/Catalog/Pages 2 0 R>>", 2: b"<</Type/Pages/Kids[" + kids + b"]/Count 1>>"}
    for n in range(3, 3 + 400):
        objs[n] = b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>"
    with pytest.raises(PdfRejected) as rejected:
        validate_pdf_isolated(_objstm_pdf(objs), max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert rejected.value.code == "too_many_pages"
