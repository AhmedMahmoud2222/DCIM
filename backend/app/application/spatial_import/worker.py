"""Sandboxed child: parses one untrusted spatial file and writes one JSON verdict to stdout.

Reads the file from stdin; argv: format max_input_bytes cpu_seconds address_space_bytes require_landlock
limits_json. It enters the process sandbox (rlimits, no network, seccomp, Landlock with no write access)
*before* it reads a single byte of the upload, runs with a scrubbed environment (see
`sandbox._child_env`: no database, Redis, signing or integration credentials), and returns only the SIR or a
stable failure code. It never imports the database layer, the settings object or any service module."""

import json
import sys
from typing import Any

from app.application.catalog_documents.extraction.sandbox import SandboxUnavailable, enter_sandbox
from app.application.spatial_import.limits import ParserLimits


def _verdict(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()


def main() -> None:
    fmt = sys.argv[1]
    cpu, address_space = int(sys.argv[3]), int(sys.argv[4])
    landlock = sys.argv[5] == "1"
    limits = ParserLimits(**json.loads(sys.argv[6]))
    try:
        enter_sandbox(cpu_seconds=cpu, address_space_bytes=address_space, require_seccomp=False, require_landlock=landlock)
    except SandboxUnavailable:
        _verdict({"ok": False, "code": "sandbox_unavailable", "detail": ""})
        return
    content = sys.stdin.buffer.read(int(sys.argv[2]) + 1)
    if len(content) > int(sys.argv[2]):
        _verdict({"ok": False, "code": "file_too_large", "detail": ""})
        return
    try:
        if fmt == "dxf":
            from app.application.spatial_import.dxf_parser import DxfRejected, parse_dxf

            try:
                document = parse_dxf(content, limits=limits)
            except DxfRejected as exc:
                _verdict({"ok": False, "code": exc.code, "detail": exc.detail[:200]})
                return
        elif fmt == "vsdx":
            from app.application.spatial_import.vsdx_parser import VsdxRejected, parse_vsdx

            try:
                document = parse_vsdx(content, limits=limits)
            except VsdxRejected as exc:
                _verdict({"ok": False, "code": exc.code, "detail": exc.detail[:200]})
                return
        else:
            _verdict({"ok": False, "code": "unsupported_format", "detail": fmt[:16]})
            return
    except RecursionError:
        _verdict({"ok": False, "code": "parser_recursion", "detail": ""})
        return
    except MemoryError:
        _verdict({"ok": False, "code": "parser_memory", "detail": ""})
        return
    _verdict({"ok": True, "sir": document.to_dict()})


if __name__ == "__main__":
    main()
