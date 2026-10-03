"""Extraction job lifecycle: request (idempotent), claim (lease + fencing token), complete or fail,
retry, and candidate review.

Concurrency design
- Request: `INSERT ... ON CONFLICT DO NOTHING` on (document, extractor_version). Two simultaneous
  requests create exactly one job; both callers get it.
- Claim: one compare-and-set UPDATE. A job is claimable when it is `queued`, or `running` with an
  expired lease (its worker died). The UPDATE writes a fresh `claim_token`, so a stale worker that
  wakes up later is recognised by its old token.
- Complete / fail / renew: UPDATE ... WHERE claim_token = :mine AND status = 'running'. Zero rows
  means this worker lost the claim; it writes nothing (candidates are inserted in the same
  transaction as the winning UPDATE, so a loser inserts none).
- A completed job is terminal (database trigger). A new extractor version is a new row, so earlier
  results stay as history. "Current" is simply the newest completed job of a document.
- Review decisions take the job row lock, so two reviewers resolving a conflict group serialise.

Nothing in this module writes to a catalog revision."""

import hashlib
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.catalog_documents.extraction.candidates import FIELD_KEYS
from app.application.catalog_documents.extraction.pipeline import (
    EXTRACTOR_VERSION,
    UNIT_REGISTRY_VERSION,
    ExtractionFailure,
    PipelineResult,
    run_pipeline,
)
from app.core.config import Settings
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.catalog.designer_models import CatalogModel
from app.domain.catalog.document_models import DOWNLOADABLE_SCAN_STATUSES, CatalogDocument
from app.domain.catalog.extraction_models import (
    CANDIDATE_METHODS,
    MODEL_MATCHES,
    CatalogExtractionCandidate,
    CatalogExtractionJob,
)
from app.infrastructure.storage import StorageBackend

logger = get_logger(__name__)

_MAX_FLAGS = 16


# ------------------------------------------------------------------------------ API side (async)


def ensure_document_extractable(document: CatalogDocument, settings: Settings) -> None:
    if document.catalog_model_id is None:
        raise ConflictError(
            detail="The document is not attached to a catalog model yet, so the model it describes is unknown. "
            "Upload it against the model first."
        )
    if document.scan_status not in DOWNLOADABLE_SCAN_STATUSES or (
        document.scan_status == "skipped" and settings.catalog_pdf_scan_mode == "required"
    ):
        raise ConflictError(detail="This document did not pass malware scanning and cannot be processed.")


async def request_extraction(
    db: AsyncSession, *, document: CatalogDocument, user_id: uuid.UUID, settings: Settings
) -> tuple[CatalogExtractionJob, bool]:
    """Returns `(job, created)`. Never commits. Identical requests (same document, same extractor
    version) return the existing job whatever its state; use `retry_extraction` to re-run a failure."""
    ensure_document_extractable(document, settings)
    model = await db.get(CatalogModel, document.catalog_model_id)
    if model is None:  # pragma: no cover - the foreign key makes this unreachable
        raise NotFoundError("The catalog model of this document no longer exists.")
    names = [n for n in (model.model_name, model.model_number) if n]
    statement = (
        pg_insert(CatalogExtractionJob)
        .values(
            catalog_document_id=document.id,
            catalog_model_id=document.catalog_model_id,
            document_sha256=document.sha256,
            extractor_version=EXTRACTOR_VERSION,
            unit_registry_version=UNIT_REGISTRY_VERSION,
            target_names=names,
            status="queued",
            requested_by_user_id=user_id,
            requested_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(constraint="uq_catalog_extraction_job_document_version")
        .returning(CatalogExtractionJob.id)
    )
    created_id = (await db.execute(statement)).scalar_one_or_none()
    job = (
        await db.execute(
            select(CatalogExtractionJob)
            .where(
                CatalogExtractionJob.catalog_document_id == document.id,
                CatalogExtractionJob.extractor_version == EXTRACTOR_VERSION,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return job, created_id is not None


async def retry_extraction(db: AsyncSession, *, job_id: uuid.UUID) -> CatalogExtractionJob:
    """Explicit re-run of a *failed* job: back to `queued` with a fresh attempt budget. A job that is
    queued, running or completed is refused, so two retry clicks cannot start two runs."""
    updated = (
        await db.execute(
            update(CatalogExtractionJob)
            .where(CatalogExtractionJob.id == job_id, CatalogExtractionJob.status == "failed")
            .values(
                status="queued",
                retry_count=CatalogExtractionJob.retry_count + 1,
                attempt_count=0,
                claim_token=None,
                lease_expires_at=None,
                error_code=None,
                finished_at=None,
                started_at=None,
            )
            .returning(CatalogExtractionJob.id)
        )
    ).scalar_one_or_none()
    if updated is None:
        if await db.get(CatalogExtractionJob, job_id) is None:
            raise NotFoundError(f"CatalogExtractionJob {job_id} not found.")
        raise ConflictError(detail="Only a failed extraction can be retried.")
    return (
        await db.execute(
            select(CatalogExtractionJob).where(CatalogExtractionJob.id == job_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def review_candidate(
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    user_id: uuid.UUID,
    decision: str,
    note: str | None,
    confirm_model_attribution: bool,
) -> tuple[CatalogExtractionCandidate, CatalogExtractionJob]:
    """Records one human decision. It does not apply the value anywhere. Rules:
    - a value attributed to another model can never be accepted for this one;
    - a value with no model evidence ('unattributed') needs `confirm_model_attribution`;
    - at most one candidate of a conflict group can be accepted;
    - a decision is final (the database refuses a second one)."""
    probe = await db.get(CatalogExtractionCandidate, candidate_id)
    if probe is None:
        raise NotFoundError(f"CatalogExtractionCandidate {candidate_id} not found.")
    job = await db.get(CatalogExtractionJob, probe.job_id, with_for_update=True)
    assert job is not None
    candidate = (
        await db.execute(
            select(CatalogExtractionCandidate)
            .where(CatalogExtractionCandidate.id == candidate_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if candidate.review_status != "pending":
        raise ConflictError(detail="This candidate was already reviewed.")
    if decision == "accepted":
        if candidate.model_match == "other":
            raise ApiError(
                status_code=422,
                title="Unprocessable Entity",
                detail="This value belongs to a different model in the datasheet and cannot be accepted for this model.",
            )
        if candidate.model_match == "unattributed" and not confirm_model_attribution:
            raise ApiError(
                status_code=422,
                title="Unprocessable Entity",
                detail="The datasheet gives no model evidence for this value. Confirm the attribution explicitly to accept it.",
            )
        if candidate.conflict_group_key is not None:
            already = (
                await db.execute(
                    select(func.count())
                    .select_from(CatalogExtractionCandidate)
                    .where(
                        CatalogExtractionCandidate.job_id == candidate.job_id,
                        CatalogExtractionCandidate.conflict_group_key == candidate.conflict_group_key,
                        CatalogExtractionCandidate.review_status == "accepted",
                    )
                )
            ).scalar_one()
            if already:
                raise ConflictError(detail="Another conflicting value was already accepted. Decisions are final.")
    candidate.review_status = decision
    candidate.reviewed_by_user_id = user_id
    candidate.reviewed_at = datetime.now(UTC)
    candidate.review_note = note[:500] if note else None
    candidate.model_attribution_confirmed = bool(confirm_model_attribution and candidate.model_match == "unattributed")
    await db.flush()
    return candidate, job


# ------------------------------------------------------------------------------ worker side (sync)


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    token: uuid.UUID
    document_id: uuid.UUID
    document_sha256: str
    storage_key: str
    target_names: list[str]


def claim_job(db: Session, job_id: uuid.UUID, *, settings: Settings) -> ClaimedJob | None:
    """Compare-and-set claim. Returns None when another worker owns a live lease, the job is already
    terminal, or its attempt budget is spent (in which case it is failed here, once)."""
    token = uuid.uuid4()
    now = func.now()
    claimable = (CatalogExtractionJob.status == "queued") | (
        (CatalogExtractionJob.status == "running") & (CatalogExtractionJob.lease_expires_at < now)
    )
    row = db.execute(
        update(CatalogExtractionJob)
        .where(
            CatalogExtractionJob.id == job_id,
            claimable,
            CatalogExtractionJob.attempt_count < settings.catalog_extraction_max_attempts,
        )
        .values(
            status="running",
            claim_token=token,
            lease_expires_at=now + timedelta(seconds=settings.catalog_extraction_lease_seconds),
            attempt_count=CatalogExtractionJob.attempt_count + 1,
            started_at=func.coalesce(CatalogExtractionJob.started_at, now),
        )
        .returning(
            CatalogExtractionJob.catalog_document_id, CatalogExtractionJob.document_sha256, CatalogExtractionJob.target_names
        )
    ).one_or_none()
    if row is None:
        db.execute(
            update(CatalogExtractionJob)
            .where(
                CatalogExtractionJob.id == job_id,
                claimable,
                CatalogExtractionJob.attempt_count >= settings.catalog_extraction_max_attempts,
            )
            .values(status="failed", error_code="max_attempts_exceeded", finished_at=now, claim_token=None, lease_expires_at=None)
        )
        db.commit()
        return None
    document_id, sha, names = row
    storage_key = db.execute(select(CatalogDocument.storage_key).where(CatalogDocument.id == document_id)).scalar_one()
    db.commit()
    return ClaimedJob(job_id, token, document_id, sha, storage_key, [str(n) for n in names])


def renew_lease(db: Session, claim: ClaimedJob, *, settings: Settings) -> bool:
    renewed = db.execute(
        update(CatalogExtractionJob)
        .where(
            CatalogExtractionJob.id == claim.job_id,
            CatalogExtractionJob.claim_token == claim.token,
            CatalogExtractionJob.status == "running",
        )
        .values(lease_expires_at=func.now() + timedelta(seconds=settings.catalog_extraction_lease_seconds))
        .returning(CatalogExtractionJob.id)
    ).scalar_one_or_none()
    db.commit()
    return renewed is not None


def _fail(db: Session, claim: ClaimedJob, code: str) -> bool:
    changed = db.execute(
        update(CatalogExtractionJob)
        .where(
            CatalogExtractionJob.id == claim.job_id,
            CatalogExtractionJob.claim_token == claim.token,
            CatalogExtractionJob.status == "running",
        )
        .values(status="failed", error_code=code, finished_at=func.now(), claim_token=None, lease_expires_at=None)
        .returning(CatalogExtractionJob.id)
    ).scalar_one_or_none()
    db.commit()
    return changed is not None


def _finite(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    return number if math.isfinite(number) and abs(number) < 1e12 else None


def candidate_row(job_id: uuid.UUID, sequence: int, raw: dict[str, object]) -> dict[str, object] | None:
    """Validates one candidate coming back from the analysis child; anything unexpected is dropped
    rather than stored."""
    field_key, method, match = str(raw.get("field_key", "")), str(raw.get("method", "")), str(raw.get("model_match", ""))
    if field_key not in FIELD_KEYS or method not in CANDIDATE_METHODS or match not in MODEL_MATCHES:
        return None
    numeric, upper = _finite(raw.get("value_numeric")), _finite(raw.get("value_max"))
    text = None if raw.get("value_text") is None else str(raw["value_text"])[:128]
    if numeric is None and text is None:
        return None
    flags = [str(f)[:48] for f in (raw.get("flags") or [])][:_MAX_FLAGS]  # type: ignore[attr-defined]
    page = int(raw.get("page_number", 0))  # type: ignore[call-overload]
    if page < 1:
        return None
    conflict = raw.get("conflict_group_key")
    context = raw.get("model_context")
    return {
        "job_id": job_id,
        "sequence": sequence,
        "field_key": field_key,
        "value_numeric": numeric,
        "value_max": upper,
        "value_text": text,
        "unit": None if raw.get("unit") is None else str(raw["unit"])[:16],
        "raw_value": str(raw.get("raw_value", ""))[:128],
        "raw_unit": str(raw.get("raw_unit", ""))[:32],
        "source_text": str(raw.get("source_text", ""))[:500],
        "page_number": page,
        "method": method,
        "confidence": round(min(1.0, max(0.0, float(raw.get("confidence", 0)))), 3),  # type: ignore[arg-type]
        "flags": flags,
        "model_context": None if context is None else str(context)[:64],
        "model_match": match,
        "conflict_group_key": None if conflict is None else str(conflict)[:160],
    }


def _complete(db: Session, claim: ClaimedJob, result: PipelineResult) -> bool:
    """One transaction: the fenced UPDATE first, the candidate inserts only if it won."""
    rows = [r for i, raw in enumerate(result.candidates) if (r := candidate_row(claim.job_id, i, raw)) is not None]
    won = db.execute(
        update(CatalogExtractionJob)
        .where(
            CatalogExtractionJob.id == claim.job_id,
            CatalogExtractionJob.claim_token == claim.token,
            CatalogExtractionJob.status == "running",
        )
        .values(
            status="completed",
            finished_at=func.now(),
            claim_token=None,
            lease_expires_at=None,
            error_code=None,
            outcome=result.outcome,
            method_summary=result.method_summary,
            model_resolution=result.model_resolution,
            identified_models=result.identified_models,
            warnings=result.warnings,
            pages_total=result.pages_total,
            pages_native=result.pages_native,
            pages_ocr=result.pages_ocr,
            pages_ocr_failed=result.pages_ocr_failed,
            candidate_count=len(rows),
        )
        .returning(CatalogExtractionJob.id)
    ).scalar_one_or_none()
    if won is None:
        db.rollback()
        return False
    if rows:
        db.execute(pg_insert(CatalogExtractionCandidate), rows)
    db.commit()
    return True


def run_extraction_job(
    job_id: uuid.UUID,
    *,
    settings: Settings,
    storage: StorageBackend,
    session_factory: Callable[[], Session],
    runner: Callable[..., PipelineResult] = run_pipeline,
) -> str:
    """Worker entry point. Returns `completed`, `failed`, `skipped` (not claimable) or `lost_claim`.
    Every exit path leaves the job terminal or claimable again; the stored document and catalog
    revisions are only ever read."""
    with session_factory() as db:
        claim = claim_job(db, job_id, settings=settings)
    if claim is None:
        return "skipped"

    def heartbeat() -> None:
        with session_factory() as beat_db:
            renew_lease(beat_db, claim, settings=settings)

    try:
        try:
            content = storage.read(claim.storage_key)
        except FileNotFoundError:
            raise ExtractionFailure("stored_object_missing") from None
        if hashlib.sha256(content).hexdigest() != claim.document_sha256:
            raise ExtractionFailure("stored_object_mismatch")
        result = runner(content, target_names=claim.target_names, settings=settings, heartbeat=heartbeat)
    except ExtractionFailure as failure:
        with session_factory() as db:
            _fail(db, claim, failure.code)
        logger.info("catalog_extraction_job_failed", job_id=str(job_id), error_code=failure.code)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - never log the message: it can contain document content
        with session_factory() as db:
            _fail(db, claim, "internal_error")
        logger.error("catalog_extraction_job_error", job_id=str(job_id), error_type=type(exc).__name__)
        return "failed"

    with session_factory() as db:
        won = _complete(db, claim, result)
    if not won:
        logger.warning("catalog_extraction_job_claim_lost", job_id=str(job_id))
        return "lost_claim"
    logger.info(
        "catalog_extraction_job_completed",
        job_id=str(job_id),
        candidates=len(result.candidates),
        outcome=result.outcome,
        method=result.method_summary,
        pages_ocr=result.pages_ocr,
        pages_ocr_failed=result.pages_ocr_failed,
    )
    return "completed"


def find_jobs_to_dispatch(
    db: Session, *, now: datetime | None = None, queued_grace_seconds: int = 120, limit: int = 50
) -> list[uuid.UUID]:
    """Jobs nobody is working on: queued past the grace period (the dispatch failed or was lost) or
    running with an expired lease (the worker died). Re-dispatching is safe; the claim decides."""
    instant = now or datetime.now(UTC)
    stale_queued = (CatalogExtractionJob.status == "queued") & (
        CatalogExtractionJob.updated_at < instant - timedelta(seconds=queued_grace_seconds)
    )
    expired = (CatalogExtractionJob.status == "running") & (CatalogExtractionJob.lease_expires_at < instant)
    rows = db.execute(
        select(CatalogExtractionJob.id).where(stale_queued | expired).order_by(CatalogExtractionJob.updated_at).limit(limit)
    )
    ids = list(rows.scalars())
    db.rollback()
    return ids
