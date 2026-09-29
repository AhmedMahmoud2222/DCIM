"""SEC (Codex PR #50 review, ROUND 2, finding #2): a synthetic "secret sentinel" embedded
in an unexpected parse/commit failure must never appear in ANY log record emitted by
either the app/application/bulk_import/service.py layer or the
app/infrastructure/tasks/bulk_import.py Celery-task layer -- while the job's own
`rejection_reason` (the sanctioned user-facing channel, surfaced via
`GET /import-jobs/{id}`) can still legitimately contain it. Log capture mirrors
tests/api/test_collectors.py's SEC-07 mechanism exactly: a real structlog logger wired to
an in-memory buffer, monkeypatched onto the module(s) under test, so this asserts against
actually-emitted structured log lines rather than mock call arguments.

Part A (finding #2A): `_parse_and_validate_async`/`_commit_async` previously did a bare
`raise` after logging a sanitized event, re-raising the ORIGINAL exception object (message
+ traceback + any chained cause) into Celery's own task-failure machinery. Both now raise
`BulkImportTaskFailed(f"job {job_id} failed: {type(exc).__name__}") from None` instead --
verified here by asserting the exception that actually propagates out of `.run()` is
`BulkImportTaskFailed`, never the original exception type, and that its own message
carries no sentinel.

Part B (finding #2B): `ParseRejected.reason` is free text that can embed content
influenced by the uploaded file's own structure (e.g. a corrupt-zip message interpolating
`{exc}`). `run_parse_and_validate`'s `except ParseRejected` handler now logs
`exc.reason_code` (a short fixed string) instead of `exc.reason` -- verified here with a
corrupted archive whose corrupt member name IS the sentinel, so `.reason` (and therefore
`job.rejection_reason`) legitimately contains it while every captured log line does not."""

import io
import json
import uuid
import zipfile

import pytest
import structlog

from app.application.bulk_import import service as bulk_import_service
from app.infrastructure.tasks import bulk_import as bulk_import_tasks
from app.infrastructure.tasks.bulk_import import BulkImportTaskFailed, parse_and_validate_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model

SENTINEL = "synthetic-SEC-round2-keep-private-8f3a1c"


def _wire_capture_logger(monkeypatch, *modules) -> io.StringIO:
    """Same mechanism tests/api/test_collectors.py's SEC-07 test already uses: a real
    structlog logger writing actual JSON lines to an in-memory buffer, so assertions run
    against what was genuinely emitted -- not against a mock's call arguments."""
    log_output = io.StringIO()
    captured_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output), processors=[structlog.processors.JSONRenderer()],
    )
    for module in modules:
        monkeypatch.setattr(module, "logger", captured_logger)
    return log_output


def _captured_lines(log_output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in log_output.getvalue().splitlines() if line.strip()]


def _build_corrupt_zip_with_sentinel_in_member_name(sentinel: str) -> bytes:
    """A real, openable ZIP (so it passes both the upload-time magic-byte sniff and
    `_reject_if_zip_bomb`'s central-directory checks) whose single member is named with
    the attacker-controlled sentinel and whose stored (uncompressed) payload bytes have
    been corrupted in place -- `zipfile.testzip()` detects the CRC-32 mismatch and returns
    that member's name as `bad_member`, which `parsing.py` then embeds verbatim in
    `ParseRejected.reason` (by design -- `.reason` is user-facing). `ZIP_STORED` (no
    compression) is used so the payload appears byte-for-byte in the archive, making it
    trivial to locate and corrupt without disturbing the central directory."""
    payload = b"payload-bytes-for-crc-corruption-check-only"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(f"{sentinel}.dat", payload)
    raw = bytearray(buf.getvalue())
    idx = raw.index(payload)
    raw[idx] ^= 0xFF  # flip one payload byte -- breaks the member's stored CRC-32
    return bytes(raw)


async def test_parse_rejection_logs_reason_code_never_the_raw_reason_text(client, auth_headers, monkeypatch):
    headers = await auth_headers("Engineer")
    content = _build_corrupt_zip_with_sentinel_in_member_name(SENTINEL)

    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert upload.status_code == 202, upload.text
    job = upload.json()

    log_output = _wire_capture_logger(monkeypatch, bulk_import_service, bulk_import_tasks)

    # ParseRejected is caught and handled cleanly inside run_parse_and_validate itself --
    # this must NOT raise (it's an expected, user-facing rejection, not an unexpected
    # internal error).
    parse_and_validate_bulk_import_job.run(job["id"])

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "failed_parse", job_status
    # The sanctioned user-facing channel: legitimately allowed to contain the sentinel.
    assert SENTINEL in job_status["rejection_reason"]

    lines = _captured_lines(log_output)
    rejected_lines = [line for line in lines if line.get("event") == "bulk_import_parse_rejected"]
    assert len(rejected_lines) == 1, lines
    assert rejected_lines[0].get("reason_code") == "zip_corrupt_member"
    assert "reason" not in rejected_lines[0], "the raw free-text .reason must never be logged, only .reason_code"

    full_output = log_output.getvalue()
    assert SENTINEL not in full_output, f"sentinel leaked into a log record: {full_output!r}"


async def test_unexpected_parse_failure_raises_wrapped_exception_and_never_leaks_the_original_message(
    client, auth_headers, monkeypatch,
):
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    content = build_workbook(
        RACK_HEADERS,
        [[f"RACK-{uuid.uuid4().hex[:8]}", "Row A Rack 1", model["manufacturer"], model["model_name"], "",
          room["site_code"], room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]],
    )
    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert upload.status_code == 202, upload.text
    job = upload.json()

    def _raise_unexpected_error_with_sentinel(_content: bytes, _import_type: str):
        # Simulates a genuine unexpected internal error whose own message happens to embed
        # attacker/user-influenced content -- e.g. a raw cell value or SQL literal, the
        # same class of leak finding #2B's IntegrityError scenario describes, just forced
        # here at the parse layer since it's simpler to trigger deterministically.
        raise ValueError(f"unexpected internal failure touching {SENTINEL}")

    monkeypatch.setattr(bulk_import_service, "parse_workbook", _raise_unexpected_error_with_sentinel)
    log_output = _wire_capture_logger(monkeypatch, bulk_import_service, bulk_import_tasks)

    with pytest.raises(BulkImportTaskFailed) as excinfo:
        parse_and_validate_bulk_import_job.run(job["id"])

    # Part A: the ORIGINAL exception (ValueError, carrying the sentinel) must never be what
    # propagates out of `.run()` -- only the sanitized wrapper, and its own message must
    # never carry the sentinel either.
    assert SENTINEL not in str(excinfo.value)
    assert excinfo.value.__cause__ is None, "raised `from None` -- must never chain to the original exception"

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "failed_parse", job_status
    assert SENTINEL not in (job_status["rejection_reason"] or ""), (
        "the unexpected-failure fallback path uses a fixed generic message, never the original exception's text"
    )

    full_output = log_output.getvalue()
    assert SENTINEL not in full_output, f"sentinel leaked into a log record: {full_output!r}"
    lines = _captured_lines(log_output)
    unexpected_lines = [line for line in lines if line.get("event") == "bulk_import_parse_unexpected_failure"]
    assert len(unexpected_lines) == 1, lines
    assert unexpected_lines[0].get("error_code") == "ValueError"
