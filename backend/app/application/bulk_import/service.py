"""Bulk-import orchestration: `run_parse_and_validate` and `run_commit`, called from the
Celery tasks in app/infrastructure/tasks/bulk_import.py (which own the transaction
boundary and the guaranteed-terminal-status fallback — see that module's docstring).
Job/row status bookkeeping commits here are the one deliberate exception to this repo's
"service functions never commit" convention (app/application/equipment_instantiation_
service.py's own docstring), needed so polling clients see live progress — exactly like
app/infrastructure/tasks/floorplan_import.py's tasks already do for the floor-plan
importer."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.bulk_import import validators as validators_pkg
from app.application.bulk_import.commit import catalog as commit_catalog
from app.application.bulk_import.commit import describe_integrity_error
from app.application.bulk_import.commit import equipment as commit_equipment
from app.application.bulk_import.commit import rack as commit_rack
from app.application.bulk_import.limits import BULK_IMPORT_COMMIT_BATCH_SIZE
from app.application.bulk_import.parsing import ParseRejected, parse_workbook
from app.application.bulk_import.report import build_report_workbook
from app.application.bulk_import.resolvers import RowRejected
from app.application.bulk_import.validators import catalog as validate_catalog
from app.application.bulk_import.validators import equipment as validate_equipment
from app.application.bulk_import.validators import rack as validate_rack
from app.application.placement_service import PlacementConflict
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.audit.models import AuditLog
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.storage import get_storage_backend

logger = get_logger(__name__)

_VALIDATE_FUNCS = {
    "rack": validate_rack.validate_row,
    "equipment": validate_equipment.validate_row,
    "catalog": validate_catalog.validate_row,
}
_COMMIT_FUNCS = {
    "rack": commit_rack.commit_row,
    "equipment": commit_equipment.commit_row,
    "catalog": commit_catalog.commit_row,
}


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_audit_log_sync_safe(
    db: AsyncSession, *, actor_user_id: uuid.UUID, action: str, entity_type: str, entity_id: uuid.UUID | None,
    correlation_id: str, after: dict | None = None,
) -> None:
    """A thin, synchronous-shaped add (no await needed beyond the flush this function
    itself performs) mirroring app/application/audit_service.py::write_audit_log's shape
    exactly, used for the job-level (not row-level) audit entries this module writes
    directly rather than importing the async helper for a single extra call site."""
    db.add(
        AuditLog(
            timestamp=datetime.now(UTC), actor_user_id=actor_user_id, action=action, entity_type=entity_type,
            entity_id=entity_id, request_id=None, correlation_id=correlation_id, source="api", after=after,
        )
    )


async def create_job(
    db: AsyncSession, *, import_type: str, mode: str, uploaded_by_user_id: uuid.UUID, original_filename: str,
    file_hash: str, file_size_bytes: int, storage_key: str,
) -> BulkImportJob:
    job = BulkImportJob(
        import_type=import_type, mode=mode, status="uploaded", uploaded_by_user_id=uploaded_by_user_id,
        original_filename=original_filename, file_hash=file_hash, file_size_bytes=file_size_bytes,
        storage_key=storage_key,
    )
    db.add(job)
    await db.flush()
    return job


async def run_parse_and_validate(db: AsyncSession, job_id: uuid.UUID) -> None:
    job = await db.get(BulkImportJob, job_id)
    if job is None:
        logger.error("bulk_import_job_not_found", job_id=str(job_id))
        return

    job.status = "parsing"
    await db.commit()

    assert job.storage_key is not None
    storage = get_storage_backend()
    content = storage.read(job.storage_key)

    try:
        sheet_name, parsed_rows = parse_workbook(content, job.import_type)
    except ParseRejected as exc:
        job.status = "failed_parse"
        job.rejection_reason = exc.reason
        await db.commit()
        logger.warning("bulk_import_parse_rejected", job_id=str(job_id), reason=exc.reason)
        return

    validate_row = _VALIDATE_FUNCS[job.import_type]
    batch_state = validators_pkg.BatchState()

    row_count = 0
    valid_count = 0
    error_count = 0
    warning_count = 0
    for row_number, raw in parsed_rows:
        result = await validate_row(db, row_number=row_number, raw=raw, mode=job.mode, batch_state=batch_state)
        row_count += 1
        if result.status == "valid":
            valid_count += 1
        else:
            error_count += 1
        if result.warnings:
            warning_count += 1
        db.add(
            BulkImportRow(
                job_id=job.id, row_number=row_number, sheet_name=sheet_name, status=result.status, action=result.action,
                raw_data={k: _json_safe(v) for k, v in raw.items()}, errors=result.errors, warnings=result.warnings,
                target_managed_asset_id=result.target_managed_asset_id,
                target_catalog_model_id=result.target_catalog_model_id,
                target_catalog_revision_id=result.target_catalog_revision_id,
            )
        )

    job.row_count = row_count
    job.valid_row_count = valid_count
    job.error_row_count = error_count
    job.warning_row_count = warning_count
    job.status = "validated"
    job.validated_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "bulk_import_validated", job_id=str(job_id), row_count=row_count, valid_count=valid_count, error_count=error_count,
    )


def _describe_commit_error(exc: Exception) -> dict:
    if isinstance(exc, RowRejected):
        return {"field": exc.field, "message": exc.message}
    if isinstance(exc, IntegrityError):
        return {"field": None, "message": describe_integrity_error(exc)}
    if isinstance(exc, PlacementConflict):
        return {"field": None, "message": "Placement conflicts with a concurrent change."}
    if isinstance(exc, (NotFoundError, ConflictError)):
        return {"field": None, "message": exc.detail}
    return {"field": None, "message": "This row could not be committed due to an unexpected error."}


async def run_commit(db: AsyncSession, job_id: uuid.UUID) -> None:
    job = await db.get(BulkImportJob, job_id)
    if job is None:
        logger.error("bulk_import_job_not_found", job_id=str(job_id))
        return

    job.status = "committing"
    await db.commit()

    commit_row = _COMMIT_FUNCS[job.import_type]

    while True:
        job = await db.get(BulkImportJob, job_id)
        assert job is not None
        batch = list(
            (
                await db.execute(
                    select(BulkImportRow)
                    .where(BulkImportRow.job_id == job.id, BulkImportRow.status == "valid")
                    .order_by(BulkImportRow.row_number)
                    .limit(BULK_IMPORT_COMMIT_BATCH_SIZE)
                )
            ).scalars()
        )
        if not batch:
            break

        for row in batch:
            try:
                async with db.begin_nested():
                    result = await commit_row(db, job=job, row=row, mode=job.mode)
            except (RowRejected, IntegrityError, PlacementConflict, NotFoundError, ConflictError) as exc:
                row.status = "failed"
                row.errors = [*row.errors, _describe_commit_error(exc)]
                job.failed_row_count += 1
                logger.warning(
                    "bulk_import_row_commit_failed", job_id=str(job_id), row_number=row.row_number, reason=str(exc)
                )
            else:
                row.status = "committed"
                if result.target_managed_asset_id is not None:
                    row.target_managed_asset_id = result.target_managed_asset_id
                if result.target_catalog_model_id is not None:
                    row.target_catalog_model_id = result.target_catalog_model_id
                if result.target_catalog_revision_id is not None:
                    row.target_catalog_revision_id = result.target_catalog_revision_id
                job.committed_row_count += 1

        await db.commit()  # checkpoint this batch — visible to polling clients immediately

    job = await db.get(BulkImportJob, job_id)
    assert job is not None
    all_rows_stmt = select(BulkImportRow).where(BulkImportRow.job_id == job.id).order_by(BulkImportRow.row_number)
    all_rows = list((await db.execute(all_rows_stmt)).scalars())
    report_bytes = build_report_workbook(job, all_rows)
    storage = get_storage_backend()
    report_key = f"bulk-import-report-{job.id.hex}.xlsx"
    storage.save(report_key, report_bytes)

    job.report_storage_key = report_key
    # "committed_with_errors" whenever the job did not cleanly succeed end to end — a row
    # invalid at validate time (never attempted at commit) is just as much a reason to
    # flag the job for operator attention as a row that failed during commit itself.
    job.status = "committed_with_errors" if (job.failed_row_count > 0 or job.error_row_count > 0) else "committed"
    job.committed_at = datetime.now(UTC)
    _write_audit_log_sync_safe(
        db, actor_user_id=job.uploaded_by_user_id, action=f"{job.import_type}.bulk_import.commit_job",
        entity_type="bulk_import_job", entity_id=job.id, correlation_id=str(job.id),
        after={"status": job.status, "committed_row_count": job.committed_row_count, "failed_row_count": job.failed_row_count},
    )
    await db.commit()
    logger.info(
        "bulk_import_committed", job_id=str(job_id), status=job.status, committed=job.committed_row_count,
        failed=job.failed_row_count,
    )
