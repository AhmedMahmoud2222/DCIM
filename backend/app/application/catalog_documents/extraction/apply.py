"""Explicit, atomic accepted-candidate handoff to a versioned catalog draft."""

import uuid
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog_designer_service import lock_draft_revision_for_edit
from app.application.catalog_documents.extraction.service import ensure_document_extractable
from app.application.catalog_documents.extraction.unit_handoff import convert_extracted_catalog_candidate
from app.core.config import Settings
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.catalog.designer_models import CatalogModelRevision
from app.domain.catalog.document_models import CatalogDocument, CatalogRevisionDocument
from app.domain.catalog.extraction_models import CatalogExtractionCandidate, CatalogExtractionJob
from app.domain.telemetry.registry import REGISTRY_VERSION, convert_value

# Semantically distinct fields must never be merged merely because their units match.
FIELD_TARGETS = {
    "width": "width_value", "height": "height_value", "depth": "depth_value",
    "weight": "weight_value", "power_rated_w": "rated_power_w",
    "power_typical_w": "typical_power_w", "power_max_w": "max_power_w",
}
BLOCKING_FLAGS = {
    "number_format_ambiguous", "unit_missing", "unit_unrecognized", "unit_dimension_mismatch",
    "dimensions_order_unknown",
}


def candidate_patch(candidate: CatalogExtractionCandidate, revision: CatalogModelRevision) -> tuple[dict, dict]:
    """Convert via #108, retaining authored draft units and every untouched axis."""
    if candidate.field_key not in FIELD_TARGETS:
        raise ValueError(f"No supported scalar catalog field for {candidate.field_key}.")
    if BLOCKING_FLAGS.intersection(candidate.flags or []):
        raise ValueError("Resolve ambiguous numbers, axis order or missing units before applying.")
    canonical = convert_extracted_catalog_candidate(candidate)
    target = FIELD_TARGETS[candidate.field_key]
    unit_column = "dimension_unit" if candidate.field_key in {"width", "height", "depth"} else (
        "weight_unit" if candidate.field_key == "weight" else None
    )
    unit = getattr(revision, unit_column) if unit_column else canonical.unit
    if unit_column and unit is None:
        siblings = ("width_value", "height_value", "depth_value") if unit_column == "dimension_unit" else ("weight_value",)
        if any(getattr(revision, name) is not None for name in siblings):
            raise ValueError("Existing draft values have no unit; correct the draft before applying.")
    unit = unit or canonical.unit
    value = convert_value(canonical.value, canonical.unit, unit)
    scale = 3 if unit_column else 2
    value = value.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)
    if value < 0 or (unit_column and value == 0) or value >= Decimal(10) ** (10 - scale):
        raise ValueError("Converted value is outside the catalog field's storage range.")
    patch: dict[str, Any] = {target: value}
    if unit_column:
        patch[unit_column] = unit
    evidence = {
        "candidate_id": str(candidate.id), "field_key": candidate.field_key, "target_field": target,
        "raw_value": candidate.raw_value, "raw_unit": candidate.raw_unit,
        "source_value": str(canonical.source_value), "source_unit": canonical.source_unit,
        "canonical_value": str(canonical.value), "canonical_unit": canonical.unit,
        "registry_version": canonical.registry_version, "applied_value": str(value), "applied_unit": unit,
        "source_text": candidate.source_text, "page_number": candidate.page_number,
        "method": candidate.method, "confidence": str(candidate.confidence), "flags": list(candidate.flags or []),
        "model_context": candidate.model_context, "model_match": candidate.model_match,
        "model_attribution_confirmed": candidate.model_attribution_confirmed,
        "reviewed_by_user_id": str(candidate.reviewed_by_user_id),
        "reviewed_at": candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
        "review_note": candidate.review_note,
    }
    return patch, evidence


async def apply_candidates(
    db: AsyncSession, *, revision_id: uuid.UUID, document_id: uuid.UUID, job_id: uuid.UUID,
    candidate_ids: list[uuid.UUID], if_match_version: int, overwrite_existing: bool, settings: Settings,
) -> tuple[CatalogModelRevision, dict, dict, list[dict]]:
    """Never commits. Caller writes provenance/audit/outbox before the single commit.

    Revision first (same lock order as attachment), then job (same lock as review),
    then document. All selection validation precedes any scalar assignment.
    """
    if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
        raise ApiError(status_code=422, title="Invalid selection", detail="Select distinct candidate IDs.")
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    job = (await db.execute(select(CatalogExtractionJob).where(CatalogExtractionJob.id == job_id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if job is None:
        raise NotFoundError("Extraction job not found.")
    document = (await db.execute(select(CatalogDocument).where(CatalogDocument.id == document_id)
                                 .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document not found.")
    if (job.catalog_document_id != document.id or job.catalog_model_id != revision.catalog_model_id
            or document.catalog_model_id != revision.catalog_model_id or job.document_sha256 != document.sha256):
        raise ConflictError("The document, job and target draft must describe the same model and document bytes.")
    ensure_document_extractable(document, settings)
    attached = (await db.execute(select(CatalogRevisionDocument.id).where(
        CatalogRevisionDocument.catalog_model_revision_id == revision.id,
        CatalogRevisionDocument.catalog_document_id == document.id,
    ))).scalar_one_or_none()
    if attached is None:
        raise ConflictError("Attach this datasheet to the target draft before applying.")
    current = (await db.execute(select(CatalogExtractionJob.id).where(
        CatalogExtractionJob.catalog_document_id == document.id, CatalogExtractionJob.status == "completed",
    ).order_by(CatalogExtractionJob.finished_at.desc(), CatalogExtractionJob.requested_at.desc()).limit(1))).scalar_one_or_none()
    if job.status != "completed" or current != job.id:
        raise ConflictError("Only the current completed extraction job can be applied.")
    if job.unit_registry_version != REGISTRY_VERSION:
        raise ConflictError("The extraction unit registry version is not supported by this apply operation.")
    if job.model_resolution in {"target_not_found", "ambiguous_target"}:
        raise ConflictError("The extraction did not resolve the target model safely.")
    candidates = list((await db.execute(select(CatalogExtractionCandidate).where(
        CatalogExtractionCandidate.id.in_(candidate_ids), CatalogExtractionCandidate.job_id == job.id,
    ).order_by(CatalogExtractionCandidate.sequence).execution_options(populate_existing=True))).scalars())
    if len(candidates) != len(candidate_ids):
        raise ConflictError("Every selected candidate must belong to this document's extraction job.")
    patch, evidence = {}, []
    for candidate in candidates:
        if candidate.review_status != "accepted" or candidate.model_match == "other" or (
            candidate.model_match == "unattributed" and not candidate.model_attribution_confirmed
        ):
            raise ConflictError("Only accepted candidates with confirmed target-model attribution can be applied.")
        try:
            changes, source = candidate_patch(candidate, revision)
        except ValueError as exc:
            raise ApiError(status_code=422, title="Unsupported candidate", detail=str(exc)) from exc
        field = source["target_field"]
        if field in patch:
            raise ConflictError("Select only one candidate per target field, including duplicate values.")
        if getattr(revision, field) is not None and not overwrite_existing:
            raise ConflictError("The draft already has a selected field. Explicit overwrite confirmation is required.")
        patch.update(changes)
        evidence.append(source)
    before = {key: None if getattr(revision, key) is None else str(getattr(revision, key)) for key in patch}
    for key, value in patch.items():
        setattr(revision, key, value)
    after = {key: str(value) for key, value in patch.items()}
    return revision, before, after, evidence
