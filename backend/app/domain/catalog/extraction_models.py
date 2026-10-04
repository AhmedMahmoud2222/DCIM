"""DCIM01 PDF datasheet import, PR-B: extraction jobs and candidate values
(docs/CATALOG_DATASHEET_EXTRACTION.md).

`CatalogExtractionJob` is one run of one extractor version over one stored document. The pair
(document, extractor_version) is unique, which makes a repeated request idempotent and lets a new
extractor version add a second job without replacing the first (history is kept). A job is claimed
by a worker with a lease and a fencing token, so two workers can never both publish a result.

`CatalogExtractionCandidate` is one value read from the document, with everything needed to audit
it: the manufacturer's raw value and unit, the parsed value and unit, the exact supporting text, the
page, the method, a confidence, flags, and the model evidence. Candidates are immutable apart from a
single review decision (migration 0033 enforces that with a trigger). Nothing here is ever copied
into a catalog revision by this feature."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

JOB_STATUSES = ("queued", "running", "completed", "failed")
JOB_OUTCOMES = ("complete", "partial")
METHOD_SUMMARIES = ("native", "ocr", "mixed", "none")
MODEL_RESOLUTIONS = (
    "single_model_matched",
    "multi_model_matched",
    "target_not_found",
    "ambiguous_target",
    "no_model_evidence",
)
CANDIDATE_METHODS = ("native", "table", "ocr")
MODEL_MATCHES = ("target", "other", "unattributed")
REVIEW_STATUSES = ("pending", "accepted", "rejected")


class CatalogExtractionJob(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "catalog_extraction_job"
    __table_args__ = (
        UniqueConstraint("catalog_document_id", "extractor_version", name="uq_catalog_extraction_job_document_version"),
        CheckConstraint(f"status IN {JOB_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"outcome IS NULL OR outcome IN {JOB_OUTCOMES!r}", name="outcome_allowed"),
        CheckConstraint(f"method_summary IS NULL OR method_summary IN {METHOD_SUMMARIES!r}", name="method_summary_allowed"),
        CheckConstraint(
            f"model_resolution IS NULL OR model_resolution IN {MODEL_RESOLUTIONS!r}", name="model_resolution_allowed"
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "(status = 'completed') = (outcome IS NOT NULL AND model_resolution IS NOT NULL AND finished_at IS NOT NULL)"
            " AND (status <> 'completed' OR error_code IS NULL)",
            name="completed_has_result",
        ),
        CheckConstraint("status <> 'failed' OR (error_code IS NOT NULL AND finished_at IS NOT NULL)", name="failed_has_error"),
        CheckConstraint(
            "(status = 'running') = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)", name="running_has_lease"
        ),
        CheckConstraint("char_length(document_sha256) = 64", name="document_sha256_length"),
    )

    catalog_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_model.id", ondelete="RESTRICT"), nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    unit_registry_version: Mapped[str] = mapped_column(String(16), nullable=False)
    target_names: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(48))
    outcome: Mapped[str | None] = mapped_column(String(16))
    method_summary: Mapped[str | None] = mapped_column(String(8))
    model_resolution: Mapped[str | None] = mapped_column(String(32))
    identified_models: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    pages_total: Mapped[int | None] = mapped_column(Integer)
    pages_native: Mapped[int | None] = mapped_column(Integer)
    pages_ocr: Mapped[int | None] = mapped_column(Integer)
    pages_ocr_failed: Mapped[int | None] = mapped_column(Integer)
    candidate_count: Mapped[int | None] = mapped_column(BigInteger)


class CatalogExtractionCandidate(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "catalog_extraction_candidate"
    __table_args__ = (
        CheckConstraint(f"method IN {CANDIDATE_METHODS!r}", name="method_allowed"),
        CheckConstraint(f"model_match IN {MODEL_MATCHES!r}", name="model_match_allowed"),
        CheckConstraint(f"review_status IN {REVIEW_STATUSES!r}", name="review_status_allowed"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("value_numeric IS NOT NULL OR value_text IS NOT NULL", name="has_value"),
        CheckConstraint(
            "(review_status = 'pending') = (reviewed_by_user_id IS NULL AND reviewed_at IS NULL)", name="review_consistent"
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_extraction_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(18, 6))
    value_max: Mapped[float | None] = mapped_column(Numeric(18, 6))
    value_text: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str | None] = mapped_column(String(16))
    raw_value: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    source_text: Mapped[str] = mapped_column(String(500), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    model_context: Mapped[str | None] = mapped_column(String(64))
    model_match: Mapped[str] = mapped_column(String(16), nullable=False)
    conflict_group_key: Mapped[str | None] = mapped_column(String(160), index=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(500))
    model_attribution_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
