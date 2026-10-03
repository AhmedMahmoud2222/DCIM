"""Orchestrates one extraction run: native text, OCR for the pages that need it, then analysis.

Every stage that touches PDF bytes or pixels is a sandboxed child (`sandbox.run_sandboxed_child`);
this module only moves JSON between them and decides what runs next. Failures are reported as
fixed `ExtractionFailure` codes. No child output, exception text, path or document content is
ever copied into an error, so nothing unsafe can reach a response or a log through this module."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.catalog_documents.extraction.candidates import EXTRACTOR_VERSION, UNIT_REGISTRY_VERSION
from app.application.catalog_documents.extraction.sandbox import ChildResult, run_sandboxed_child
from app.core.config import Settings

__all__ = ["EXTRACTOR_VERSION", "UNIT_REGISTRY_VERSION", "ExtractionFailure", "PipelineResult", "needs_ocr", "run_pipeline"]

_PKG = "app.application.catalog_documents.extraction"

FAILURE_MESSAGES = {
    "document_rejected": "The stored document no longer passes the structural safety checks.",
    "too_many_pages": "The document has more pages than the configured limit.",
    "native_timeout": "Reading the document took too long and was stopped.",
    "native_crashed": "The document reader stopped unexpectedly.",
    "native_output_too_large": "The document produced more text than the configured limit.",
    "native_memory": "The document needed more memory than the configured limit.",
    "native_parse_error": "The document could not be read.",
    "analysis_timeout": "Interpreting the document text took too long and was stopped.",
    "analysis_failed": "The document text could not be interpreted.",
    "ocr_timeout": "Text recognition took too long and was stopped.",
    "ocr_failed": "Text recognition failed.",
    "ocr_unavailable": "Text recognition is not installed.",
    "ocr_disabled": "Text recognition is disabled.",
    "ocr_sandbox_unavailable": "Text recognition cannot be isolated on this host, so it was not run.",
    "ocr_no_image": "The page has no image to recognise.",
    "ocr_image_too_large": "The page image is larger than the configured limit.",
    "stored_object_missing": "The stored document file is missing.",
    "stored_object_mismatch": "The stored document file does not match its recorded checksum.",
    "max_attempts_exceeded": "Extraction failed repeatedly and was stopped.",
    "internal_error": "Extraction failed unexpectedly.",
}


class ExtractionFailure(Exception):
    def __init__(self, code: str):
        self.code = code if code in FAILURE_MESSAGES else "internal_error"
        super().__init__(self.code)


@dataclass
class PipelineResult:
    candidates: list[dict[str, object]]
    model_resolution: str
    identified_models: list[str]
    warnings: list[dict[str, object]]
    pages_total: int
    pages_native: int
    pages_ocr: int
    pages_ocr_failed: int
    outcome: str  # complete | partial
    method_summary: str  # native | ocr | mixed | none
    unit_registry_version: str = UNIT_REGISTRY_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    stages: list[str] = field(default_factory=list)


def needs_ocr(page: dict[str, Any], settings: Settings) -> bool:
    """OCR runs only for a page with almost no native text that does carry an image to read."""
    return int(page.get("alnum", 0)) < settings.catalog_ocr_min_native_chars and int(page.get("images", 0)) > 0


def _json(result: ChildResult, *, timeout_code: str, crash_code: str, overflow_code: str) -> dict[str, Any]:
    if result.timed_out:
        raise ExtractionFailure(timeout_code)
    if result.output_overflow:
        raise ExtractionFailure(overflow_code)
    try:
        parsed = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ExtractionFailure(crash_code) from None  # killed by a limit, crashed, or no verdict
    if not isinstance(parsed, dict):
        raise ExtractionFailure(crash_code)
    return parsed


def run_pipeline(
    content: bytes, *, target_names: list[str], settings: Settings, heartbeat: Callable[[], None] | None = None
) -> PipelineResult:
    beat = heartbeat or (lambda: None)
    cap = 4 * settings.catalog_extraction_max_total_chars + 2 * 1024 * 1024
    native = _json(
        run_sandboxed_child(
            f"{_PKG}.native_worker",
            [
                str(settings.catalog_document_max_bytes),
                str(settings.catalog_document_max_pages),
                str(settings.catalog_extraction_max_page_chars),
                str(settings.catalog_extraction_max_total_chars),
                str(settings.catalog_extraction_native_cpu_seconds),
                str(settings.catalog_extraction_address_space_bytes),
            ],
            content,
            wall_seconds=settings.catalog_extraction_native_wall_seconds,
            stdout_cap_bytes=cap,
        ),
        timeout_code="native_timeout",
        crash_code="native_crashed",
        overflow_code="native_output_too_large",
    )
    if not native.get("ok"):
        raise ExtractionFailure(str(native.get("code", "native_parse_error")))
    beat()
    pages = [p for p in native["pages"] if isinstance(p, dict)]
    warnings: list[dict[str, object]] = [
        {"code": "page_text_truncated", "page": p["page"]} for p in pages if p.get("truncated")
    ] + [{"code": "page_unreadable", "page": p["page"]} for p in pages if p.get("error")]

    texts: list[dict[str, Any]] = []
    ocr_candidates: list[int] = []
    for page in pages:
        if needs_ocr(page, settings):
            ocr_candidates.append(int(page["page"]))
        elif int(page.get("alnum", 0)) > 0:
            texts.append({"page": page["page"], "text": page["text"], "method": "native", "ocr_confidence": None})
    pages_native = len(texts)

    ocr_ok = ocr_failed = 0
    first_ocr_failure: str | None = None
    if ocr_candidates:
        usable = _ocr_unavailable_reason(settings)
        to_run = ocr_candidates[: settings.catalog_ocr_max_pages]
        for skipped in ocr_candidates[settings.catalog_ocr_max_pages :]:
            warnings.append({"code": "ocr_page_limit", "page": skipped})
            ocr_failed += 1
        if usable is not None:
            for page_number in to_run:
                warnings.append({"code": usable, "page": page_number})
            ocr_failed += len(to_run)
            first_ocr_failure = usable
        else:
            for page_number in to_run:
                beat()
                outcome = _ocr_page(content, page_number, settings)
                if outcome.get("ok"):
                    ocr_ok += 1
                    texts.append(
                        {
                            "page": page_number,
                            "text": outcome["text"],
                            "method": "ocr",
                            "ocr_confidence": outcome["mean_confidence"],
                        }
                    )
                else:
                    ocr_failed += 1
                    code = str(outcome.get("code", "ocr_failed"))
                    code = code if code in FAILURE_MESSAGES else "ocr_failed"
                    first_ocr_failure = first_ocr_failure or code
                    warnings.append({"code": code, "page": page_number})
    beat()
    texts.sort(key=lambda t: int(t["page"]))

    analysis = _json(
        run_sandboxed_child(
            f"{_PKG}.analysis_worker",
            [str(settings.catalog_extraction_native_cpu_seconds), str(settings.catalog_extraction_address_space_bytes)],
            json.dumps({"pages": texts, "target_names": target_names}).encode("utf-8"),
            wall_seconds=settings.catalog_extraction_analysis_wall_seconds,
            stdout_cap_bytes=16 * 1024 * 1024,
        ),
        timeout_code="analysis_timeout",
        crash_code="analysis_failed",
        overflow_code="analysis_failed",
    )
    if not analysis.get("ok"):
        raise ExtractionFailure("analysis_failed")
    candidates = [c for c in analysis["candidates"] if isinstance(c, dict)]
    warnings += [w for w in analysis.get("warnings", []) if isinstance(w, dict)]

    if ocr_candidates and ocr_ok == 0 and not candidates:
        # The document needed OCR, none of it worked and nothing else was read: say so as a failure
        # instead of reporting an empty "completed" result that looks like a datasheet with no data.
        raise ExtractionFailure(first_ocr_failure or "ocr_failed")
    method = "none" if not (pages_native or ocr_ok) else "native" if not ocr_ok else "ocr" if not pages_native else "mixed"
    return PipelineResult(
        candidates=candidates,
        model_resolution=str(analysis["model_resolution"]),
        identified_models=[str(m) for m in analysis["identified_models"]],
        warnings=warnings,
        pages_total=len(pages),
        pages_native=pages_native,
        pages_ocr=ocr_ok,
        pages_ocr_failed=ocr_failed,
        outcome="partial"
        if ocr_failed or any(w.get("code") in {"page_unreadable", "page_text_truncated"} for w in warnings)
        else "complete",
        method_summary=method,
        stages=["native", "ocr" if ocr_candidates else "ocr_skipped", "analysis"],
    )


def _ocr_unavailable_reason(settings: Settings) -> str | None:
    if not settings.catalog_ocr_enabled:
        return "ocr_disabled"
    if not (os.path.isabs(settings.catalog_ocr_tesseract_path) and os.access(settings.catalog_ocr_tesseract_path, os.X_OK)):
        return "ocr_unavailable"
    return None


def _ocr_page(content: bytes, page_number: int, settings: Settings) -> dict[str, Any]:
    timeout = settings.catalog_ocr_page_timeout_seconds
    try:
        result = _json(
            run_sandboxed_child(
                f"{_PKG}.ocr_worker",
                [
                    str(page_number),
                    str(settings.catalog_document_max_bytes),
                    str(settings.catalog_document_max_pages),
                    settings.catalog_ocr_tesseract_path,
                    settings.catalog_ocr_language,
                    str(timeout),
                    str(settings.catalog_ocr_max_image_pixels),
                    str(settings.catalog_extraction_max_page_chars),
                    str(timeout + 15),
                    str(settings.catalog_extraction_address_space_bytes),
                ],
                content,
                wall_seconds=timeout + 20,
                stdout_cap_bytes=2 * 1024 * 1024,
            ),
            timeout_code="ocr_timeout",
            crash_code="ocr_failed",
            overflow_code="ocr_failed",
        )
    except ExtractionFailure as exc:
        return {"ok": False, "code": exc.code}
    return result
