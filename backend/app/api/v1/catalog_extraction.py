"""DCIM01 PDF datasheet import, PR-B: request an extraction of candidate values from a stored
datasheet, follow the job, and review the candidates.

Review records a decision without changing a revision. A separate explicit application operation
copies accepted scalar values into a version-matched draft with immutable provenance.

Authorization mirrors the datasheet itself, because candidates carry text copied from the file:
- request / retry / review: `catalog:manage` plus Administrator, like every catalog write;
- reads: `catalog:document_download`, and `catalog:read_draft` as well while the document is not
  linked to a published or retired revision (the same rule as the file download).
Every job and candidate is reached through its document, so a direct ID never bypasses that rule."""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog_designer import _request_ids
from app.api.v1.catalog_documents import require_draft_stage_access
from app.application.audit_service import write_audit_log
from app.application.catalog_documents.extraction.apply import apply_candidates
from app.application.catalog_documents.extraction.pipeline import FAILURE_MESSAGES
from app.application.catalog_documents.extraction.service import request_extraction, retry_extraction, review_candidate
from app.application.concurrency import require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.rbac import AuthContext, require_catalog_administrator, require_permission
from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, NotFoundError
from app.domain.catalog.application_models import CatalogExtractionApplication
from app.domain.catalog.document_models import CatalogDocument
from app.domain.catalog.extraction_models import CatalogExtractionCandidate, CatalogExtractionJob
from app.infrastructure.tasks.catalog_extraction import dispatch_extraction_job

router = APIRouter(prefix="/catalog", tags=["catalog-extraction"])


class ExtractionApplyIn(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    overwrite_existing: bool = False


class ExtractionApplicationOut(BaseModel):
    id: uuid.UUID
    revision_id: uuid.UUID
    document_id: uuid.UUID
    job_id: uuid.UUID
    actor_user_id: uuid.UUID
    revision_version: int
    document_sha256: str
    extractor_version: str
    extraction_unit_registry_version: str
    before_values: dict[str, Any]
    after_values: dict[str, Any]
    candidates: list[dict[str, Any]]
    applied_at: datetime

    model_config = {"from_attributes": True}


@router.post("/revisions/{revision_id}/extraction-applications", response_model=ExtractionApplicationOut, status_code=201)
async def apply_extraction_to_draft(
    revision_id: uuid.UUID, body: ExtractionApplyIn, request: Request,
    db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> ExtractionApplicationOut:
    if not ctx.has_permission("catalog:read_draft") or not ctx.has_permission("catalog:document_download"):
        raise ForbiddenError("Applying extraction requires draft-read and document-download permissions.")
    actor_id = ctx.user.id
    revision, before, after, evidence = await apply_candidates(
        db, revision_id=revision_id, document_id=body.document_id, job_id=body.job_id,
        candidate_ids=body.candidate_ids, if_match_version=if_match_version,
        overwrite_existing=body.overwrite_existing, settings=settings,
    )
    job = await db.get(CatalogExtractionJob, body.job_id)
    assert job is not None  # apply_candidates locked and validated the job in this transaction
    application = CatalogExtractionApplication(
        revision_id=revision.id, document_id=body.document_id, job_id=body.job_id,
        actor_user_id=actor_id, revision_version=revision.version, document_sha256=job.document_sha256,
        extractor_version=job.extractor_version, extraction_unit_registry_version=job.unit_registry_version,
        before_values=before, after_values=after, candidates=evidence,
    )
    db.add(application)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=actor_id, action="catalog.extraction.apply", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after={"application_id": str(application.id), "version": revision.version, "values": after,
               "document_id": str(body.document_id), "job_id": str(body.job_id), "overwrite_existing": body.overwrite_existing},
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftUpdated", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"version": revision.version, "application_id": str(application.id)},
        correlation_id=correlation_id,
    )
    await db.commit()
    return ExtractionApplicationOut.model_validate(application)


@router.get("/revisions/{revision_id}/extraction-applications", response_model=list[ExtractionApplicationOut])
async def list_extraction_applications(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:document_download")),
) -> list[ExtractionApplicationOut]:
    from app.api.v1.catalog_designer import _revision_read_dependency

    # The same lifecycle-sensitive rule as reading the revision itself.
    await _revision_read_dependency(revision_id=revision_id, db=db, ctx=ctx)
    rows = (await db.execute(select(CatalogExtractionApplication).where(
        CatalogExtractionApplication.revision_id == revision_id,
    ).order_by(CatalogExtractionApplication.revision_version.desc()))).scalars()
    return [ExtractionApplicationOut.model_validate(row) for row in rows]


class ExtractionJobOut(BaseModel):
    id: uuid.UUID
    catalog_document_id: uuid.UUID
    catalog_model_id: uuid.UUID
    document_sha256: str
    extractor_version: str
    unit_registry_version: str
    status: str
    is_current: bool
    attempt_count: int
    retry_count: int
    requested_by_user_id: uuid.UUID
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    outcome: str | None
    method_summary: str | None
    model_resolution: str | None
    identified_models: list[str]
    warnings: list[dict[str, Any]]
    pages_total: int | None
    pages_native: int | None
    pages_ocr: int | None
    pages_ocr_failed: int | None
    candidate_count: int | None


class ExtractionCandidateOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    sequence: int
    field_key: str
    value_numeric: float | None
    value_max: float | None
    value_text: str | None
    unit: str | None
    raw_value: str
    raw_unit: str
    source_text: str
    page_number: int
    method: str
    confidence: float
    flags: list[str]
    model_context: str | None
    model_match: str
    conflict_group_key: str | None
    review_status: str
    reviewed_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    model_attribution_confirmed: bool


class CandidateReviewIn(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=500)
    confirm_model_attribution: bool = False


def _job_out(job: CatalogExtractionJob, *, current_id: uuid.UUID | None) -> ExtractionJobOut:
    return ExtractionJobOut(
        id=job.id,
        catalog_document_id=job.catalog_document_id,
        catalog_model_id=job.catalog_model_id,
        document_sha256=job.document_sha256,
        extractor_version=job.extractor_version,
        unit_registry_version=job.unit_registry_version,
        status=job.status,
        is_current=job.id == current_id,
        attempt_count=job.attempt_count,
        retry_count=job.retry_count,
        requested_by_user_id=job.requested_by_user_id,
        requested_at=job.requested_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_code=job.error_code,
        error_message=FAILURE_MESSAGES.get(job.error_code or "", "Extraction failed.") if job.error_code else None,
        outcome=job.outcome,
        method_summary=job.method_summary,
        model_resolution=job.model_resolution,
        identified_models=[str(m) for m in job.identified_models or []],
        warnings=list(job.warnings or []),
        pages_total=job.pages_total,
        pages_native=job.pages_native,
        pages_ocr=job.pages_ocr,
        pages_ocr_failed=job.pages_ocr_failed,
        candidate_count=job.candidate_count,
    )


def _candidate_out(c: CatalogExtractionCandidate) -> ExtractionCandidateOut:
    return ExtractionCandidateOut(
        id=c.id,
        job_id=c.job_id,
        sequence=c.sequence,
        field_key=c.field_key,
        value_numeric=None if c.value_numeric is None else float(c.value_numeric),
        value_max=None if c.value_max is None else float(c.value_max),
        value_text=c.value_text,
        unit=c.unit,
        raw_value=c.raw_value,
        raw_unit=c.raw_unit,
        source_text=c.source_text,
        page_number=c.page_number,
        method=c.method,
        confidence=float(c.confidence),
        flags=[str(f) for f in c.flags or []],
        model_context=c.model_context,
        model_match=c.model_match,
        conflict_group_key=c.conflict_group_key,
        review_status=c.review_status,
        reviewed_by_user_id=c.reviewed_by_user_id,
        reviewed_at=c.reviewed_at,
        review_note=c.review_note,
        model_attribution_confirmed=c.model_attribution_confirmed,
    )


async def _current_job_id(db: AsyncSession, document_id: uuid.UUID) -> uuid.UUID | None:
    """The newest completed job of the document. Older jobs stay readable as history."""
    return (
        await db.execute(
            select(CatalogExtractionJob.id)
            .where(CatalogExtractionJob.catalog_document_id == document_id, CatalogExtractionJob.status == "completed")
            .order_by(CatalogExtractionJob.finished_at.desc(), CatalogExtractionJob.requested_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _document(db: AsyncSession, document_id: uuid.UUID) -> CatalogDocument:
    document = await db.get(CatalogDocument, document_id)
    if document is None:
        raise NotFoundError(f"CatalogDocument {document_id} not found.")
    return document


async def _job_for_reader(db: AsyncSession, ctx: AuthContext, job_id: uuid.UUID) -> CatalogExtractionJob:
    job = await db.get(CatalogExtractionJob, job_id)
    if job is None:
        raise NotFoundError(f"CatalogExtractionJob {job_id} not found.")
    await require_draft_stage_access(db, ctx, job.catalog_document_id)
    return job


@router.post("/documents/{document_id}/extraction-jobs", response_model=ExtractionJobOut, status_code=202)
async def request_document_extraction(
    document_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> ExtractionJobOut:
    """Idempotent: the same document and extractor version return the existing job (HTTP 200)."""
    document = await _document(db, document_id)
    job, created = await request_extraction(db, document=document, user_id=ctx.user.id, settings=settings)
    if created:
        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db,
            actor_user_id=ctx.user.id,
            action="catalog.extraction.request",
            entity_type="catalog_extraction_job",
            entity_id=job.id,
            request_id=request_id,
            correlation_id=correlation_id,
            after={"document_id": str(document.id), "extractor_version": job.extractor_version},
        )
    await db.commit()
    if created:
        dispatch_extraction_job(job.id)
    else:
        response.status_code = 200
    return _job_out(job, current_id=await _current_job_id(db, document_id))


@router.get("/documents/{document_id}/extraction-jobs", response_model=list[ExtractionJobOut])
async def list_document_extraction_jobs(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:document_download")),
) -> list[ExtractionJobOut]:
    await _document(db, document_id)
    await require_draft_stage_access(db, ctx, document_id)
    jobs = list(
        (
            await db.execute(
                select(CatalogExtractionJob)
                .where(CatalogExtractionJob.catalog_document_id == document_id)
                .order_by(CatalogExtractionJob.requested_at.desc(), CatalogExtractionJob.id)
            )
        ).scalars()
    )
    current = await _current_job_id(db, document_id)
    return [_job_out(job, current_id=current) for job in jobs]


@router.get("/extraction-jobs/{job_id}", response_model=ExtractionJobOut)
async def get_extraction_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:document_download")),
) -> ExtractionJobOut:
    job = await _job_for_reader(db, ctx, job_id)
    return _job_out(job, current_id=await _current_job_id(db, job.catalog_document_id))


@router.post("/extraction-jobs/{job_id}/retry", response_model=ExtractionJobOut, status_code=202)
async def retry_extraction_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> ExtractionJobOut:
    job = await retry_extraction(db, job_id=job_id)
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="catalog.extraction.retry",
        entity_type="catalog_extraction_job",
        entity_id=job.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"retry_count": job.retry_count},
    )
    await db.commit()
    dispatch_extraction_job(job.id)
    return _job_out(job, current_id=await _current_job_id(db, job.catalog_document_id))


@router.get("/extraction-jobs/{job_id}/candidates", response_model=list[ExtractionCandidateOut])
async def list_extraction_candidates(
    job_id: uuid.UUID,
    field_key: str | None = Query(default=None, max_length=40),
    model_match: Literal["target", "other", "unattributed"] | None = Query(default=None),
    include_other_models: bool = Query(default=False),
    review_status: Literal["pending", "accepted", "rejected"] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:document_download")),
) -> list[ExtractionCandidateOut]:
    await _job_for_reader(db, ctx, job_id)
    query = select(CatalogExtractionCandidate).where(CatalogExtractionCandidate.job_id == job_id)
    if field_key is not None:
        query = query.where(CatalogExtractionCandidate.field_key == field_key)
    if model_match is not None:
        query = query.where(CatalogExtractionCandidate.model_match == model_match)
    elif not include_other_models:
        # Values the datasheet ties to a different model are not offered unless asked for by name.
        query = query.where(CatalogExtractionCandidate.model_match != "other")
    if review_status is not None:
        query = query.where(CatalogExtractionCandidate.review_status == review_status)
    rows = (await db.execute(query.order_by(CatalogExtractionCandidate.sequence).limit(limit).offset(offset))).scalars()
    return [_candidate_out(c) for c in rows]


@router.post("/extraction-candidates/{candidate_id}/review", response_model=ExtractionCandidateOut)
async def review_extraction_candidate(
    candidate_id: uuid.UUID,
    body: CandidateReviewIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> ExtractionCandidateOut:
    """Records a decision only. The value is not written to any revision."""
    probe = await db.get(CatalogExtractionCandidate, candidate_id)
    if probe is None:
        raise NotFoundError(f"CatalogExtractionCandidate {candidate_id} not found.")
    await _job_for_reader(db, ctx, probe.job_id)
    actor_id = ctx.user.id
    candidate, job = await review_candidate(
        db,
        candidate_id=candidate_id,
        user_id=actor_id,
        decision=body.decision,
        note=body.note,
        confirm_model_attribution=body.confirm_model_attribution,
    )
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=actor_id,
        action="catalog.extraction.candidate_review",
        entity_type="catalog_extraction_candidate",
        entity_id=candidate.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={
            "decision": candidate.review_status,
            "field_key": candidate.field_key,
            "job_id": str(job.id),
            "attribution_confirmed": candidate.model_attribution_confirmed,
        },
    )
    await db.commit()
    return _candidate_out(candidate)
