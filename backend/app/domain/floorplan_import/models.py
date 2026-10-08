"""Floor-plan import pipeline state (ARCHITECTURE_REVIEW.md §10/§10a/§11). An import job
never mutates authoritative inventory directly — its output is a queue of
FloorPlanImportCandidate rows an operator explicitly accepts or rejects (§21 of the
Phase 2 prompt: "Import should assist operators, not silently mutate authoritative
inventory."). See app/application/floorplan_import.py for the actual parse/sanitize
pipeline this state machine tracks."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

IMPORT_JOB_STATUSES = ("queued", "quarantined", "parsing", "parsed", "failed", "rejected")
CANDIDATE_STATUSES = ("pending", "accepted", "rejected")
MATCH_STATUSES = ("not_applicable", "unmatched", "matched", "ambiguous", "conflict", "duplicate")


class FloorPlanImportJob(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "floor_plan_import_job"
    __table_args__ = (CheckConstraint(f"status IN {IMPORT_JOB_STATUSES!r}", name="status_allowed"),)

    floor_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("floor_plan.id", ondelete="CASCADE"), nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Issue #104: format proven from content (never the filename), what the client claimed, and the
    # identical-upload key (floor plan + content hash while the job is live; cleared on failure so a corrected
    # re-upload of the same bytes after an infrastructure failure is possible).
    detected_format: Mapped[str | None] = mapped_column(String(8))
    declared_format: Mapped[str | None] = mapped_column(String(32))
    dedup_key: Mapped[str | None] = mapped_column(String(64))


class FloorPlanImportSir(Base, UUIDPkMixin):
    """The immutable sanitized intermediate representation of one job (UPDATE rejected by a trigger in
    migration 0044). Candidates are derived from it; it is never inventory."""

    __tablename__ = "floor_plan_import_sir"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("floor_plan_import_job.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sir_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sir: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class FloorPlanImportDiagnostics(Base, UUIDPkMixin):
    """One row per job (§11) — surfaces *why* an import produced poor/partial detection
    from stored data, never a bare 'Import failed.'"""

    __tablename__ = "floor_plan_import_diagnostics"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("floor_plan_import_job.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    source_format: Mapped[str | None] = mapped_column(String(8))
    parser_name: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(32))

    objects_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    objects_classified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    racks_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    equipment_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsupported_object_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Issue #104
    source_units: Mapped[str | None] = mapped_column(String(8))
    units_trusted: Mapped[bool | None] = mapped_column(Boolean)
    y_axis: Mapped[str | None] = mapped_column(String(4))
    source_bbox: Mapped[dict | None] = mapped_column(JSONB)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sir_sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class FloorPlanImportCandidate(Base, UUIDPkMixin, TimestampMixin):
    """A shape the importer found in the Sanitized Intermediate Representation, not yet
    (and possibly never) promoted to an authoritative SpatialObject. `raw_geometry` is
    SIR data only — normalized shape/text/coordinates, never raw source markup (§10a)."""

    __tablename__ = "floor_plan_import_candidate"
    __table_args__ = (
        CheckConstraint(f"status IN {CANDIDATE_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"match_status IN {MATCH_STATUSES!r}", name="match_status_allowed"),
        Index("ix_floor_plan_import_candidate_job_status", "job_id", "status"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("floor_plan_import_job.id", ondelete="CASCADE"), nullable=False)
    raw_geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    suggested_object_type: Mapped[str | None] = mapped_column(String(32))
    suggested_label: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    matched_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    resulting_spatial_object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spatial_object.id", ondelete="SET NULL")
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Issue #104: optimistic-concurrency token, stable per-source reference, detection/match evidence, and the
    # operator's staged corrections (in *source* coordinates, so a recalibration re-maps them). `raw_geometry`
    # stays exactly what the parser produced; the effective geometry is raw overlaid with `correction`.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source_ref: Mapped[str | None] = mapped_column(String(128))
    ordinal: Mapped[int | None] = mapped_column(Integer)  # position in the SIR's classification order (stable listing)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    match_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="not_applicable", server_default="not_applicable"
    )
    match_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    duplicate_of_spatial_object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spatial_object.id", ondelete="SET NULL")
    )
    reconciled_calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("floor_plan_calibration.id", ondelete="SET NULL")
    )
    correction: Mapped[dict | None] = mapped_column(JSONB)
    correction_history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
