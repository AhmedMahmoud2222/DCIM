"""Results workbook: every original row (rebuilt from `raw_data` using the exact same
column list `templates.py` used to build the upload template — never allowed to drift,
since both import from `templates.COLUMNS_BY_IMPORT_TYPE`) plus appended `Status`,
`Action`, `Errors`, `Warnings`, `EntityId` columns."""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from app.application.bulk_import.templates import COLUMNS_BY_IMPORT_TYPE
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow


def _format_issues(issues: list) -> str:
    if not issues:
        return ""
    return "; ".join(f"{item.get('field', '')}: {item.get('message', '')}" for item in issues)


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
            sheet.cell(row=row_index, column=col_index, value=raw.get(column.name))
        base = len(columns)
        sheet.cell(row=row_index, column=base + 1, value=row.status)
        sheet.cell(row=row_index, column=base + 2, value=row.action)
        sheet.cell(row=row_index, column=base + 3, value=_format_issues(row.errors))
        sheet.cell(row=row_index, column=base + 4, value=_format_issues(row.warnings))
        entity_id = row.target_catalog_revision_id or row.target_managed_asset_id or row.target_catalog_model_id
        sheet.cell(row=row_index, column=base + 5, value=str(entity_id) if entity_id else "")

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
