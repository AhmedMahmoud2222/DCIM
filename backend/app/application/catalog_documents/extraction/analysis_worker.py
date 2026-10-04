"""Sandboxed child: turns page text into candidates (`candidates.analyze_pages`).

Its input is text from a hostile file, so even the pure-Python parser runs under CPU and memory
limits: a pathological table or line that makes it slow is killed and reported, never run inside the
worker that holds database connections.

stdin: JSON {"pages": [{page, text, method, ocr_confidence}], "target_names": [...]}
argv: cpu_seconds address_space_bytes require_landlock"""

import json
import sys
from dataclasses import asdict

from app.application.catalog_documents.extraction.candidates import PageText, analyze_pages
from app.application.catalog_documents.extraction.sandbox import SandboxUnavailable, enter_sandbox


def main() -> None:
    cpu, address_space = int(sys.argv[1]), int(sys.argv[2])
    try:
        enter_sandbox(
            cpu_seconds=cpu, address_space_bytes=address_space, require_seccomp=False, require_landlock=sys.argv[3] == "1"
        )
    except SandboxUnavailable:
        sys.stdout.write(json.dumps({"ok": False, "code": "sandbox_unavailable"}))
        return
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    pages = [PageText(int(p["page"]), str(p["text"]), str(p["method"]), p.get("ocr_confidence")) for p in payload["pages"]]
    result = analyze_pages(pages, [str(n) for n in payload["target_names"]])
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "model_resolution": result.model_resolution,
                "identified_models": result.identified_models,
                "warnings": result.warnings,
                "candidates": [asdict(c) for c in result.candidates],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
