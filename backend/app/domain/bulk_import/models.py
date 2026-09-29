"""Generic bulk-import job pipeline (shared across rack/equipment/catalog XLSX imports).

One shared `BulkImportJob`/`BulkImportRow` pair backs three thin per-domain plug-ins
(app/application/bulk_import/validators/*.py, app/application/bulk_import/commit/*.py) —
the same shared-pipeline/thin-integration split app/domain/floorplan_import/models.py
already established for the floor-plan importer, applied here to a spreadsheet-shaped
import instead of an SVG-shaped one. Unlike the floor-plan importer's "assist, never
silently mutate" candidate-review queue, a bulk-import row's `commit` step *does* write
authoritative inventory directly — the human review gate here is the mandatory preview
step (`GET /import-jobs/{id}/rows`) between `validated` and the explicit `POST .../commit`
call, not a per-row accept/reject loop."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

IMPORT_TYPES = ("rack", "equipment", "catalog")
IMPORT_MODES = ("create_only", "update_existing")
JOB_STATUSES = (
    "uploaded",
    "parsing",
    "validated",
    "failed_parse",
    "committing",
    "committed",
    "committed_with_errors",
    "cancelled",
)
ROW_STATUSES = ("pending", "valid", "invalid", "skipped", "committed", "failed")
ROW_ACTIONS = ("create", "update")


class BulkImportJob(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "bulk_import_job"
    __table_args__ = (
        CheckConstraint(f"import_type IN {IMPORT_TYPES!r}", name="import_type_allowed"),
        CheckConstraint(f"mode IN {IMPORT_MODES!r}", name="mode_allowed"),
        CheckConstraint(f"status IN {JOB_STATUSES!r}", name="status_allowed"),
    )

    import_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uploaded")

    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(255))
    report_storage_key: Mapped[str | None] = mapped_column(String(255))

    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    rejection_reason: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class BulkImportRow(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "bulk_import_row"
    __table_args__ = (
        CheckConstraint(f"status IN {ROW_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"action IS NULL OR action IN {ROW_ACTIONS!r}", name="action_allowed"),
        Index("ix_bulk_import_row_job_id_status", "job_id", "status"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bulk_import_job.id", ondelete="CASCADE"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    action: Mapped[str | None] = mapped_column(String(16))

    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    target_managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"))
    target_catalog_model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("catalog_model.id", ondelete="SET NULL"))
    target_catalog_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="SET NULL")
    )

    # SEC (Codex PR #50 review, finding #5): a snapshot of the target entity's own
    # optimistic-concurrency `version` (Rack.version / Equipment.version /
    # CatalogModelRevision.version) at validate time, for update-mode rows only — never
    # set for a create-mode row, where there is no existing entity to go stale. Preview
    # and commit can be arbitrarily far apart in time (and other commits can land in
    # between), so without this, commit re-fetching "the current version" at commit time
    # can never distinguish "still exactly what was previewed" from "changed since
    # preview" — it would always trivially match itself. NULL means either a create-mode
    # row, or an update-mode row whose target wasn't actually resolved at validate time
    # (so there is nothing to compare staleness against — commit falls back to an
    # unconditional live-version claim for that row, same as before this column existed).
    expected_version: Mapped[int | None] = mapped_column(Integer)
