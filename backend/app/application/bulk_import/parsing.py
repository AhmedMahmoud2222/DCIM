"""Untrusted-input XLSX parsing for the bulk-import pipeline — content-sniffed (never
trusted by extension or declared content-type, mirroring app/api/v1/floor_plans.py's
`upload_floor_plan_file` discipline), bounded by `limits.py`, and read with openpyxl's
`read_only=True` mode for a memory-bounded pass over the workbook."""

import io
import zipfile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from app.application.bulk_import.limits import (
    MAX_BULK_IMPORT_ROWS,
    MAX_BULK_IMPORT_UNCOMPRESSED_BYTES,
    MAX_BULK_IMPORT_ZIP_COMPRESSION_RATIO,
    MAX_BULK_IMPORT_ZIP_ENTRIES,
)
from app.application.bulk_import.templates import COLUMNS_BY_IMPORT_TYPE, ColumnSpec

# The ZIP local-file-header magic bytes — every real .xlsx is a ZIP archive (OOXML), so
# this is the same "verify real content, not the extension/declared type" discipline
# app/api/v1/floor_plans.py already applies to SVG/PNG/JPEG uploads.
_XLSX_MAGIC = b"PK\x03\x04"


class ParseRejected(Exception):
    """Raised for a problem with the file/workbook itself (not an individual row) —
    the caller sets the job to `failed_parse` with `.reason` as `rejection_reason`,
    mirroring app/application/svg_sanitizer.py's SvgRejected for the floor-plan importer."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _is_xlsx(content: bytes) -> bool:
    return content[:4] == _XLSX_MAGIC


def _validate_header(header: list[str | None], columns: tuple[ColumnSpec, ...]) -> None:
    present = {str(h).strip() for h in header if h is not None and str(h).strip()}
    required = {c.name for c in columns if c.required}
    missing_required = required - present
    known = {c.name for c in columns}
    if not (present & known):
        raise ParseRejected(
            "The uploaded file's header row does not match any expected column for this import type "
            f"(expected columns such as {sorted(known)[:5]}...)."
        )
    if missing_required:
        raise ParseRejected(f"The uploaded file is missing required column(s): {sorted(missing_required)}.")


def _reject_if_zip_bomb(content: bytes) -> None:
    """SEC (Codex PR #50 review, finding #4): checked against the zip's own central
    directory metadata — cheap and entirely bounded regardless of how large the archive
    claims its members decompress to — before openpyxl ever inflates a single byte of
    member content. Every real .xlsx passes this trivially (a handful of small, well-
    compressed XML parts); this only rejects an archive shaped like a decompression-bomb
    attack."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_BULK_IMPORT_ZIP_ENTRIES:
                raise ParseRejected(
                    f"The uploaded file's archive has more than the maximum allowed "
                    f"{MAX_BULK_IMPORT_ZIP_ENTRIES} entries."
                )
            total_uncompressed = 0
            for info in infos:
                if info.file_size > MAX_BULK_IMPORT_UNCOMPRESSED_BYTES:
                    raise ParseRejected(
                        f"The uploaded file's archive contains an entry that exceeds the maximum "
                        f"allowed uncompressed size of {MAX_BULK_IMPORT_UNCOMPRESSED_BYTES} bytes."
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_BULK_IMPORT_UNCOMPRESSED_BYTES:
                    raise ParseRejected(
                        f"The uploaded file's archive would decompress to more than the maximum "
                        f"allowed {MAX_BULK_IMPORT_UNCOMPRESSED_BYTES} bytes in total."
                    )
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > MAX_BULK_IMPORT_ZIP_COMPRESSION_RATIO:
                    raise ParseRejected(
                        "The uploaded file's archive contains an entry with an implausible "
                        "compression ratio (possible decompression bomb)."
                    )
            bad_member = zf.testzip()
            if bad_member is not None:
                raise ParseRejected(f"The uploaded file's archive is corrupt (bad member {bad_member!r}).")
    except zipfile.BadZipFile as exc:
        raise ParseRejected(f"The uploaded file could not be read as a valid XLSX workbook: {exc}") from exc


def parse_workbook(content: bytes, import_type: str) -> tuple[str, list[tuple[int, dict]]]:
    """Returns `(sheet_name, rows)`, where `rows` is `[(row_number, row_dict), ...]` —
    `row_number` is the real 1-based spreadsheet row (header is row 1; gaps from
    skipped fully-blank rows are preserved, not renumbered), and each `row_dict` is
    keyed by column header name (only the columns this `import_type` actually declares —
    an unrecognized extra column in the file is kept verbatim under its own header key so
    a validator could in principle inspect it, but no validator here reads unknown keys).
    Raises ParseRejected for anything wrong with the file/workbook itself; per-row
    problems are the caller's/validator's job, not this function's."""
    if not _is_xlsx(content):
        raise ParseRejected("File content is not recognized as an XLSX workbook (checked by content, not filename).")

    # Bounded against a decompression bomb (finding #4) strictly before openpyxl is ever
    # asked to inflate anything — see MAX_BULK_IMPORT_UNCOMPRESSED_BYTES's docstring.
    _reject_if_zip_bomb(content)

    columns = COLUMNS_BY_IMPORT_TYPE[import_type]

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (InvalidFileException, OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ParseRejected(f"The uploaded file could not be read as a valid XLSX workbook: {exc}") from exc

    try:
        sheet_names = [
            name for name in workbook.sheetnames if name != "Instructions"
        ] or workbook.sheetnames
        if not sheet_names:
            raise ParseRejected("The uploaded workbook has no worksheets.")
        sheet_name = sheet_names[0]
        sheet = workbook[sheet_name]

        row_iter = sheet.iter_rows(values_only=True)
        try:
            header = list(next(row_iter))
        except StopIteration:
            raise ParseRejected("The uploaded workbook's first worksheet is empty.") from None

        _validate_header(header, columns)
        header_names = [str(h).strip() if h is not None else None for h in header]

        rows: list[tuple[int, dict]] = []
        for row_number, values in enumerate(row_iter, start=2):
            if values is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                continue  # fully-blank row — skipped, not an error
            row: dict = {}
            for col_index, col_name in enumerate(header_names):
                if col_name is None:
                    continue
                value = values[col_index] if col_index < len(values) else None
                row[col_name] = value
            rows.append((row_number, row))
            if len(rows) > MAX_BULK_IMPORT_ROWS:
                raise ParseRejected(
                    f"The uploaded workbook has more than the maximum allowed {MAX_BULK_IMPORT_ROWS} data rows."
                )
        return sheet_name, rows
    finally:
        workbook.close()
