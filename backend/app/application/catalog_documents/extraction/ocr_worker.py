"""Sandboxed child: OCR of one page.

The strongest isolation in this feature, because it executes a native binary on attacker-controlled
pixels. Before it reads any input it installs the rlimits, blocks Python's socket module and installs
the seccomp filter that denies socket creation; the filter is inherited by the `tesseract` process it
spawns, so the OCR engine cannot open a connection either. If the filter cannot be installed here the
child reports `ocr_sandbox_unavailable` and does nothing: OCR fails closed.

Only the largest page-level image XObject is OCR'd, decoded by Pillow under a pixel cap, written to a
private temporary directory as PNG and read by tesseract with a fixed command line (no user-controlled
argument, language from settings, no shell). It never renders the page, so no PDF content stream or
action is executed. The child's environment holds no secrets (see `sandbox.run_sandboxed_child`).

argv: page_number max_bytes max_pages tesseract_path language tesseract_timeout max_pixels max_chars
      cpu_seconds address_space_bytes"""

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from typing import Any

from app.application.catalog_documents.extraction.sandbox import SandboxUnavailable, enter_sandbox
from app.application.catalog_documents.pdf_validation import PdfRejected, validate_pdf


def _verdict(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def _largest_image_name(page: object, max_pixels: int) -> tuple[str | None, str | None]:
    """(name, failure code). Declared dimensions are compared to the cap before anything decodes."""
    best: tuple[int, str] | None = None
    oversize = False
    resources = page.get("/Resources")  # type: ignore[attr-defined]
    resources = resources.get_object() if resources is not None else None
    xobjects: Any = resources.get("/XObject") if resources is not None else None
    xobjects = xobjects.get_object() if xobjects is not None else None
    for name in list(xobjects.keys())[:200] if xobjects is not None else []:
        item = xobjects[name].get_object()
        if item.get("/Subtype") != "/Image":
            continue
        pixels = int(item.get("/Width", 0)) * int(item.get("/Height", 0))
        if pixels <= 0:
            continue
        if pixels > max_pixels:
            oversize = True
            continue
        if best is None or pixels > best[0]:
            best = (pixels, str(name))
    if best is not None:
        return best[1], None
    return None, "ocr_image_too_large" if oversize else "ocr_no_image"


def _tsv_to_text(tsv: str, max_chars: int) -> tuple[str, float]:
    """Rebuilds text lines from tesseract's word boxes. A horizontal gap wider than twice the line
    height becomes a run of spaces, which is how the table parser recognises columns."""
    lines: dict[tuple[str, str, str], list[tuple[int, int, int, str]]] = {}
    confidences: list[float] = []
    for row in tsv.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            conf = float(parts[10])
            left, width, height = int(parts[6]), int(parts[8]), int(parts[9])
        except ValueError:
            continue
        if conf < 0:
            continue
        confidences.append(conf)
        lines.setdefault((parts[2], parts[3], parts[4]), []).append((left, width, height, parts[11].strip()))
    out: list[str] = []
    for words in lines.values():
        words.sort()
        line_height = statistics.median([w[2] for w in words]) or 1
        text = words[0][3]
        for previous, current in zip(words, words[1:], strict=False):
            gap = current[0] - (previous[0] + previous[1])
            text += ("   " if gap > 2 * line_height else " ") + current[3]
        out.append(text)
        if sum(len(line) + 1 for line in out) > max_chars:
            break
    mean = statistics.fmean(confidences) if confidences else 0.0
    return "\n".join(out)[:max_chars], mean


def main() -> None:
    (
        page_number,
        max_bytes,
        max_pages,
        tesseract_path,
        language,
        tesseract_timeout,
        max_pixels,
        max_chars,
        cpu,
        address_space,
    ) = (
        sys.argv[1],
        int(sys.argv[2]),
        int(sys.argv[3]),
        sys.argv[4],
        sys.argv[5],
        int(sys.argv[6]),
        int(sys.argv[7]),
        int(sys.argv[8]),
        int(sys.argv[9]),
        int(sys.argv[10]),
    )
    try:
        enter_sandbox(cpu_seconds=cpu, address_space_bytes=address_space, require_seccomp=True)
    except SandboxUnavailable:
        _verdict({"ok": False, "code": "ocr_sandbox_unavailable"})
        return
    content = sys.stdin.buffer.read(max_bytes + 1)
    try:
        validate_pdf(content, max_bytes=max_bytes, max_pages=max_pages)
    except PdfRejected:
        _verdict({"ok": False, "code": "document_rejected"})
        return

    import io

    from PIL import Image
    from pypdf import PdfReader

    Image.MAX_IMAGE_PIXELS = max_pixels
    workdir = tempfile.mkdtemp(prefix="dcim-ocr-")
    try:
        os.chmod(workdir, 0o700)
        try:
            reader = PdfReader(io.BytesIO(content), strict=False)
            index = int(page_number) - 1
            if index < 0 or index >= min(len(reader.pages), max_pages):
                _verdict({"ok": False, "code": "ocr_failed"})
                return
            page = reader.pages[index]
            name, failure = _largest_image_name(page, max_pixels)
            if name is None:
                _verdict({"ok": False, "code": failure})
                return
            picture = page.images[name].image
            if picture is None:
                _verdict({"ok": False, "code": "ocr_failed"})
                return
            picture.convert("L").save(os.path.join(workdir, "page.png"), format="PNG")
        except (MemoryError, Image.DecompressionBombError):
            _verdict({"ok": False, "code": "ocr_image_too_large"})
            return
        except Exception:  # noqa: BLE001 - fixed code only; the message may contain file content
            _verdict({"ok": False, "code": "ocr_failed"})
            return

        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv; path and language come from settings, never the PDF
                [tesseract_path, "page.png", "stdout", "-l", language, "--psm", "6", "tsv"],
                cwd=workdir,
                env={"PATH": "/usr/bin:/bin", "OMP_THREAD_LIMIT": "1", "LC_ALL": "C.UTF-8"},
                capture_output=True,
                timeout=tesseract_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _verdict({"ok": False, "code": "ocr_timeout"})
            return
        except OSError:
            _verdict({"ok": False, "code": "ocr_unavailable"})
            return
        if completed.returncode != 0 or len(completed.stdout) > 20 * 1024 * 1024:
            _verdict({"ok": False, "code": "ocr_failed"})
            return
        text, mean = _tsv_to_text(completed.stdout.decode("utf-8", errors="replace"), max_chars)
        _verdict({"ok": True, "page": int(page_number), "text": text, "mean_confidence": round(mean, 2)})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
