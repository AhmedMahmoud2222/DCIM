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
rejection, never a hung request."""

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
_FORBIDDEN_KEYS = frozenset({"/JS", "/AA", "/OpenAction", "/XFA", "/EF", "/RichMedia"})
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


def _scan_all_objects(reader: "PdfReader") -> None:
    """Checks every parsed object, including those stored in compressed object streams that
    the raw byte scan cannot see and the page/catalog walk may not reach (outlines, fields no
    page references). Dictionary keys and `/S` action types are compared to the forbidden sets."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    xref = cast(dict, reader.xref)
    compressed = cast(dict, reader.xref_objStm)
    references = [(number, generation) for generation, entries in xref.items() for number in entries]
    references += [(number, 0) for number in compressed]
    if len(references) > _MAX_OBJECTS_SCANNED:
        raise _reject("too_complex", "The PDF contains too many objects to inspect safely.")
    for number, generation in references:
        try:
            obj = IndirectObject(number, generation, reader).get_object()
        except Exception:  # noqa: BLE001  unreadable free or broken entries are the parser's concern
            continue
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
            if not isinstance(candidate, DictionaryObject):
                continue
            if _FORBIDDEN_KEYS.intersection(candidate.keys()):
                raise _reject("active_content", "PDF contains active or embedded content (scripts, actions or attachments).")
            action_type = candidate.get("/S")
            if action_type is not None and str(action_type) in _FORBIDDEN_ACTION_TYPES:
                raise _reject("active_content", f"PDF contains a {action_type} action.")
            pending.extend(value for value in candidate.values() if not isinstance(value, IndirectObject))


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
    if _FORBIDDEN_NAME.search(_names_normalized(content)):
        raise _reject(
            "active_content", "PDF contains active or embedded content (scripts, launch actions, attachments or forms)."
        )

    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
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

        root = cast(DictionaryObject, reader.trailer["/Root"])
        if "/OpenAction" in root or "/AA" in root:
            raise _reject("active_content", "PDF defines document-level actions.")
        names = root.get("/Names")
        if names is not None:
            names_dict = cast(DictionaryObject, names.get_object())
            if "/JavaScript" in names_dict or "/EmbeddedFiles" in names_dict:
                raise _reject("active_content", "PDF contains scripts or embedded files.")
        acro_form = root.get("/AcroForm")
        if acro_form is not None and "/XFA" in cast(DictionaryObject, acro_form.get_object()):
            raise _reject("active_content", "PDF contains XFA forms.")

        _scan_all_objects(reader)

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
                annotation = cast(DictionaryObject, annotation_ref.get_object())
                if "/AA" in annotation:
                    raise _reject("active_content", f"PDF annotation on page {index + 1} defines actions.")
                if "/A" in annotation:
                    _scan_action(annotation["/A"], f"page {index + 1}")
    except PdfRejected:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, RecursionError, OSError) as exc:
        raise _reject("invalid_structure", f"The PDF could not be parsed ({type(exc).__name__}).") from exc
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
