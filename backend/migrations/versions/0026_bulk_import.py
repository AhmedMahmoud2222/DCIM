"""Generic bulk-import job pipeline: `bulk_import_job` / `bulk_import_row` (see
app/domain/bulk_import/models.py's module docstring for the shared-pipeline/thin-plug-in
design this schema backs — rack/equipment/catalog XLSX imports through one shared job/row
lifecycle rather than three separate schemas).

Revision ID: 0026_bulk_import
Revises: 0025_collector_retention
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_bulk_import"
down_revision = "0025_collector_retention"
branch_labels = None
depends_on = None

_IMPORT_TYPES = ("rack", "equipment", "catalog")
_IMPORT_MODES = ("create_only", "update_existing")
_JOB_STATUSES = (
    "uploaded",
    "parsing",
    "validated",
    "failed_parse",
    "committing",
    "committed",
    "committed_with_errors",
    "cancelled",
)
_ROW_STATUSES = ("pending", "valid", "invalid", "skipped", "committed", "failed")
_ROW_ACTIONS = ("create", "update")


def upgrade() -> None:
    op.create_table(
        "bulk_import_job",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("import_type", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="uploaded"),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=True),
        sa.Column("report_storage_key", sa.String(length=255), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warning_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"import_type IN {_IMPORT_TYPES!r}", name=op.f("ck_bulk_import_job_import_type_allowed")),
        sa.CheckConstraint(f"mode IN {_IMPORT_MODES!r}", name=op.f("ck_bulk_import_job_mode_allowed")),
        sa.CheckConstraint(f"status IN {_JOB_STATUSES!r}", name=op.f("ck_bulk_import_job_status_allowed")),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["app_user.id"],
            name=op.f("fk_bulk_import_job_uploaded_by_user_id_app_user"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bulk_import_job")),
    )

    op.create_table(
        "bulk_import_row",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("sheet_name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("target_managed_asset_id", sa.Uuid(), nullable=True),
        sa.Column("target_catalog_model_id", sa.Uuid(), nullable=True),
        sa.Column("target_catalog_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"status IN {_ROW_STATUSES!r}", name=op.f("ck_bulk_import_row_status_allowed")),
        sa.CheckConstraint(f"action IS NULL OR action IN {_ROW_ACTIONS!r}", name=op.f("ck_bulk_import_row_action_allowed")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["bulk_import_job.id"], name=op.f("fk_bulk_import_row_job_id_bulk_import_job"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_managed_asset_id"], ["managed_asset.id"],
            name=op.f("fk_bulk_import_row_target_managed_asset_id_managed_asset"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_catalog_model_id"], ["catalog_model.id"],
            name=op.f("fk_bulk_import_row_target_catalog_model_id_catalog_model"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_catalog_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_bulk_import_row_target_catalog_revision_id_catalog_model_revision"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bulk_import_row")),
    )
    op.create_index("ix_bulk_import_row_job_id_status", "bulk_import_row", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_bulk_import_row_job_id_status", table_name="bulk_import_row")
    op.drop_table("bulk_import_row")
    op.drop_table("bulk_import_job")
