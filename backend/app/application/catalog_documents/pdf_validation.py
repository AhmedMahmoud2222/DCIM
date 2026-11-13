"""Structural validation of an uploaded datasheet before anything is stored or scanned for
malware. Content-sniffed (never trusts the extension or declared content type), size- and
page-capped, and strict about active content: a datasheet is static reference material, so a
file carrying JavaScript, launch actions, embedded files, XFA forms or rich media is rejected
rather than sanitized.

`/URI` link actions are deliberately NOT rejected: manufacturer datasheets routinely link to
product pages, and a URI action executes nothing on the server. The download endpoint serves
the file as an attachment with `nosniff`, so a link is only followed by a person who opens
the file and clicks it.

`pypdf` has had denial-of-service defects on malformed input, so the API never parses in its
own process. `validate_pdf_isolated()` runs this module as a child interpreter with CPU and
address-space rlimits and a wall-clock timeout; an over-limit or crashing parse becomes a
rejection, never a hung request.

Forbidden names are looked for in three places, and a file must pass all three:

1. Every object the parser reaches through the cross-reference table, including objects in
   compressed object streams: dictionary keys, name values and `/S` action types. A member of
   an object stream that the parser cannot load is a rejection, never a skipped entry.
2. The decoded bytes of every object stream, so a member the cross-reference table does not
   list is still seen. A stream counts as an object stream when the cross-reference table
   uses it as one or when its `/Type` says so; the parser's own opinion of it is not asked.
3. The raw file bytes, minus stream payloads. A payload is compressed or binary data in
   which `/JS` or `/AA` appear by chance (ASCII85 uses `/` and letters), so scanning it
   rejects benign files. Only the exact bytes the parser itself consumed as a stream's data
   are skipped, never a span guessed from the `stream`/`endstream` keywords, and a payload
   that carries object syntax (`endobj`, `endstream`, an `N G obj` header, with comments of any
   length between its parts) is not skipped."""

import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pypdf import PdfReader

# Names whose presence means active or attachable content. `(?![A-Za-z0-9_.\-])` keeps `/JS`
# from matching `/JSON`-style names and `/AA` from matching longer names.
_FORBIDDEN_NAME = re.compile(
    rb"/(JavaScript|JS|Launch|OpenAction|AA|EmbeddedFiles?|XFA|RichMedia|SubmitForm|ImportData|GoToR|GoToE)"
    rb"(?![A-Za-z0-9_.\-])"
)
_NAME_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_FORBIDDEN_ACTION_TYPES = frozenset(
    {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition", "/Sound", "/Movie"}
)
_MAX_ANNOTATIONS_SCANNED = 20000
_MAX_OBJECTS_SCANNED = 200000
_MAX_NODES_PER_OBJECT = 50000
# Loading an object can make the parser add cross-reference entries (resolving a reference to
# an unlisted object makes it search the whole file for the header). A repair that is still
# adding entries after this many passes is a chain built to make the walk cost a file scan per
# object.
_MAX_WALK_PASSES = 5
# The names `_FORBIDDEN_NAME` matches, as the parser spells them (it decodes `#xx` escapes).
_FORBIDDEN_NAMES = frozenset(
    {
        "/JavaScript", "/JS", "/Launch", "/OpenAction", "/AA", "/EmbeddedFile", "/EmbeddedFiles", "/XFA",
        "/RichMedia", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE",
    }
)
_FORBIDDEN_KEYS = _FORBIDDEN_NAMES | {"/EF"}
_PDF_WHITESPACE = rb"[\x00\t\n\x0c\r ]"
# Object syntax inside a stream payload. Compressed or binary data does not spell these by
# chance (six or more fixed bytes), so a payload that contains one is scanned like the rest
# of the file: it may be a second set of objects aimed at a reader that rebuilds a broken
# cross-reference table by searching the file for `N G obj`. A comment may sit between the
# parts of a header, whatever its length, so comments are cut out first (one linear pass) and
# the header pattern uses possessive quantifiers so no input makes it backtrack.
_COMMENT = re.compile(rb"%[^\r\n]*")
_OBJECT_HEADER = re.compile(rb"\d" + _PDF_WHITESPACE + rb"++\d++" + _PDF_WHITESPACE + rb"++obj")
_STREAM_KEYWORD_BEFORE_PAYLOAD = re.compile(rb"(?<!end)stream(?:\r\n|\n|\r)\Z")
_ENDSTREAM_AFTER_PAYLOAD = re.compile(_PDF_WHITESPACE + rb"{0,4}endstream")
_TRAILER_WINDOW = 1024

CHILD_TIMEOUT_SECONDS = 20
CHILD_CPU_SECONDS = 15
CHILD_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024


class PdfRejected(Exception):
    """`code` is a stable machine-readable reason; `reason` is safe to show a user (it never
    echoes file content)."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PdfInfo:
    page_count: int


def _reject(code: str, reason: str) -> PdfRejected:
    return PdfRejected(code, reason)


def _names_normalized(content: bytes) -> bytes:
    """Decodes `#4A`-style name escapes so `/Java#53cript` cannot hide from the byte scan."""
    return _NAME_HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), content)


def _scan_action(action: object, where: str) -> None:
    resolved = action.get_object() if hasattr(action, "get_object") else action
    if not hasattr(resolved, "get"):
        return
    action_type = resolved.get("/S")
    if action_type is not None and str(action_type) in _FORBIDDEN_ACTION_TYPES:
        raise _reject("active_content", f"PDF contains a {action_type} action ({where}).")
    following = resolved.get("/Next")
    if following is not None:
        following = following.get_object()
        for item in following if isinstance(following, list) else [following]:
            _scan_action(item, where)


def _has_forbidden_name(data: bytes) -> bool:
    return _FORBIDDEN_NAME.search(_names_normalized(data)) is not None


def _carries_object_syntax(payload: bytes) -> bool:
    """True when a stream payload holds `endobj`, `endstream` or an `N G obj` header, with
    whitespace or comments of any length between the parts. Linear in the payload size."""
    if b"endobj" in payload or b"endstream" in payload:
        return True
    if b"obj" not in payload:
        return False
    return _OBJECT_HEADER.search(_COMMENT.sub(b" ", payload)) is not None


def _scan_decoded_object_stream(stream: object) -> None:
    """An object stream holds dictionaries, so its decoded bytes get the same name scan as the
    file itself. This sees members the cross-reference table does not list, which the object
    walk never visits but a reader that repairs the file may load."""
    try:
        decoded = stream.get_data()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001  an undecodable object stream is refused, whatever the decoder raised
        raise _reject("invalid_structure", f"The PDF could not be parsed ({type(exc).__name__}).") from exc
    if isinstance(decoded, bytes) and _has_forbidden_name(decoded):
        raise _reject("active_content", "PDF contains active or embedded content (scripts, actions or attachments).")


def _scan_all_objects(reader: "PdfReader") -> list[tuple[int, bytes]]:
    """Checks every parsed object, including those stored in compressed object streams that
    the raw byte scan cannot see and the page/catalog walk may not reach (outlines, fields no
    page references). Dictionary keys, name values and `/S` action types are compared to the
    forbidden sets, so a forbidden name in any dictionary the parser read is refused here
    without relying on the byte scan.

    An entry the parser cannot load is handled by where its bytes are. An object written
    directly in the file stays in the byte scan, so it is skipped here. A member of an object
    stream is visible nowhere else, so the file is refused. The parser declines an object
    stream for reasons another reader may not share (a `/Type` it does not like, for one).

    Returns `(file offset of the object, raw payload)` for every stream object, for
    `_outside_stream_payloads()`."""
    xref = cast(dict, reader.xref)
    compressed = cast(dict, reader.xref_objStm)
    payloads: list[tuple[int, bytes]] = []
    seen: set[tuple[int, int]] = set()
    passes = 0
    while True:
        # Loading an object can make the parser add entries (it repairs as it reads), so the
        # table is read again until a pass finds nothing new, for at most _MAX_WALK_PASSES.
        references = _unseen_references(xref, compressed, seen)
        if not references:
            return payloads
        passes += 1
        if passes > _MAX_WALK_PASSES:
            raise _reject("too_complex", "The PDF cross-reference table keeps changing while it is read.")
        if len(seen) + len(references) > _MAX_OBJECTS_SCANNED:
            raise _reject("too_complex", "The PDF contains too many objects to inspect safely.")
        containers = {entry[0] for entry in list(compressed.values())}
        for number, generation, offset in references:
            seen.add((number, generation))
            payload = _inspect_reference(reader, number, generation, offset, generation == 0 and number in containers)
            if payload is not None:
                payloads.append(payload)


def _unseen_references(xref: dict, compressed: dict, seen: set[tuple[int, int]]) -> list[tuple[int, int, object]]:
    """Cross-reference entries not yet inspected. A number listed both directly and as an
    object-stream member counts as a member (offset `None`), the stricter rule."""
    entries: dict[tuple[int, int], object] = {}
    for generation, table in list(xref.items()):
        for number, offset in list(table.items()):
            entries[(number, generation)] = offset
    for number in list(compressed):
        entries[(number, 0)] = None
    return [(number, generation, offset) for (number, generation), offset in entries.items() if (number, generation) not in seen]


def _inspect_reference(
    reader: "PdfReader", number: int, generation: int, offset: object, is_container: bool
) -> tuple[int, bytes] | None:
    """Loads and checks one entry. Returns `(offset, raw payload)` when it is a stream the byte
    scan may skip, else `None`."""
    from pypdf.generic import IndirectObject, StreamObject

    try:
        obj = IndirectObject(number, generation, reader).get_object()
    except Exception as exc:  # noqa: BLE001  whatever the parser raised, the entry was not inspected
        if offset is None or is_container:
            raise _reject("invalid_structure", "The PDF holds an object stream that could not be read.") from exc
        return None
    if is_container and not isinstance(obj, StreamObject):
        raise _reject("invalid_structure", "The PDF holds an object stream that could not be read.")
    payload = None
    if isinstance(obj, StreamObject):
        raw = getattr(obj, "_data", None)
        if isinstance(raw, bytes) and raw and isinstance(offset, int) and offset >= 0:
            payload = (offset, raw)
        if is_container or _declared_type(obj) == "/ObjStm":
            _scan_decoded_object_stream(obj)
    _scan_object(obj)
    return payload


def _declared_type(stream: object) -> object:
    """The stream's `/Type`, followed through an indirect reference."""
    try:
        declared = stream.get("/Type")  # type: ignore[attr-defined]
        return declared.get_object() if hasattr(declared, "get_object") else declared
    except Exception:  # noqa: BLE001  an unreadable /Type is not an object-stream declaration
        return None


def _scan_object(obj: object) -> None:
    """Walks one object and its direct children for forbidden keys, names and action types."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

    pending = [obj]
    visited = 0
    while pending:
        candidate = pending.pop()
        visited += 1
        if visited > _MAX_NODES_PER_OBJECT:
            raise _reject("too_complex", "The PDF contains too deeply nested objects to inspect safely.")
        if isinstance(candidate, ArrayObject):
            # Indirect members are visited as objects of their own; only direct ones nest here.
            pending.extend(item for item in candidate if not isinstance(item, IndirectObject))
            continue
        if isinstance(candidate, NameObject) and candidate in _FORBIDDEN_NAMES:
            raise _reject("active_content", "PDF contains active or embedded content (scripts, actions or attachments).")
        if not isinstance(candidate, DictionaryObject):
            continue
        if _FORBIDDEN_KEYS.intersection(candidate.keys()):
            raise _reject("active_content", "PDF contains active or embedded content (scripts, actions or attachments).")
        action_type = candidate.get("/S")
        if action_type is not None and str(action_type) in _FORBIDDEN_ACTION_TYPES:
            raise _reject("active_content", f"PDF contains a {action_type} action.")
        pending.extend(value for value in candidate.values() if not isinstance(value, IndirectObject))


def _outside_stream_payloads(content: bytes, payloads: list[tuple[int, bytes]]) -> bytes:
    """The file with stream payloads cut out, for the forbidden-name byte scan.

    A payload is cut only where all of these hold; anything else stays in the scan:

    - the bytes are what the parser read as that stream's data (not a span between keywords,
      so a `stream` keyword written inside a string or a comment hides nothing);
    - they are the first occurrence at or after the object's own offset, directly after
      `stream` + end-of-line and directly before `endstream`. That occurrence is either the
      real payload or a copy of it inside the object's own dictionary. A copy leaves the real
      payload, with the same bytes, in the scan, and the object walk covers the dictionary;
    - they contain no object syntax (see `_carries_object_syntax`), which also covers a
      `/Length` that reaches past the stream's own `endstream` to swallow a later object."""
    spans: list[tuple[int, int]] = []
    for offset, payload in payloads:
        if _carries_object_syntax(payload):
            continue
        start = content.find(payload, offset)
        if start < 0:
            continue
        end = start + len(payload)
        if _STREAM_KEYWORD_BEFORE_PAYLOAD.search(content[max(0, start - 12) : start]) and _ENDSTREAM_AFTER_PAYLOAD.match(
            content, end
        ):
            spans.append((start, end))
    pieces: list[bytes] = []
    cursor = 0
    for start, end in sorted(spans):
        if start > cursor:
            pieces.append(content[cursor:start])
        cursor = max(cursor, end)
    pieces.append(content[cursor:])
    return b"".join(pieces)


def _check_catalog(root: object) -> None:
    """Document-level actions, name-tree scripts and attachments, and XFA forms."""
    if "/OpenAction" in root or "/AA" in root:  # type: ignore[operator]
        raise _reject("active_content", "PDF defines document-level actions.")
    names = root.get("/Names")  # type: ignore[attr-defined]
    if names is not None:
        names_dict = names.get_object()
        if "/JavaScript" in names_dict or "/EmbeddedFiles" in names_dict:
            raise _reject("active_content", "PDF contains scripts or embedded files.")
    acro_form = root.get("/AcroForm")  # type: ignore[attr-defined]
    if acro_form is not None and "/XFA" in acro_form.get_object():
        raise _reject("active_content", "PDF contains XFA forms.")


def _check_pages(reader: "PdfReader") -> None:
    """Page-level and annotation-level actions."""
    scanned = 0
    for index, page in enumerate(reader.pages):
        if "/AA" in page:
            raise _reject("active_content", f"PDF page {index + 1} defines page actions.")
        annotations = page.get("/Annots")
        if annotations is None:
            continue
        for annotation_ref in cast(list, annotations.get_object()):
            scanned += 1
            if scanned > _MAX_ANNOTATIONS_SCANNED:
                raise _reject("too_complex", "The PDF contains too many annotations to inspect safely.")
            annotation = annotation_ref.get_object()
            if "/AA" in annotation:
                raise _reject("active_content", f"PDF annotation on page {index + 1} defines actions.")
            if "/A" in annotation:
                _scan_action(annotation["/A"], f"page {index + 1}")


def validate_pdf(content: bytes, *, max_bytes: int, max_pages: int) -> PdfInfo:
    """Raises PdfRejected. Runs in the current process; API callers use
    `validate_pdf_isolated()`."""
    if not content:
        raise _reject("empty_file", "The uploaded file is empty.")
    if len(content) > max_bytes:
        raise _reject("too_large", f"The uploaded file exceeds the maximum allowed size of {max_bytes} bytes.")
    if content[:5] != b"%PDF-":
        raise _reject("not_pdf", "File content is not recognized as a PDF (checked by content, not filename).")
    if b"%%EOF" not in content[-_TRAILER_WINDOW:]:
        raise _reject("truncated", "The PDF is truncated or malformed (no end-of-file marker).")

    from pypdf import PdfReader
    from pypdf.generic import DictionaryObject

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise _reject("encrypted", "Encrypted or password-protected PDFs are not accepted.")
        page_count = len(reader.pages)
        if page_count < 1:
            raise _reject("no_pages", "The PDF contains no pages.")
        if page_count > max_pages:
            raise _reject("too_many_pages", f"The PDF has {page_count} pages; the maximum allowed is {max_pages}.")

        _check_catalog(cast(DictionaryObject, reader.trailer["/Root"]))
        stream_payloads = _scan_all_objects(reader)
        _check_pages(reader)
    except PdfRejected:
        raise
    except Exception as exc:  # noqa: BLE001  the parser raises more than its own error types (AssertionError, for one)
        # A file the parser cannot read is refused either way; one that also names active
        # content keeps the more specific reason. No payload is skipped here.
        if _has_forbidden_name(content):
            raise _reject(
                "active_content", "PDF contains active or embedded content (scripts, launch actions, attachments or forms)."
            ) from exc
        raise _reject("invalid_structure", f"The PDF could not be parsed ({type(exc).__name__}).") from exc
    if _has_forbidden_name(_outside_stream_payloads(content, stream_payloads)):
        raise _reject(
            "active_content", "PDF contains active or embedded content (scripts, launch actions, attachments or forms)."
        )
    return PdfInfo(page_count=page_count)


def validate_pdf_isolated(content: bytes, *, max_bytes: int, max_pages: int) -> PdfInfo:
    """Cheap checks run in-process (they cannot hang); the pypdf parse runs in a child
    interpreter under rlimits and a timeout."""
    if not content:
        raise _reject("empty_file", "The uploaded file is empty.")
    if len(content) > max_bytes:
        raise _reject("too_large", f"The uploaded file exceeds the maximum allowed size of {max_bytes} bytes.")
    if content[:5] != b"%PDF-":
        raise _reject("not_pdf", "File content is not recognized as a PDF (checked by content, not filename).")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-controlled arguments
            [sys.executable, "-m", "app.application.catalog_documents.pdf_validation", str(max_bytes), str(max_pages)],
            input=content, capture_output=True, timeout=CHILD_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _reject("too_complex", "The PDF took too long to inspect and was rejected.") from exc
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # Killed by an rlimit, crashed, or produced no verdict: fail closed.
        raise _reject("too_complex", "The PDF could not be inspected safely and was rejected.") from exc
    if result.get("ok"):
        return PdfInfo(page_count=int(result["page_count"]))
    raise _reject(str(result.get("code", "invalid_structure")), str(result.get("reason", "The PDF was rejected.")))


def _apply_child_limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_ADDRESS_SPACE_BYTES, CHILD_ADDRESS_SPACE_BYTES))
    except (ImportError, ValueError, OSError):  # pragma: no cover - non-POSIX platforms
        pass


def _block_network() -> None:
    """The child needs no network. Refusing socket use makes it structurally impossible for a
    PDF (for example a `/URI` action or an external reference) to trigger an outbound request
    from the parser, whatever the library does."""
    import socket

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network access is disabled in the PDF validation process")

    class _BlockedSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            _refuse()

    socket.socket = _BlockedSocket  # type: ignore[misc, assignment]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]
    socket.gethostbyname = _refuse  # type: ignore[assignment]


def _child_main() -> None:
    _apply_child_limits()
    _block_network()
    max_bytes, max_pages = int(sys.argv[1]), int(sys.argv[2])
    content = sys.stdin.buffer.read()
    try:
        info = validate_pdf(content, max_bytes=max_bytes, max_pages=max_pages)
        verdict: dict[str, object] = {"ok": True, "page_count": info.page_count}
    except PdfRejected as exc:
        verdict = {"ok": False, "code": exc.code, "reason": exc.reason}
    sys.stdout.write(json.dumps(verdict))


if __name__ == "__main__":
    _child_main()
