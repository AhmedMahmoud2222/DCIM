"""Regression coverage for two hardening measures on the bulk-import pipeline that were
claimed complete in earlier PR #50 review responses but were never actually committed to
the test suite:

1. Report formula injection (Codex review finding #2): `report.py`'s `_defuse_formula`
   must actually stop a hostile `raw_data`/error-message value from becoming a live
   formula in the generated results workbook — proven here by writing a report and
   reading it back with openpyxl, the same round-trip a real user's spreadsheet
   application would do.
2. Decompression-bomb / archive resource limits (Codex review finding #4): `parsing.py`'s
   `_reject_if_zip_bomb` must actually reject a genuinely oversized/over-compressed or
   over-populated archive before `openpyxl.load_workbook` is ever called, and must not
   reject a normal, legitimately-generated template.

Also covers: the template/parser/report column lists never drift apart (they all import
from `templates.COLUMNS_BY_IMPORT_TYPE`, but that's a design intent, not a guarantee —
this proves the round trip actually holds for all three import types).

These are pure-logic tests (no DB, no HTTP) — `report.py` and `parsing.py` operate only on
plain Python objects/bytes."""

import io
import time
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from openpyxl import load_workbook

from app.application.bulk_import.limits import (
    MAX_BULK_IMPORT_UNCOMPRESSED_BYTES,
    MAX_BULK_IMPORT_ZIP_ENTRIES,
)
from app.application.bulk_import.parsing import ParseRejected, parse_workbook
from app.application.bulk_import.report import build_report_workbook
from app.application.bulk_import.templates import (
    COLUMNS_BY_IMPORT_TYPE,
    build_catalog_template,
    build_equipment_template,
    build_rack_template,
)
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow

# ---------------------------------------------------------------------------
# Finding #2: report formula injection
# ---------------------------------------------------------------------------

_HOSTILE_VALUES = [
    "=1+1",
    "=HYPERLINK(\"http://evil.example/\",\"click me\")",
    "+cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1,1)",
    "\t=1+1",  # leading tab
    "\r=1+1",  # leading CR
]


def _make_job(import_type: str = "rack") -> BulkImportJob:
    job = BulkImportJob(
        id=uuid.uuid4(), import_type=import_type, mode="create_only", status="committed_with_errors",
        uploaded_by_user_id=uuid.uuid4(), original_filename="hostile.xlsx", file_hash="0" * 64,
        file_size_bytes=1, storage_key="irrelevant", row_count=1, valid_row_count=0, error_row_count=1,
        warning_row_count=0, committed_row_count=0, failed_row_count=1, created_at=datetime.now(UTC),
    )
    return job


def _make_row(job: BulkImportJob, raw_data: dict, errors: list, warnings: list) -> BulkImportRow:
    return BulkImportRow(
        id=uuid.uuid4(), job_id=job.id, row_number=2, sheet_name="Sheet1", status="failed", action="create",
        raw_data=raw_data, errors=errors, warnings=warnings, target_managed_asset_id=None,
        target_catalog_model_id=None, target_catalog_revision_id=None, created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("hostile", _HOSTILE_VALUES)
def test_hostile_raw_data_value_never_becomes_a_live_formula_in_the_report(hostile: str) -> None:
    job = _make_job("rack")
    columns = COLUMNS_BY_IMPORT_TYPE["rack"]
    raw_data = {c.name: hostile if c.name == "asset_tag" else None for c in columns}
    row = _make_row(job, raw_data, errors=[], warnings=[])

    report_bytes = build_report_workbook(job, [row])
    workbook = load_workbook(io.BytesIO(report_bytes))
    sheet = workbook.active
    assert sheet is not None

    # asset_tag is the first declared rack column -> column A, data row 2.
    cell = sheet["A2"]
    assert cell.data_type != "f", f"{hostile!r} became a live formula (data_type={cell.data_type!r})"
    # The defused value must still recognizably contain the original text (minus the
    # leading defusing quote openpyxl itself doesn't strip on write) -- proves this is a
    # real escape, not silent data loss.
    assert hostile.lstrip("\t\r") in str(cell.value)


@pytest.mark.parametrize("hostile", _HOSTILE_VALUES)
def test_hostile_error_message_never_becomes_a_live_formula_in_the_report(hostile: str) -> None:
    job = _make_job("rack")
    columns = COLUMNS_BY_IMPORT_TYPE["rack"]
    raw_data = {c.name: None for c in columns}
    row = _make_row(job, raw_data, errors=[{"field": "asset_tag", "message": hostile}], warnings=[])

    report_bytes = build_report_workbook(job, [row])
    workbook = load_workbook(io.BytesIO(report_bytes))
    sheet = workbook.active
    assert sheet is not None

    # Errors column is immediately after the declared columns + Status + Action.
    errors_col_index = len(columns) + 3
    cell = sheet.cell(row=2, column=errors_col_index)
    assert cell.data_type != "f", f"error message {hostile!r} became a live formula (data_type={cell.data_type!r})"


def test_benign_values_are_left_completely_unchanged() -> None:
    job = _make_job("rack")
    columns = COLUMNS_BY_IMPORT_TYPE["rack"]
    raw_data: dict[str, object] = {c.name: None for c in columns}
    raw_data["asset_tag"] = "RACK-042"
    raw_data["x_mm"] = 1500
    row = _make_row(job, raw_data, errors=[], warnings=[])

    report_bytes = build_report_workbook(job, [row])
    workbook = load_workbook(io.BytesIO(report_bytes))
    sheet = workbook.active
    assert sheet is not None
    assert sheet["A2"].value == "RACK-042"
    assert sheet["A2"].data_type == "s"
    x_mm_col = next(i for i, c in enumerate(columns, start=1) if c.name == "x_mm")
    assert sheet.cell(row=2, column=x_mm_col).value == 1500


# ---------------------------------------------------------------------------
# Template / parser / report column consistency
# ---------------------------------------------------------------------------

_TEMPLATE_BUILDERS = {"rack": build_rack_template, "equipment": build_equipment_template, "catalog": build_catalog_template}


@pytest.mark.parametrize("import_type", ["rack", "equipment", "catalog"])
def test_generated_template_header_matches_the_declared_schema_exactly(import_type: str) -> None:
    """Builds the real downloadable template and parses it back through the real
    parser — proves the template a user downloads, the header parse_workbook accepts,
    and COLUMNS_BY_IMPORT_TYPE (report.py's own source of truth) never drift apart, since
    a template built from stale/extra/reordered columns would fail this round trip."""
    template_bytes = _TEMPLATE_BUILDERS[import_type]()
    sheet_name, rows = parse_workbook(template_bytes, import_type)
    assert sheet_name != "Instructions"
    # The template ships with one example data row -- parse_workbook must accept it
    # against the exact same column set it was generated from.
    assert len(rows) >= 1
    example_row_number, example_row = rows[0]
    assert example_row_number == 2
    expected_names = {c.name for c in COLUMNS_BY_IMPORT_TYPE[import_type]}
    assert set(example_row.keys()) <= expected_names
    required_names = {c.name for c in COLUMNS_BY_IMPORT_TYPE[import_type] if c.required}
    assert required_names <= set(example_row.keys()), "the template's own example row must fill every required column"


# ---------------------------------------------------------------------------
# Finding #4: decompression-bomb / archive resource limits
# ---------------------------------------------------------------------------


def _zip_with_one_highly_compressible_member(uncompressed_size: int) -> bytes:
    """An honest zip bomb: real DEFLATE compression of a long run of a single repeated
    byte, which compresses to a tiny archive but decompresses to `uncompressed_size`
    bytes. Not a spoofed/forged central-directory size -- this is what `zf.infolist()`'s
    `file_size` would legitimately report for this exact archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * uncompressed_size)
    return buffer.getvalue()


def test_a_genuine_zip_bomb_is_rejected_before_openpyxl_ever_inflates_it() -> None:
    bomb = _zip_with_one_highly_compressible_member(MAX_BULK_IMPORT_UNCOMPRESSED_BYTES * 3)
    # A real archive this shape compresses to a few hundred KB at most -- far under the
    # upload size cap -- so it must be `_reject_if_zip_bomb`, not the file-size check,
    # that catches it.
    assert len(bomb) < 2 * 1024 * 1024

    started = time.monotonic()
    with pytest.raises(ParseRejected) as exc_info:
        parse_workbook(bomb, "rack")
    elapsed = time.monotonic() - started

    assert exc_info.value.reason_code in {
        "zip_entry_too_large", "zip_total_too_large", "zip_compression_ratio_suspicious",
    }
    # Rejected on central-directory metadata alone -- must not spend real time inflating
    # hundreds of MB of member content.
    assert elapsed < 5.0


def test_an_archive_with_too_many_entries_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(MAX_BULK_IMPORT_ZIP_ENTRIES + 10):
            zf.writestr(f"part-{i}.xml", b"<x/>")
    content = buffer.getvalue()

    with pytest.raises(ParseRejected) as exc_info:
        parse_workbook(content, "rack")
    assert exc_info.value.reason_code == "zip_entry_count_exceeded"


def test_a_corrupt_archive_is_rejected_cleanly() -> None:
    # Real ZIP local-file-header magic bytes (so it passes the earlier content-sniff),
    # followed by garbage -- not a parseable archive.
    content = b"PK\x03\x04" + b"\x00" * 64
    with pytest.raises(ParseRejected) as exc_info:
        parse_workbook(content, "rack")
    assert exc_info.value.reason_code in {"zip_unreadable", "workbook_load_failed"}


@pytest.mark.parametrize("import_type", ["rack", "equipment", "catalog"])
def test_a_normal_legitimate_template_is_not_affected_by_the_zip_bomb_guard(import_type: str) -> None:
    """The zip-bomb guard must never false-positive on a real, small, well-compressed
    template -- this is the same file real users download and upload back unmodified."""
    template_bytes = _TEMPLATE_BUILDERS[import_type]()
    sheet_name, rows = parse_workbook(template_bytes, import_type)
    assert sheet_name is not None
    assert len(rows) >= 1
