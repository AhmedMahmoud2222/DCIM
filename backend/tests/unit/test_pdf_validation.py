"""Datasheet PDF structural validation, in-process and through the isolated child."""

import pytest

from app.application.catalog_documents.pdf_validation import PdfRejected, validate_pdf, validate_pdf_isolated
from tests.api._document_helpers import (
    make_blank_pdf,
    make_pdf,
    pdf_encrypted,
    pdf_with_attachment,
    pdf_with_javascript,
    pdf_with_open_action,
    to_bytes,
)

LIMITS = {"max_bytes": 5 * 1024 * 1024, "max_pages": 10}


def _reject_code(content: bytes, **overrides) -> str:
    with pytest.raises(PdfRejected) as caught:
        validate_pdf(content, **{**LIMITS, **overrides})
    return caught.value.code


def test_valid_pdf_reports_page_count():
    assert validate_pdf(make_pdf(pages=3), **LIMITS).page_count == 3


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "empty_file"),
        (b"PK\x03\x04 not a pdf", "not_pdf"),
        (b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "truncated"),
        (b"%PDF-1.4 garbage %%EOF", "invalid_structure"),
    ],
)
def test_rejects_non_pdf_and_malformed_input(content, code):
    assert _reject_code(content) == code


def test_rejects_truncated_real_pdf():
    assert _reject_code(make_pdf()[:-200]) in {"truncated", "invalid_structure"}


def test_rejects_oversize():
    assert _reject_code(make_pdf(), max_bytes=100) == "too_large"


def test_rejects_too_many_pages():
    assert _reject_code(make_pdf(pages=4), max_pages=3) == "too_many_pages"


def test_rejects_encrypted_pdf():
    assert _reject_code(pdf_encrypted()) in {"encrypted", "active_content", "invalid_structure"}


@pytest.mark.parametrize("factory", [pdf_with_javascript, pdf_with_attachment, pdf_with_open_action])
def test_rejects_active_and_embedded_content(factory):
    assert _reject_code(factory()) == "active_content"


def test_rejects_name_hidden_with_hex_escape():
    content = pdf_with_javascript().replace(b"/JavaScript", b"/Java#53cript")
    assert _reject_code(content) == "active_content"


def test_uri_links_are_allowed():
    from pypdf.annotations import Link

    writer = make_blank_pdf()
    writer.add_annotation(0, Link(rect=(10, 10, 100, 40), url="https://example.com/product"))
    assert validate_pdf(to_bytes(writer), **LIMITS).page_count == 1


def test_isolated_validation_matches_in_process_verdicts():
    assert validate_pdf_isolated(make_pdf(pages=2), **LIMITS).page_count == 2
    with pytest.raises(PdfRejected) as caught:
        validate_pdf_isolated(pdf_with_javascript(), **LIMITS)
    assert caught.value.code == "active_content"
    with pytest.raises(PdfRejected) as caught:
        validate_pdf_isolated(make_pdf(pages=4), max_bytes=LIMITS["max_bytes"], max_pages=3)
    assert caught.value.code == "too_many_pages"


def test_isolated_validation_fails_closed_when_the_child_dies(monkeypatch):
    import subprocess

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(PdfRejected) as caught:
        validate_pdf_isolated(make_pdf(), **LIMITS)
    assert caught.value.code == "too_complex"


# ------------------------------------------------------------------ Hostile-input hardening


def _uri_pdf(url: str) -> bytes:
    from pypdf.annotations import Link

    writer = make_blank_pdf()
    writer.add_annotation(0, Link(rect=(10, 10, 100, 40), url=url))
    return to_bytes(writer)


def test_uri_action_is_kept_inert_and_never_dereferenced():
    """Accepting /URI must not make the validator (or its child process) contact the URL.
    A real listening socket stands in for the target; any connection attempt would land on it."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(3)
    port = server.getsockname()[1]
    hits: list[str] = []

    def _accept() -> None:
        try:
            conn, _ = server.accept()
            hits.append("connected")
            conn.close()
        except (TimeoutError, OSError):
            pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    content = _uri_pdf(f"http://127.0.0.1:{port}/track?x=1")
    assert validate_pdf(content, **LIMITS).page_count == 1
    assert validate_pdf_isolated(content, **LIMITS).page_count == 1
    thread.join(4)
    server.close()
    assert hits == []


def test_child_process_cannot_open_sockets():
    import subprocess
    import sys

    code = (
        "from app.application.catalog_documents.pdf_validation import _block_network; _block_network();"
        "import socket\n"
        "for attempt in (lambda: socket.socket(), lambda: socket.create_connection(('127.0.0.1', 9)),"
        " lambda: socket.getaddrinfo('example.com', 80)):\n"
        "    try:\n        attempt()\n    except OSError:\n        continue\n    raise SystemExit('network call was not blocked')\n"
        "print('blocked')"
    )
    completed = subprocess.run([sys.executable, "-c", code.replace("\\n", "\n")], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "blocked"


def test_child_process_applies_cpu_and_memory_limits():
    import subprocess
    import sys

    code = (
        "import resource\n"
        "from app.application.catalog_documents import pdf_validation as v\n"
        "v._apply_child_limits()\n"
        "print(resource.getrlimit(resource.RLIMIT_CPU)[0], resource.getrlimit(resource.RLIMIT_AS)[0])"
    )
    completed = subprocess.run([sys.executable, "-c", code.replace("\\n", "\n")], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    cpu, address_space = (int(x) for x in completed.stdout.split())
    assert cpu == 15 and address_space == 1024 * 1024 * 1024


def test_stream_decompression_bomb_is_not_expanded_by_validation():
    """Validation reads structure only. A page whose content stream inflates to ~300 MB must
    validate quickly and within the child's memory limit, because nothing decodes the stream."""
    import time
    import zlib

    from pypdf.generic import DecodedStreamObject, NameObject

    writer = make_blank_pdf()
    stream = DecodedStreamObject()
    stream.set_data(b"0" * 1000)
    stream._data = zlib.compress(b"0" * (300 * 1024 * 1024), 9)
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    content = to_bytes(writer)
    assert len(content) < 1_000_000
    started = time.monotonic()
    assert validate_pdf_isolated(content, **LIMITS).page_count == 1
    assert time.monotonic() - started < 10


def test_annotation_flood_is_rejected_as_too_complex():
    from pypdf.annotations import Link
    from pypdf.generic import ArrayObject, NameObject

    writer = make_blank_pdf()
    writer.add_annotation(0, Link(rect=(1, 1, 2, 2), url="https://example.com"))
    reference = writer.pages[0]["/Annots"][0]
    writer.pages[0][NameObject("/Annots")] = ArrayObject([reference] * 20001)
    assert _reject_code(to_bytes(writer)) == "too_complex"


def test_pdf_with_large_foreign_tail_is_rejected_as_truncated():
    """A PDF/ZIP polyglot keeps its ZIP central directory at the end of the file, so the PDF
    end-of-file marker is far from the tail and the file is refused."""
    assert _reject_code(make_pdf() + b"PK\x03\x04" + b"A" * 4096) == "truncated"


def test_pdf_with_small_tail_is_accepted_but_served_as_inert_attachment():
    """A short trailer after %%EOF is legal. Such a file is still only ever served as
    application/pdf with `attachment` and `nosniff` (see the API download tests) and is scanned."""
    assert validate_pdf(make_pdf() + b"\n<script>alert(1)</script>\n", **LIMITS).page_count == 1
