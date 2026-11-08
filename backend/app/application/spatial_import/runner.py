"""Parent side of the isolated parse: spawns the sandboxed worker, bounds its time/output, then re-validates
whatever it returned. A malformed, hostile, slow, crashing or over-producing parse yields a `ParseFailure`
with a stable code; it never yields a partial SIR."""

import json
from dataclasses import dataclass

from app.application.catalog_documents.extraction.sandbox import run_sandboxed_child
from app.application.spatial_import.limits import DEFAULT_LIMITS, ParserLimits
from app.application.spatial_import.sir import SirDocument, SirInvalid, validate_sir

_MODULE = "app.application.spatial_import.worker"

FAILURE_MESSAGES = {
    "timeout": "The file took too long to process and was stopped.",
    "crash": "The file could not be processed (the parser was stopped by a resource limit or crashed).",
    "sir_oversized": "The file produced more geometry than the importer allows.",
    "sandbox_unavailable": "The isolated parser is unavailable on this host.",
    "file_too_large": "The file exceeds the maximum size for this format.",
    "unsupported_format": "This file format is not supported.",
}


class ParseFailure(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail

    @property
    def reason(self) -> str:
        return FAILURE_MESSAGES.get(self.code) or f"The file was rejected ({self.code.replace('_', ' ')})." + (
            f" {self.detail}" if self.detail else ""
        )


@dataclass(frozen=True)
class ParseOutcome:
    document: SirDocument
    raw: dict


def parse_in_sandbox(
    content: bytes, source_format: str, *, limits: ParserLimits = DEFAULT_LIMITS, require_landlock: bool = True
) -> ParseOutcome:
    result = run_sandboxed_child(
        _MODULE,
        [
            source_format, str(limits.max_input_bytes), str(limits.cpu_seconds), str(limits.address_space_bytes),
            "1" if require_landlock else "0", json.dumps(limits.as_dict()),
        ],
        content,
        wall_seconds=limits.wall_seconds,
        stdout_cap_bytes=limits.max_sir_bytes,
    )
    if result.timed_out:
        raise ParseFailure("timeout")
    if result.output_overflow:
        raise ParseFailure("sir_oversized")
    try:
        verdict = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ParseFailure("crash") from None
    if not isinstance(verdict, dict):
        raise ParseFailure("crash")
    if not verdict.get("ok"):
        raise ParseFailure(str(verdict.get("code", "crash"))[:64], str(verdict.get("detail", ""))[:200])
    try:
        document = validate_sir(verdict.get("sir"), limits=limits)
    except SirInvalid as exc:
        raise ParseFailure(exc.code) from None
    return ParseOutcome(document=document, raw=verdict["sir"])
