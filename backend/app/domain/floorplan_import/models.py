"""Floor-plan import pipeline state (ARCHITECTURE_REVIEW.md §10/§10a/§11). An import job
never mutates authoritative inventory directly — its output is a queue of
FloorPlanImportCandidate rows an operator explicitly accepts or rejects (§21 of the
Phase 2 prompt: "Import should assist operators, not silently mutate authoritative
inventory."). See app/application/floorplan_import.py for the actual parse/sanitize
pipeline this state machine tracks."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

IMPORT_JOB_STATUSES = ("queued", "quarantined", "parsing", "parsed", "failed", "rejected")
CANDIDATE_STATUSES = ("pending", "accepted", "rejected")


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


class FloorPlanImportCandidate(Base, UUIDPkMixin, TimestampMixin):
    """A shape the importer found in the Sanitized Intermediate Representation, not yet
    (and possibly never) promoted to an authoritative SpatialObject. `raw_geometry` is
    SIR data only — normalized shape/text/coordinates, never raw source markup (§10a)."""

    __tablename__ = "floor_plan_import_candidate"
    __table_args__ = (
        CheckConstraint(f"status IN {CANDIDATE_STATUSES!r}", name="status_allowed"),
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
