"""Append-only extraction application provenance.

Revision ID: 0036_catalog_extraction_apply
Revises: 0035_units_metric_registry
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_catalog_extraction_apply"
down_revision = "0035_units_metric_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_extraction_application",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("revision_id", sa.UUID(), sa.ForeignKey("catalog_model_revision.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("catalog_document.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("catalog_extraction_job.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_version", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("extractor_version", sa.String(48), nullable=False),
        sa.Column("extraction_unit_registry_version", sa.String(16), nullable=False),
        sa.Column("before_values", postgresql.JSONB(), nullable=False),
        sa.Column("after_values", postgresql.JSONB(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("revision_id", "revision_version", name="uq_catalog_extraction_application_revision_version"),
        sa.CheckConstraint("revision_version > 1", name="revision_version_positive"),
    )
    op.create_index("ix_catalog_extraction_application_revision_id", "catalog_extraction_application", ["revision_id"])
    op.execute("""
        CREATE FUNCTION fn_catalog_extraction_application_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Extraction application provenance is append-only' USING ERRCODE = '23514';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_catalog_extraction_application_immutable
        BEFORE UPDATE OR DELETE ON catalog_extraction_application FOR EACH ROW
        EXECUTE FUNCTION fn_catalog_extraction_application_immutable();
    """)


def downgrade() -> None:
    # Do not erase historical evidence just to make a schema downgrade succeed.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM catalog_extraction_application) THEN
                RAISE EXCEPTION 'Cannot downgrade populated extraction application provenance';
            END IF;
        END $$;
    """)
    op.drop_table("catalog_extraction_application")
    op.execute("DROP FUNCTION fn_catalog_extraction_application_immutable()")
