"""The forbidden-name byte scan and stream payloads.

A stream payload is compressed or binary data. ASCII85 uses `/` and letters, and raw bytes can
hold anything, so `/JS` or `/AA` followed by a non-name byte turns up in benign payloads by
chance. The validator skips the bytes the parser read as stream data. Every hostile fixture
here tries to use that skip to hide a forbidden name and asserts the SECURE outcome.

Fixtures are benign: markers only, never a working payload."""

import base64
import hashlib
import io
import re
import time
import zlib
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from app.application.catalog_documents import pdf_validation
from app.application.catalog_documents.pdf_validation import _FORBIDDEN_NAME, PdfRejected, validate_pdf
from tests.api._document_helpers import make_pdf
from tests.unit.test_pr70_pdf_hostile import _PAGES, _objstm_pdf

LIMITS = {"max_bytes": 5 * 1024 * 1024, "max_pages": 10}


@pytest.fixture
def tight_budget(monkeypatch):
    """A small file is allowed hundreds of unlisted lookups, because each is cheap. The mechanism
    tests refuse a few hundred references, so they cap the count at 32. The default allowance is
    covered by the size-scaling, large-file and small-file acceptance tests."""
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 32)


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


def _ascii85_payload_spelling_a_forbidden_name() -> tuple[bytes, bytes]:
    """Searches page texts in a fixed order for one whose ASCII85 encoding spells a forbidden name.
    The text carries a hash of the attempt number, so every encoded group varies. No compression is
    involved, so the result does not depend on the zlib in use; the first hit is near attempt 1700."""
    for attempt in range(500_000):
        token = base64.b64encode(hashlib.sha256(b"%d" % attempt).digest())
        plain = b"BT /F1 12 Tf 72 720 Td (Datasheet " + token + b") Tj ET"
        encoded = base64.a85encode(plain) + b"~>"
        if _FORBIDDEN_NAME.search(encoded):
            return plain, encoded
    raise AssertionError("no qualifying payload found")


def test_ascii85_payload_that_spells_a_forbidden_name_by_chance_is_accepted():
    plain, encoded = _ascii85_payload_spelling_a_forbidden_name()
    content = _page_pdf(_stream(encoded, b"/Filter/ASCII85Decode"))
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
    "object_header_split_by_comments_inside_a_payload": _page_pdf(_stream(b"9 %x\n0 %x\nobj\n" + SCRIPT)),
    "object_header_split_by_a_257_byte_comment": _page_pdf(
        _stream(b"9 %" + b"x" * 257 + b"\n0 %" + b"x" * 257 + b"\nobj\n" + SCRIPT)
    ),
    "object_header_split_by_a_100000_byte_comment": _page_pdf(
        _stream(b"9 %" + b"x" * 100_000 + b"\n0 %" + b"x" * 100_000 + b"\nobj\n" + SCRIPT)
    ),
    "object_header_split_by_carriage_return_comments": _page_pdf(_stream(b"9\r%x\r0\r%x\robj\r" + SCRIPT)),
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


@pytest.mark.parametrize("name", ["forbidden_name_as_a_value", "forbidden_name_in_an_array"])
def test_object_walk_alone_refuses_forbidden_name_values(name, monkeypatch):
    """With both byte scans switched off, only the object walk can refuse a forbidden name used
    as a value or an array member."""
    monkeypatch.setattr(pdf_validation, "_has_forbidden_name", lambda data: False)
    assert _reject_code(OBJECT_STREAM_EVASIONS[name]) == "active_content"


def test_unparseable_file_gets_the_whole_file_scan():
    """Nothing is skipped when the parser fails, so a forbidden name keeps its specific reason."""
    assert _reject_code(b"%PDF-1.4 garbage <</JS(/* benign marker */)>> %%EOF") == "active_content"
    assert _reject_code(b"%PDF-1.4 garbage <</Kind/Benign>> %%EOF") == "invalid_structure"


def _hybrid_pdf(classic: dict[int, bytes], compressed: dict[int, bytes], *, stream_type: bytes = b"/Type/ObjStm", count: int | None = None) -> bytes:
    """Catalog and pages written directly in the file, other objects in one compressed object
    stream, all indexed by a cross-reference stream. The page tree stays readable when the
    object stream's own dictionary is malformed."""
    numbers = sorted(compressed)
    offsets, running = [], 0
    for number in numbers:
        offsets.append(b"%d %d" % (number, running))
        running += len(compressed[number]) + 1
    header = b" ".join(offsets) + b"\n"
    payload = zlib.compress(header + b"".join(compressed[number] + b"\n" for number in numbers))
    stream_number = max([*classic, *numbers]) + 1
    xref_number = stream_number + 1
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    where = {}
    for number, body in classic.items():
        where[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    where[stream_number] = len(out)
    out += b"%d 0 obj\n<<%s/N %d/First %d/Filter/FlateDecode/Length %d>>\nstream\n" % (
        stream_number, stream_type, len(numbers) if count is None else count, len(header), len(payload),
    )
    out += payload + b"\nendstream\nendobj\n"
    where[xref_number] = len(out)
    rows = bytearray()
    for number in range(xref_number + 1):
        if number in compressed:
            rows += bytes([2]) + stream_number.to_bytes(4, "big") + numbers.index(number).to_bytes(2, "big")
        elif number in where:
            rows += bytes([1]) + where[number].to_bytes(4, "big") + bytes(2)
        else:
            rows += bytes(7)
    xref_payload = zlib.compress(bytes(rows))
    out += b"%d 0 obj\n<</Type/XRef/Size %d/W[1 4 2]/Root 1 0 R/Filter/FlateDecode/Length %d>>\nstream\n" % (
        xref_number, xref_number + 1, len(xref_payload),
    )
    out += xref_payload + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % where[xref_number]
    return bytes(out)


_FORM = {
    1: b"<</Type/Catalog/Pages 2 0 R/AcroForm<</Fields[4 0 R]/CO[4 0 R]>>>>",
    2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>",
}
_SCRIPTED_FIELD = {4: b"<</FT/Tx/T(f)/V(1)/AA<</C" + SCRIPT + b">>>>"}
_PLAIN_FIELD = {4: b"<</FT/Tx/T(f)/V(1)>>"}
# Object-stream dictionaries the parser declines or reads as empty. Another reader may load
# the members anyway, so the calculate script in the field must still be found.
MALFORMED_OBJECT_STREAMS = {
    "type_is_an_indirect_reference": {"classic": {**_FORM, 8: b"/ObjStm"}, "stream_type": b"/Type 8 0 R"},
    "type_is_a_dangling_reference": {"stream_type": b"/Type 7 0 R"},
    "type_is_another_name": {"stream_type": b"/Type/ObjStX"},
    "type_is_missing": {"stream_type": b""},
    "member_count_is_negative": {"count": -1},
    "member_count_is_zero": {"count": 0},
    "member_count_is_too_large": {"count": 99},
}


def _malformed(name: str, field: dict[int, bytes]) -> bytes:
    options = dict(MALFORMED_OBJECT_STREAMS[name])
    return _hybrid_pdf(options.pop("classic", _FORM), field, **options)


def test_control_form_field_in_a_well_formed_object_stream():
    assert validate_pdf(_hybrid_pdf(_FORM, _PLAIN_FIELD), **LIMITS).page_count == 1
    assert _reject_code(_hybrid_pdf(_FORM, _SCRIPTED_FIELD)) == "active_content"


@pytest.mark.parametrize("name", sorted(MALFORMED_OBJECT_STREAMS))
def test_script_in_an_object_stream_the_parser_declines_is_rejected(name):
    assert _reject_code(_malformed(name, _SCRIPTED_FIELD)) == "active_content"


@pytest.mark.parametrize("name", ["type_is_a_dangling_reference", "type_is_another_name", "type_is_missing"])
def test_object_stream_member_that_cannot_be_loaded_is_refused_not_skipped(name):
    """Fail closed: the member holds nothing forbidden here, but the walk could not inspect it.
    The parser raises AssertionError for these, which must come out as a rejection."""
    assert _reject_code(_malformed(name, _PLAIN_FIELD)) == "invalid_structure"


def _timed(function, payload: bytes) -> float:
    started = time.perf_counter()
    function(payload)
    return time.perf_counter() - started


@pytest.mark.parametrize(
    "shape", ["digit_then_spaces", "digit_then_tabs", "digit_space_repeated", "two_digits_in_long_whitespace"]
)
def test_object_syntax_rule_is_linear_on_a_25_mb_payload(shape):
    """The rule runs on every stream payload of an upload, so no payload shape may make it
    expensive. The cost is compared with one plain linear scan of the same bytes on the same
    machine, so the check does not depend on its speed: the rule costs 0.3 to 2.4 times the scan,
    and the pattern it replaced cost 8 to 14 times."""
    size = 25 * 1024 * 1024
    payload = {
        "digit_then_spaces": b"9" + b" " * size,
        "digit_then_tabs": b"9" + b"\t" * size,
        "digit_space_repeated": b"1 " * (size // 2),
        "two_digits_in_long_whitespace": b"9" + b" " * (size // 2) + b"9" + b" " * (size // 2),
    }[shape] + b"obj"
    baseline = re.compile(rb"[a-z]{4}Q")
    baseline_time = min(_timed(baseline.search, payload) for _ in range(3))
    rule_time = min(_timed(pdf_validation._carries_object_syntax, payload) for _ in range(3))
    assert rule_time < 5 * baseline_time


def test_object_syntax_rule_scales_linearly_on_comment_lines():
    """Comment lines are the costliest shape. Compare sizes on the same machine so the check does
    not depend on its speed: five times the payload may cost about five times as much, a
    quadratic rule would cost twenty-five times."""
    def payload(megabytes: int) -> bytes:
        return b"%x\n" * (megabytes * 1024 * 1024 // 3) + b"obj"

    small, large = payload(5), payload(25)
    small_time = min(_timed(pdf_validation._carries_object_syntax, small) for _ in range(3))
    large_time = min(_timed(pdf_validation._carries_object_syntax, large) for _ in range(2))
    assert large_time < 12 * small_time


def _chain_of_unlisted_objects(length: int) -> bytes:
    """Object 4 lists an `/S` that points at object 5, which the cross-reference table does not
    list; each unlisted object points at the next. Resolving one makes pypdf search the whole file
    and add an entry, so the walk finds one new entry per pass."""
    chain = b"".join(b"%d 0 obj\n<</S %d 0 R>>\nendobj\n" % (number, number + 1) for number in range(5, 5 + length))
    return _page_pdf(b"<</S 5 0 R>>", after=chain)


def test_short_chain_of_unlisted_objects_is_accepted():
    assert validate_pdf(_chain_of_unlisted_objects(2), **LIMITS).page_count == 1


def test_long_chain_of_unlisted_objects_is_rejected_by_the_pass_cap(monkeypatch, tight_budget):
    content = _chain_of_unlisted_objects(60)
    assert _reject_code(content) == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_WALK_PASSES", 1000)
    assert _reject_code(content) == "too_complex"  # the lookup budget refuses it without the pass cap
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, **LIMITS).page_count == 1  # only the caps refuse it


_ANNOTATION = b"<</Type/Annot/Subtype/Text/Rect[0 0 1 1]>>"


def _annotated_page_pdf(count: int, *, listed: bool = False, padding: int = 0) -> bytes:
    """A page whose /Annots lists `count` annotations. Unlisted ones are in the file body but not
    in the cross-reference table, so resolving each makes pypdf search the whole file."""
    first = 10
    references = b" ".join(b"%d 0 R" % (first + index) for index in range(count))
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Annots[" + references + b"]>>",
        4: _stream(b"q Q" + b" " * padding),
    }
    if listed:
        objects.update({first + index: _ANNOTATION for index in range(count)})
        return _classic_pdf(objects)
    body = b"".join(b"%d 0 obj\n" % (first + index) + _ANNOTATION + b"\nendobj\n" for index in range(count))
    return _classic_pdf(objects, after=body)


@pytest.mark.parametrize("count", [1, 8, 32])
def test_a_few_unlisted_annotations_are_accepted(count):
    assert validate_pdf(_annotated_page_pdf(count), **LIMITS).page_count == 1


def test_listed_annotations_cost_no_budget():
    assert validate_pdf(_annotated_page_pdf(500, listed=True), **LIMITS).page_count == 1


def test_many_unlisted_annotations_are_rejected_by_the_lookup_budget(monkeypatch, tight_budget):
    content = _annotated_page_pdf(200)
    assert _reject_code(content) == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, **LIMITS).page_count == 1  # only the budget refuses it


def _one_lookup_seconds(content: bytes) -> float:
    """What pypdf spends on one unlisted lookup: copy the file, then search it for an object header."""
    pattern = re.compile(rb"\s5\s+0\s+obj")
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        pattern.search(bytes(bytearray(content)))
        best = min(best, time.perf_counter() - started)
    return best


def _assert_refused_within_the_allowance(content: bytes) -> int:
    """The refusal must cost about the allowed number of lookups, measured in this machine's own
    lookup time: three times (allowance + 3) lookups plus half a second for everything else."""
    allowed = pdf_validation._LookupBudget.limit_for(len(content))
    bound = 3 * (allowed + 3) * _one_lookup_seconds(content) + 0.5
    started = time.perf_counter()
    with pytest.raises(PdfRejected) as caught:
        validate_pdf(content, max_bytes=len(content) + 1, max_pages=10)
    elapsed = time.perf_counter() - started
    assert caught.value.code == "too_complex"
    assert elapsed < bound, f"{elapsed:.2f} s against {bound:.2f} s for {allowed} lookups"
    return allowed


def test_unlisted_annotations_on_a_large_file_are_refused_quickly():
    """800 unlisted annotations in a 2 MB file took 19 s: one whole-file search each."""
    allowed = _assert_refused_within_the_allowance(_annotated_page_pdf(800, padding=2 * 1024 * 1024))
    assert 40 <= allowed <= 48  # a 2 MB file gets about 48 lookups


def _action_chain_pdf(length: int) -> bytes:
    """A link annotation whose /A points at an unlisted action, each with a /Next to the next."""
    chain = b"".join(
        b"%d 0 obj\n<</S/URI/URI(https://example.com)/Next %d 0 R>>\nendobj\n" % (20 + index, 21 + index) for index in range(length)
    )
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Annots[10 0 R]>>",
        4: _stream(b"q Q"),
        10: b"<</Type/Annot/Subtype/Link/Rect[0 0 1 1]/A 20 0 R>>",
    }
    return _classic_pdf(objects, after=chain)


def test_short_chain_of_unlisted_actions_is_accepted():
    assert validate_pdf(_action_chain_pdf(3), **LIMITS).page_count == 1


def test_long_chain_of_unlisted_actions_is_rejected_by_the_lookup_budget(monkeypatch, tight_budget):
    content = _action_chain_pdf(80)
    assert _reject_code(content) == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, **LIMITS).page_count == 1  # only the budget refuses it


def _action_fan_out_pdf(width: int) -> bytes:
    """An action whose /Next is an array of `width` references to unlisted actions."""
    members = b" ".join(b"%d 0 R" % (21 + index) for index in range(width))
    body = b"20 0 obj\n<</S/URI/URI(https://example.com)/Next[" + members + b"]>>\nendobj\n"
    body += b"".join(b"%d 0 obj\n<</S/URI/URI(https://example.com)>>\nendobj\n" % (21 + index) for index in range(width))
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Annots[10 0 R]>>",
        4: _stream(b"q Q"),
        10: b"<</Type/Annot/Subtype/Link/Rect[0 0 1 1]/A 20 0 R>>",
    }
    return _classic_pdf(objects, after=body)


def test_small_fan_out_of_unlisted_actions_is_accepted():
    assert validate_pdf(_action_fan_out_pdf(4), **LIMITS).page_count == 1


def test_wide_fan_out_of_unlisted_actions_is_rejected_by_the_lookup_budget(monkeypatch, tight_budget):
    content = _action_fan_out_pdf(100)
    assert _reject_code(content) == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, **LIMITS).page_count == 1  # only the budget refuses it


def _page_tree_pdf(count: int) -> bytes:
    """A page tree whose /Kids lists `count` pages that the cross-reference table does not list."""
    kids = b" ".join(b"%d 0 R" % (10 + index) for index in range(count))
    objects = {1: b"<</Type/Catalog/Pages 2 0 R>>", 2: b"<</Type/Pages/Kids[" + kids + b"]/Count %d>>" % count}
    pages = b"".join(
        b"%d 0 obj\n<</Type/Page/Parent 2 0 R/MediaBox[0 0 9 9]>>\nendobj\n" % (10 + index) for index in range(count)
    )
    return _classic_pdf(objects, after=pages)


def test_a_few_unlisted_pages_are_accepted():
    assert validate_pdf(_page_tree_pdf(3), max_bytes=LIMITS["max_bytes"], max_pages=10).page_count == 3


def test_many_unlisted_pages_are_rejected_by_the_lookup_budget_before_pages_are_counted(monkeypatch, tight_budget):
    """The page count walks the whole tree, so the budget has to apply there too."""
    content = _page_tree_pdf(200)
    with pytest.raises(PdfRejected) as caught:
        validate_pdf(content, max_bytes=LIMITS["max_bytes"], max_pages=1000)
    assert caught.value.code == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, max_bytes=LIMITS["max_bytes"], max_pages=1000).page_count == 200  # only the budget refuses


def _dangling_type_pdf(count: int) -> bytes:
    """`count` streams whose /Type is a reference to an object the file does not contain."""
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 9 9]>>",
    }
    objects.update({10 + index: b"<</Type %d 0 R/Length 0>>\nstream\n\nendstream" % (5000 + index) for index in range(count)})
    return _classic_pdf(objects)


def test_a_few_streams_with_a_dangling_type_are_accepted():
    assert validate_pdf(_dangling_type_pdf(3), **LIMITS).page_count == 1


def test_many_streams_with_a_dangling_type_are_rejected_by_the_lookup_budget(monkeypatch, tight_budget):
    """Resolving a stream's /Type is a parser lookup like any other."""
    content = _dangling_type_pdf(200)
    assert _reject_code(content) == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, **LIMITS).page_count == 1  # only the budget refuses it


@pytest.mark.parametrize(
    ("file_size", "limit"),
    [
        (1, 1000),
        (3715, 1000),
        (102_400, 983),
        (262_144, 384),
        (1024 * 1024, 96),
        (2 * 1024 * 1024, 48),
        (8 * 1024 * 1024, 12),
        (24 * 1024 * 1024, 4),
        (25 * 1024 * 1024, 4),
        (10**9, 4),
    ],
)
def test_lookup_allowance_shrinks_as_the_file_grows(file_size, limit):
    """Each lookup scans the whole file, so the allowance is a byte budget: hundreds of lookups for
    a small file, a handful for a large one."""
    assert pdf_validation._LookupBudget.limit_for(file_size) == limit


def test_large_file_with_unlisted_annotations_is_refused_within_the_allowance():
    """24 MB allows 4 lookups. A flat 32 would cost about 10 s here and far more on slower hardware."""
    assert _assert_refused_within_the_allowance(_annotated_page_pdf(12, padding=24 * 1024 * 1024)) == 4


def _bend_xref_entries(content: bytes, shift: int) -> bytes:
    """Keep `startxref` correct and add `shift` to every in-use entry offset. pypdf then discards the
    table when it opens the file, so every object looks unlisted."""
    return re.sub(rb"(\d{10}) (\d{5}) n ", lambda m: b"%010d %s n " % (int(m.group(1)) + shift, m.group(2)), content)


@pytest.mark.parametrize("shift", [7, 13])
def test_xref_entries_with_wrong_offsets_in_a_small_file_are_accepted(shift):
    """Repairing a 4 KB file costs milliseconds, so it is not refused. With a flat cap of 32 a file
    with 50 such annotations was refused though the merged validator accepted it in 5 ms. The test
    needs pypdf to discard the table at open (true for 5.0.1, 6.17, 6.19 and 7.0.1) and skips on a
    version that repairs it instead."""
    content = _bend_xref_entries(_annotated_page_pdf(200, listed=True), shift)
    if any(PdfReader(io.BytesIO(content), strict=False).xref.values()):
        pytest.skip("this pypdf repairs the table when it opens the file, so the premise does not hold")
    assert validate_pdf(content, **LIMITS).page_count == 1


def test_many_references_to_deleted_objects_in_a_small_file_are_accepted():
    """References to deleted objects are what an incremental save leaves behind, and each costs a
    scan of a file this small, which is nothing."""
    assert validate_pdf(_dangling_type_pdf(200), **LIMITS).page_count == 1


def _stub_reader() -> SimpleNamespace:
    return SimpleNamespace(xref={0: {1: 100}, 1: {7: 200}}, xref_objStm={5: (8, 0)})


class _Reference(SimpleNamespace):
    pass


def test_object_stream_members_are_listed_only_with_generation_zero():
    """Members always have generation 0, so `5 1 R` is a different object from member `5 0 R`."""
    budget = pdf_validation._LookupBudget(_stub_reader(), 1000)
    for _ in range(budget.limit * 3):
        budget._charge(_Reference(idnum=5, generation=0))  # a member: free however often it is asked for
        budget._charge(_Reference(idnum=1, generation=0))  # listed directly
        budget._charge(_Reference(idnum=7, generation=1))  # listed under generation 1
    budget.check()
    for _ in range(budget.limit):
        budget._charge(_Reference(idnum=5, generation=1))  # nonzero generation: not a member, not listed
    with pytest.raises(PdfRejected) as caught:
        budget._charge(_Reference(idnum=5, generation=1))
    assert caught.value.code == "too_complex"
    with pytest.raises(PdfRejected):
        budget.check()  # the verdict repeats even if the parser swallowed the exception above


def _indirect_annots_pdf(pages: int, *, listed: bool = False) -> bytes:
    """`pages` pages, each with /Annots as an indirect reference to its own (empty) array. The
    reference is to the array itself, not to its members, so resolving it is one lookup per page.
    With `listed` False the arrays are in the file body but not in the cross-reference table."""
    kids = b" ".join(b"%d 0 R" % (10 + index) for index in range(pages))
    objects = {1: b"<</Type/Catalog/Pages 2 0 R>>", 2: b"<</Type/Pages/Kids[" + kids + b"]/Count %d>>" % pages}
    unlisted = b""
    for index in range(pages):
        array = 1000 + index
        objects[10 + index] = b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 9 9]/Annots %d 0 R>>" % array
        if listed:
            objects[array] = b"[]"
        else:
            unlisted += b"%d 0 obj\n[]\nendobj\n" % array
    return _classic_pdf(objects, after=unlisted)


@pytest.mark.parametrize("pages", [1, 8, 32])
def test_a_few_unlisted_annots_arrays_are_accepted(pages):
    assert validate_pdf(_indirect_annots_pdf(pages), max_bytes=LIMITS["max_bytes"], max_pages=1000).page_count == pages


def test_listed_annots_arrays_cost_no_budget():
    assert validate_pdf(_indirect_annots_pdf(300, listed=True), max_bytes=LIMITS["max_bytes"], max_pages=1000).page_count == 300


def test_many_unlisted_annots_arrays_are_rejected_by_the_lookup_budget(monkeypatch, tight_budget):
    """Resolving the /Annots value itself is a parser lookup, separate from its members."""
    content = _indirect_annots_pdf(200)
    with pytest.raises(PdfRejected) as caught:
        validate_pdf(content, max_bytes=LIMITS["max_bytes"], max_pages=1000)
    assert caught.value.code == "too_complex"
    monkeypatch.setattr(pdf_validation, "_MAX_UNLISTED_LOOKUPS", 10**6)
    assert validate_pdf(content, max_bytes=LIMITS["max_bytes"], max_pages=1000).page_count == 200  # only the budget refuses


def _repeated_reference_pdf(repeats: int) -> bytes:
    """One unlisted annotation referenced `repeats` times from the same /Annots array."""
    references = b" ".join(b"10 0 R" for _ in range(repeats))
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 9 9]/Annots[" + references + b"]>>",
    }
    return _classic_pdf(objects, after=b"10 0 obj\n" + _ANNOTATION + b"\nendobj\n")


def test_repeated_references_to_one_unlisted_object_are_charged_once(tight_budget):
    """The first lookup adds the object to the table, so asking for it again is free. Charging each
    reference would refuse 33 references to one object under the cap of 32."""
    assert validate_pdf(_repeated_reference_pdf(100), **LIMITS).page_count == 1
