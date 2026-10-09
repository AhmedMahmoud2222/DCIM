# ruff: noqa: E501
"""Sync persistence used by the import Celery tasks: stores the immutable SIR and the candidates derived from
it, in the caller's transaction. A failure anywhere rolls the whole job back to a clean failed state; no
partial candidate set is ever left behind."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.application.spatial_import.classify import classify
from app.application.spatial_import.sir import SIR_SCHEMA_VERSION, SirDocument, sir_sha256
from app.domain.floorplan_import.models import (
    FloorPlanImportCandidate,
    FloorPlanImportDiagnostics,
    FloorPlanImportJob,
    FloorPlanImportSir,
)


def new_diagnostics(job: FloorPlanImportJob, *, source_format: str, parser_name: str, parser_version: str) -> FloorPlanImportDiagnostics:
    return FloorPlanImportDiagnostics(
        job_id=job.id, source_format=source_format, parser_name=parser_name, parser_version=parser_version,
        started_at=datetime.now(UTC), objects_discovered=0, objects_classified=0, racks_detected=0, equipment_detected=0,
        unsupported_object_count=0, warnings=[], errors=[], ambiguous_count=0, rejected_count=0, confirmed_count=0,
        candidate_count=0,
    )


def store_parse_result(
    db: Session, job: FloorPlanImportJob, diagnostics: FloorPlanImportDiagnostics, document: SirDocument, raw_sir: dict
) -> None:
    digest = sir_sha256(raw_sir)
    db.add(FloorPlanImportSir(job_id=job.id, schema_version=SIR_SCHEMA_VERSION, sir_sha256=digest, sir=raw_sir))
    result = classify(document)
    for ordinal, draft in enumerate(result.drafts):
        is_rack = draft.suggested_object_type == "rack"
        db.add(
            FloorPlanImportCandidate(
                job_id=job.id, raw_geometry=draft.raw_geometry, suggested_object_type=draft.suggested_object_type,
                suggested_label=draft.suggested_label, confidence=draft.confidence, status="pending",
                source_ref=draft.source_ref[:128] or None, ordinal=ordinal, evidence=draft.evidence,
                match_status="unmatched" if is_rack else "not_applicable", version=1,
            )
        )
    diagnostics.source_format = document.source_format
    diagnostics.objects_discovered = document.objects_discovered
    diagnostics.objects_classified = len(result.drafts)
    diagnostics.candidate_count = len(result.drafts)
    diagnostics.racks_detected = result.racks_detected
    diagnostics.ambiguous_count = result.ambiguous_count
    diagnostics.confirmed_count = result.confirmed_count
    diagnostics.unsupported_object_count = document.unsupported_object_count
    diagnostics.warnings = [w["detail"] for w in document.warnings] + result.warnings
    diagnostics.source_units = document.source_units
    diagnostics.units_trusted = document.units_trusted
    diagnostics.y_axis = document.y_axis
    diagnostics.source_bbox = document.bbox()
    diagnostics.sir_sha256 = digest
    diagnostics.finished_at = datetime.now(UTC)
    assert diagnostics.started_at is not None
    diagnostics.duration_ms = int((diagnostics.finished_at - diagnostics.started_at).total_seconds() * 1000)
    job.status = "parsed"
    job.finished_at = diagnostics.finished_at


def mark_rejected(db: Session, job: FloorPlanImportJob, diagnostics: FloorPlanImportDiagnostics, *, code: str, reason: str) -> None:
    job.status = "failed"
    job.rejection_reason = reason[:500]
    job.finished_at = datetime.now(UTC)
    job.dedup_key = None  # a corrected re-upload of different bytes, or a retry after an infrastructure failure, is allowed
    diagnostics.errors = [reason]
    diagnostics.failure_code = code[:64]
    diagnostics.finished_at = job.finished_at
