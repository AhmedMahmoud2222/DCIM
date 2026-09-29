"""Results workbook: every original row (rebuilt from `raw_data` using the exact same
column list `templates.py` used to build the upload template — never allowed to drift,
since both import from `templates.COLUMNS_BY_IMPORT_TYPE`) plus appended `Status`,
`Action`, `Errors`, `Warnings`, `EntityId` columns.

SEC (Codex PR #50 review, finding #2): every value written here can originate from an
untrusted uploaded workbook (`raw_data`) or from a validator/commit free-text `message`
built from user-controlled cell content (e.g. an asset_tag echoed back into an error
string). openpyxl infers a cell's `data_type` from the value it's given — a string
starting with `=`/`+`/`-`/`@` (or a leading tab/CR, which some spreadsheet apps also
treat as a formula prefix) becomes a live formula (`data_type == "f"`) the moment
whoever downloads this report opens it in Excel/LibreOffice — classic spreadsheet/CSV
injection. Every untrusted string written to a cell below is passed through
`_defuse_formula` first, which is a one-way, idempotent escape (never applied twice,
never applied to a value that didn't need it), so the report can never itself smuggle a
formula regardless of what appears in `raw_data`, `errors`, or `warnings`."""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from app.application.bulk_import.templates import COLUMNS_BY_IMPORT_TYPE
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow

# Leading characters that openpyxl (mirroring Excel/LibreOffice) treats as introducing a
# formula. Tab/CR are included because some spreadsheet apps also honor them as a
# formula-launching prefix when a cell is re-interpreted after a paste/import.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _defuse_formula(value: object) -> object:
    """Prepends a single `'` (apostrophe) to a string that would otherwise be
    auto-detected by openpyxl as a formula, which forces it to stay a literal text cell
    (`data_type` stays `"s"`). Non-strings (numbers, None, bools) pass through untouched —
    they were never at risk and must keep their real type (e.g. a numeric x_mm column)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _format_issues(issues: list) -> str:
    if not issues:
        return ""
    text = "; ".join(f"{item.get('field', '')}: {item.get('message', '')}" for item in issues)
    # _defuse_formula always returns a str back for a str input (either unchanged or
    # prefixed with a defusing quote) — the `object` return type on its signature is only
    # for the non-string (int/float/bool/None) passthrough case build_report_workbook's
    # other call site needs; the str() here is purely a type-narrowing no-op.
    return str(_defuse_formula(text))


def build_report_workbook(job: BulkImportJob, rows: list[BulkImportRow]) -> bytes:
    columns = COLUMNS_BY_IMPORT_TYPE[job.import_type]
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Report"

    header_font = Font(bold=True)
    headers = [c.name for c in columns] + ["Status", "Action", "Errors", "Warnings", "EntityId"]
    for col_index, header in enumerate(headers, start=1):
        sheet.cell(row=1, column=col_index, value=header).font = header_font
    sheet.freeze_panes = "A2"

    for row_index, row in enumerate(sorted(rows, key=lambda r: r.row_number), start=2):
        raw = row.raw_data or {}
        for col_index, column in enumerate(columns, start=1):
            sheet.cell(row=row_index, column=col_index, value=_defuse_formula(raw.get(column.name)))
        base = len(columns)
        # row.status/row.action are always one of this module's own fixed, trusted
        # literals (ROW_STATUSES/ROW_ACTIONS) — never user-controlled — so they need no
        # defusing, unlike the free-text errors/warnings and the raw uploaded values above.
        sheet.cell(row=row_index, column=base + 1, value=row.status)
        sheet.cell(row=row_index, column=base + 2, value=row.action)
        sheet.cell(row=row_index, column=base + 3, value=_format_issues(row.errors))
        sheet.cell(row=row_index, column=base + 4, value=_format_issues(row.warnings))
        entity_id = row.target_catalog_revision_id or row.target_managed_asset_id or row.target_catalog_model_id
        sheet.cell(row=row_index, column=base + 5, value=str(entity_id) if entity_id else "")

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
