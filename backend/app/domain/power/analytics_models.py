"""Issue #102: historical utilization snapshots and queued operational reports.

`PowerUtilizationSnapshot` is the prepared aggregate every trend, forecast and report reads, so no
request ever scans raw telemetry. A row is the roll-up of one scope over one UTC time bucket, computed
by `app/application/power_history.py` from canonical-unit telemetry (`power_kw`, kW) and the power
topology. A row is written once per (bucket, scope) and never rewritten: re-running a bucket is a
no-op, and raw `telemetry_reading` rows are never touched.

`PowerReportJob` is the queue row for an operational report generated on the `reports` Celery queue.
The finished report is kept on the row as bounded JSON; CSV is rendered from it on download. Failure
detail is a fixed code, never an exception message."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

SNAPSHOT_SCOPE_TYPES = ("power_node", "site")
SNAPSHOT_GRANULARITIES = ("hour",)
SNAPSHOT_QUALITIES = ("measured", "estimated", "mixed", "stale", "missing")
SNAPSHOT_LOAD_BASES = ("measured", "estimated", "none")

REPORT_TYPES = ("operational",)
REPORT_FORMATS = ("json", "csv")
REPORT_STATUSES = ("queued", "running", "completed", "failed")
REPORT_FAILURE_CODES = ("GENERATION_FAILED", "RESULT_TOO_LARGE", "ATTEMPTS_EXHAUSTED")


class PowerUtilizationSnapshot(Base, UUIDPkMixin):
    __tablename__ = "power_utilization_snapshot"
    __table_args__ = (
        UniqueConstraint("granularity", "bucket_start", "scope_type", "scope_id", name="uq_power_snapshot_bucket_scope"),
        CheckConstraint(f"granularity IN {SNAPSHOT_GRANULARITIES!r}", name="granularity_allowed"),
        CheckConstraint(f"scope_type IN {SNAPSHOT_SCOPE_TYPES!r}", name="scope_type_allowed"),
        CheckConstraint(f"quality IN {SNAPSHOT_QUALITIES!r}", name="quality_allowed"),
        CheckConstraint(f"load_basis IN {SNAPSHOT_LOAD_BASES!r}", name="load_basis_allowed"),
        CheckConstraint("bucket_end > bucket_start", name="bucket_ordered"),
        CheckConstraint("window_end > window_start", name="window_ordered"),
        CheckConstraint("metric = 'power_kw' AND unit = 'kW'", name="canonical_metric_and_unit"),
        CheckConstraint("load_kw IS NULL OR load_kw >= 0", name="load_kw_non_negative"),
        CheckConstraint("effective_capacity_kw IS NULL OR effective_capacity_kw >= 0", name="capacity_kw_non_negative"),
        CheckConstraint("sample_count >= 0 AND expected_samples >= 0", name="sample_counts_non_negative"),
        CheckConstraint("coverage_ratio >= 0 AND coverage_ratio <= 1", name="coverage_ratio_range"),
        CheckConstraint("(load_basis = 'none') = (load_kw IS NULL)", name="load_basis_matches_load"),
        Index("ix_power_snapshot_scope_bucket", "scope_type", "scope_id", "bucket_start"),
        Index("ix_power_snapshot_site_bucket", "site_id", "bucket_start"),
    )

    granularity: Mapped[str] = mapped_column(String(8), nullable=False, default="hour")
    bucket_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(32), nullable=False, default="power_kw")
    unit: Mapped[str] = mapped_column(String(8), nullable=False, default="kW")
    registry_version: Mapped[str | None] = mapped_column(String(16))
    load_kw: Mapped[float | None] = mapped_column(Numeric(14, 4))
    load_basis: Mapped[str] = mapped_column(String(16), nullable=False)
    allocated_kw: Mapped[float | None] = mapped_column(Numeric(14, 4))
    effective_capacity_kw: Mapped[float | None] = mapped_column(Numeric(14, 4))
    headroom_kw: Mapped[float | None] = mapped_column(Numeric(14, 4))
    utilization_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))
    sample_count: Mapped[int] = mapped_column(nullable=False, default=0)
    expected_samples: Mapped[int] = mapped_column(nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False, default=0)
    quality: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    method_version: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class PowerReportJob(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "power_report_job"
    __table_args__ = (
        CheckConstraint(f"report_type IN {REPORT_TYPES!r}", name="report_type_allowed"),
        CheckConstraint(f"format IN {REPORT_FORMATS!r}", name="format_allowed"),
        CheckConstraint(f"status IN {REPORT_STATUSES!r}", name="status_allowed"),
        CheckConstraint(
            f"failure_code IS NULL OR failure_code IN {REPORT_FAILURE_CODES!r}", name="failure_code_allowed"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("(status = 'completed') = (result IS NOT NULL)", name="result_iff_completed"),
        CheckConstraint("(status = 'failed') = (failure_code IS NOT NULL)", name="failure_code_iff_failed"),
        Index("ix_power_report_job_requester_created", "requested_by_user_id", "created_at"),
        Index("ix_power_report_job_status_lease", "status", "lease_expires_at"),
    )

    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False, default="operational")
    format: Mapped[str] = mapped_column(String(8), nullable=False, default="json")
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    row_count: Mapped[int | None] = mapped_column()
    result: Mapped[dict | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(32))
