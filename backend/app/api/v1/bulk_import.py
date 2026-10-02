"""Generic bulk-import job API — status/preview/commit/cancel/report, shared across all
three import types (rack/equipment/catalog). Upload itself is per-domain (a thin
addition to each existing router: `GET .../import-template` + `POST .../import-jobs`, in
app/api/v1/racks.py / equipment.py / catalog_designer.py), since only the owning router
knows the right permission code and template builder — everything after upload is
generic, per the shared-pipeline design (app/domain/bulk_import/models.py's docstring).

Every handler here loads the job first, then checks the permission that matches its
`import_type` — the same post-load-permission-check idiom app/api/v1/floor_plans.py's
`_revision_read_dependency` already established, since which permission applies can't be
known until the job (and its `import_type`) is loaded."""

import uuid
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.rbac import AuthContext, get_auth_context
from app.core.errors import ApiError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.storage import get_storage_backend
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job

router = APIRouter(prefix="/import-jobs", tags=["bulk-import"])
logger = get_logger(__name__)

_DISPATCH_FAILURE_DETAIL = "Could not queue this job for processing; please retry."


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


async def _load_job(job_id: uuid.UUID, db: AsyncSession) -> BulkImportJob:
    job = await db.get(BulkImportJob, job_id)
    if job is None:
        raise NotFoundError(f"BulkImportJob {job_id} not found.")
    return job


def _assert_job_visible(job: BulkImportJob, ctx: AuthContext) -> None:
    """A job has no site link, so a site-restricted caller may only touch jobs they
    uploaded themselves. Reported as 404, the same as a missing job, so ids cannot be
    probed (SEC-RBAC-59-04). Unrestricted callers keep the existing behaviour."""
    if not ctx.scope.unrestricted and job.uploaded_by_user_id != ctx.user.id:
        raise NotFoundError(f"BulkImportJob {job.id} not found.")


def _check_import_permission(job: BulkImportJob, ctx: AuthContext) -> None:
    """`catalog` import additionally requires Administrator role membership, mirroring
    `require_catalog_administrator` (app/application/rbac.py) exactly — this is not a new
    RBAC mechanism, just that dependency's logic inlined for a permission that can only
    be chosen after the job (and its import_type) is loaded."""
    _assert_job_visible(job, ctx)
    if job.import_type == "catalog":
        if not ctx.has_permission("catalog:import"):
            raise ForbiddenError("Missing required permission: catalog:import")
        if not ctx.has_role("Administrator"):
            raise ForbiddenError("'catalog:import' additionally requires Administrator role membership.")
        return
    required = f"{job.import_type}:import"
    if not ctx.has_permission(required):
        raise ForbiddenError(f"Missing required permission: {required}")


async def _load_job_for_read(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> BulkImportJob:
    job = await _load_job(job_id, db)
    _assert_job_visible(job, ctx)
    required = f"{job.import_type}:read"
    if not ctx.has_permission(required):
        raise ForbiddenError(f"Missing required permission: {required}")
    return job


class BulkImportJobOut(BaseModel):
    id: uuid.UUID
    import_type: str
    mode: str
    status: str
    original_filename: str
    file_size_bytes: int
    row_count: int
    valid_row_count: int
    error_row_count: int
    warning_row_count: int
    committed_row_count: int
    failed_row_count: int
    rejection_reason: str | None
    created_at: datetime
    validated_at: datetime | None
    committed_at: datetime | None
    report_available: bool

    model_config = {"from_attributes": False}

    @classmethod
    def from_job(cls, job: BulkImportJob) -> "BulkImportJobOut":
        return cls(
            id=job.id, import_type=job.import_type, mode=job.mode, status=job.status,
            original_filename=job.original_filename, file_size_bytes=job.file_size_bytes, row_count=job.row_count,
            valid_row_count=job.valid_row_count, error_row_count=job.error_row_count,
            warning_row_count=job.warning_row_count, committed_row_count=job.committed_row_count,
            failed_row_count=job.failed_row_count, rejection_reason=job.rejection_reason, created_at=job.created_at,
            validated_at=job.validated_at, committed_at=job.committed_at, report_available=job.report_storage_key is not None,
        )


class BulkImportRowOut(BaseModel):
    id: uuid.UUID
    row_number: int
    sheet_name: str | None
    status: str
    action: str | None
    raw_data: dict
    errors: list
    warnings: list
    target_managed_asset_id: uuid.UUID | None
    target_catalog_model_id: uuid.UUID | None
    target_catalog_revision_id: uuid.UUID | None

    model_config = {"from_attributes": True}


async def dispatch_parse_job_or_fail(db: AsyncSession, job: BulkImportJob) -> None:
    """Shared by the three per-domain upload endpoints (app/api/v1/racks.py /
    equipment.py / catalog_designer.py) right after they create+commit a job at
    status='uploaded'.

    SEC (Codex PR #50 review, finding #7): `.delay()` can itself fail synchronously
    (broker unreachable, serialization error) -- without this, the client would receive
    a 202 for a job that no worker will ever pick up, and it would sit at
    status='uploaded' forever with no indication anything went wrong. There is nothing
    meaningful to "revert" the just-created job to (it was only ever 'uploaded'), so on
    dispatch failure it is instead marked 'failed_parse' with an explanatory
    rejection_reason -- the same terminal state a real parse rejection produces -- and
    the caller gets a 502 instead of a 202 that will never resolve."""
    try:
        parse_and_validate_bulk_import_job.delay(str(job.id))
    except Exception as exc:
        job.status = "failed_parse"
        job.rejection_reason = _DISPATCH_FAILURE_DETAIL
        await db.commit()
        logger.error("bulk_import_parse_dispatch_failed", job_id=str(job.id), error_code=type(exc).__name__)
        raise ApiError(status_code=502, title="Bad Gateway", detail=_DISPATCH_FAILURE_DETAIL) from exc


@router.get("/{job_id}", response_model=BulkImportJobOut)
async def get_bulk_import_job(job: BulkImportJob = Depends(_load_job_for_read)) -> BulkImportJobOut:
    return BulkImportJobOut.from_job(job)


@router.get("/{job_id}/rows", response_model=Page[BulkImportRowOut])
async def list_bulk_import_rows(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    job: BulkImportJob = Depends(_load_job_for_read),
) -> Page:
    stmt = select(BulkImportRow).where(BulkImportRow.job_id == job.id)
    count_stmt = select(func.count()).select_from(BulkImportRow).where(BulkImportRow.job_id == job.id)
    if status is not None:
        stmt = stmt.where(BulkImportRow.status == status)
        count_stmt = count_stmt.where(BulkImportRow.status == status)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(BulkImportRow.row_number).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/{job_id}/commit", response_model=BulkImportJobOut, status_code=202)
async def commit_bulk_import_job_endpoint(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> BulkImportJobOut:
    job = await _load_job(job_id, db)
    _check_import_permission(job, ctx)

    # SEC (Codex PR #50 review, finding #1): a plain read-then-check here ("is job.status
    # == 'validated'?") lets two concurrent commit requests both read 'validated' and both
    # dispatch a commit task against the same job -- service.py::run_commit's own
    # SELECT...FOR UPDATE only protects against a *second Celery delivery* of the *same*
    # dispatched task, not against two independently-dispatched tasks racing each other in
    # the first place. This single atomic UPDATE ... WHERE status='validated' is the only
    # place a job may ever move into 'committing' (see run_commit's docstring for the other
    # half of this fix, which now trusts that and never self-claims): exactly one
    # concurrent request's UPDATE can match the WHERE clause and flip the row, so exactly
    # one gets to proceed past this point.
    claimed_id = (
        await db.execute(
            update(BulkImportJob)
            .where(BulkImportJob.id == job_id, BulkImportJob.status == "validated")
            .values(status="committing")
            .returning(BulkImportJob.id)
        )
    ).scalar_one_or_none()
    if claimed_id is None:
        await db.rollback()
        raise ApiError(
            status_code=409, title="Conflict",
            detail=f"BulkImportJob {job_id} is not in a committable state; only a 'validated' job can be committed.",
        )

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action=f"{job.import_type}.bulk_import.commit", entity_type="bulk_import_job",
        entity_id=job.id, request_id=request_id, correlation_id=correlation_id, after={"mode": job.mode},
    )
    await db.commit()

    # SEC (Codex PR #50 review, finding #7): if the dispatch itself fails, this request
    # already claimed the job into 'committing' above -- release that claim back to
    # 'validated' (another atomic, WHERE-guarded UPDATE, matching the claim's own
    # discipline) so the job is not left permanently stuck claiming to be in progress, and
    # surface a 502 rather than a 202 that will never resolve.
    try:
        commit_bulk_import_job.delay(str(job.id))
    except Exception as exc:
        await db.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "committing").values(
                status="validated"
            )
        )
        await db.commit()
        logger.error("bulk_import_commit_dispatch_failed", job_id=str(job_id), error_code=type(exc).__name__)
        raise ApiError(status_code=502, title="Bad Gateway", detail=_DISPATCH_FAILURE_DETAIL) from exc

    await db.refresh(job)
    return BulkImportJobOut.from_job(job)


@router.post("/{job_id}/cancel", response_model=BulkImportJobOut)
async def cancel_bulk_import_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> BulkImportJobOut:
    job = await _load_job(job_id, db)
    _check_import_permission(job, ctx)
    if job.status not in ("uploaded", "validated"):
        raise ApiError(
            status_code=409, title="Conflict",
            detail=f"BulkImportJob {job_id} is {job.status!r}; only an 'uploaded' or 'validated' job can be cancelled.",
        )

    job.status = "cancelled"
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action=f"{job.import_type}.bulk_import.cancel", entity_type="bulk_import_job",
        entity_id=job.id, request_id=request_id, correlation_id=correlation_id, after={"status": "cancelled"},
    )
    await db.commit()
    await db.refresh(job)
    return BulkImportJobOut.from_job(job)


@router.get("/{job_id}/report")
async def get_bulk_import_report(job: BulkImportJob = Depends(_load_job_for_read)) -> StreamingResponse:
    if job.report_storage_key is None:
        raise ApiError(
            status_code=409, title="Conflict", detail=f"BulkImportJob {job.id} has no report available yet.",
        )
    storage = get_storage_backend()
    content = storage.read(job.report_storage_key)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="bulk-import-report-{job.id}.xlsx"'},
    )
