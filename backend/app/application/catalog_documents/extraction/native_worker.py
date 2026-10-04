"""Sandboxed child: native text extraction.

Reads the PDF from stdin and writes one JSON verdict to stdout. Runs under CPU and address-space
limits with the network blocked. It re-runs the PR-A structural validation first (the stored object
is treated as hostile, not as "already validated") and counts the pages it actually iterates,
because the page tree's declared count is attacker-controlled.

argv: max_bytes max_pages max_page_chars max_total_chars cpu_seconds address_space_bytes require_landlock"""

import json
import sys
from typing import Any

from pypdf import PdfReader

from app.application.catalog_documents.extraction.sandbox import SandboxUnavailable, enter_sandbox
from app.application.catalog_documents.pdf_validation import PdfRejected, validate_pdf


def _image_stats(page: object) -> tuple[int, int]:
    """(number of page-level image XObjects, largest declared pixel area). Declared sizes only:
    nothing is decoded here."""
    count = largest = 0
    try:
        resources = page.get("/Resources")  # type: ignore[attr-defined]
        resources = resources.get_object() if resources is not None else None
        xobjects: Any = resources.get("/XObject") if resources is not None else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        for name in list(xobjects.keys())[:200] if xobjects is not None else []:
            item = xobjects[name].get_object()
            if item.get("/Subtype") == "/Image":
                count += 1
                largest = max(largest, int(item.get("/Width", 0)) * int(item.get("/Height", 0)))
    except Exception:  # noqa: BLE001 - a malformed resource dictionary just means "no usable image"
        pass
    return count, largest


def _verdict(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def main() -> None:
    max_bytes, max_pages, max_page_chars, max_total_chars, cpu, address_space = (int(v) for v in sys.argv[1:7])
    landlock = sys.argv[7] == "1"
    try:
        enter_sandbox(cpu_seconds=cpu, address_space_bytes=address_space, require_seccomp=False, require_landlock=landlock)
    except SandboxUnavailable:
        _verdict({"ok": False, "code": "sandbox_unavailable", "detail": ""})
        return
    content = sys.stdin.buffer.read(max_bytes + 1)
    try:
        validate_pdf(content, max_bytes=max_bytes, max_pages=max_pages)
    except PdfRejected as exc:
        _verdict({"ok": False, "code": "document_rejected", "detail": exc.code})
        return

    pages: list[dict[str, object]] = []
    total = 0
    try:
        import io

        reader = PdfReader(io.BytesIO(content), strict=False)
        for index, page in enumerate(reader.pages):
            if index >= max_pages:
                _verdict({"ok": False, "code": "too_many_pages", "detail": "iterated"})
                return
            image_count, largest_pixels = _image_stats(page)
            entry: dict[str, object] = {
                "page": index + 1,
                "text": "",
                "alnum": 0,
                "images": image_count,
                "largest_image_pixels": largest_pixels,
                "truncated": False,
                "error": False,
            }
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except Exception:  # noqa: BLE001 - one unreadable page must not lose the others
                text = ""
                entry["error"] = True
            if len(text) > max_page_chars:
                text, entry["truncated"] = text[:max_page_chars], True
            if total + len(text) > max_total_chars:
                text, entry["truncated"] = text[: max(0, max_total_chars - total)], True
            total += len(text)
            entry["text"] = text
            entry["alnum"] = sum(1 for ch in text if ch.isalnum())
            pages.append(entry)
    except MemoryError:
        _verdict({"ok": False, "code": "native_memory", "detail": ""})
        return
    except Exception:  # noqa: BLE001 - reported as a fixed code; the exception text may contain file content
        _verdict({"ok": False, "code": "native_parse_error", "detail": ""})
        return
    _verdict({"ok": True, "pages": pages})


if __name__ == "__main__":
    main()
