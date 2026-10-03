"""DCIM01 PDF datasheet import, PR-B: `catalog_extraction_job` and `catalog_extraction_candidate`.

Additive only: no existing catalog, physical-measurement or legacy-bridge column is touched and
nothing in a catalog revision is written by this feature (the dimensional/unit schema decision is
still open; candidate units are extraction metadata, never converted).

Jobs: one row per (document, extractor version), unique, so a repeated request is idempotent and a
new extractor version adds history instead of replacing it. A BEFORE INSERT trigger requires the
document to belong to a catalog model, to have passed scanning and to match the recorded checksum
and model. A BEFORE UPDATE trigger makes the identity columns immutable and a completed job fully
immutable. The `running` state carries a lease and fencing token (CHECK constraint).

Candidates: immutable except for one review decision (pending -> accepted | rejected), enforced by
a BEFORE UPDATE trigger; deleted only with their job.

Revision ID: 0033_catalog_extraction
Revises: 0032_catalog_documents
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033_catalog_extraction"
down_revision = "0032_catalog_documents"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "catalog_extraction_job",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_document_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_model_id", sa.Uuid(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=48), nullable=False),
        sa.Column("unit_registry_version", sa.String(length=16), nullable=False),
        sa.Column("target_names", JSONB(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", _TS, nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("requested_at", _TS, nullable=False),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("method_summary", sa.String(length=8), nullable=True),
        sa.Column("model_resolution", sa.String(length=32), nullable=True),
        sa.Column("identified_models", JSONB(), server_default="[]", nullable=False),
        sa.Column("warnings", JSONB(), server_default="[]", nullable=False),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("pages_native", sa.Integer(), nullable=True),
        sa.Column("pages_ocr", sa.Integer(), nullable=True),
        sa.Column("pages_ocr_failed", sa.Integer(), nullable=True),
        sa.Column("candidate_count", sa.BigInteger(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name=op.f("ck_catalog_extraction_job_status_allowed")
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('complete', 'partial')", name=op.f("ck_catalog_extraction_job_outcome_allowed")
        ),
        sa.CheckConstraint(
            "method_summary IS NULL OR method_summary IN ('native', 'ocr', 'mixed', 'none')",
            name=op.f("ck_catalog_extraction_job_method_summary_allowed"),
        ),
        sa.CheckConstraint(
            "model_resolution IS NULL OR model_resolution IN ('single_model_matched', 'multi_model_matched', "
            "'target_not_found', 'ambiguous_target', 'no_model_evidence')",
            name=op.f("ck_catalog_extraction_job_model_resolution_allowed"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_catalog_extraction_job_attempt_count_nonnegative")),
        sa.CheckConstraint(
            "(status = 'completed') = (outcome IS NOT NULL AND model_resolution IS NOT NULL AND finished_at IS NOT NULL)"
            " AND (status <> 'completed' OR error_code IS NULL)",
            name=op.f("ck_catalog_extraction_job_completed_has_result"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (error_code IS NOT NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_catalog_extraction_job_failed_has_error"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_catalog_extraction_job_running_has_lease"),
        ),
        sa.CheckConstraint("char_length(document_sha256) = 64", name=op.f("ck_catalog_extraction_job_document_sha256_length")),
        sa.ForeignKeyConstraint(
            ["catalog_document_id"],
            ["catalog_document.id"],
            name=op.f("fk_catalog_extraction_job_catalog_document_id_catalog_document"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_id"],
            ["catalog_model.id"],
            name=op.f("fk_catalog_extraction_job_catalog_model_id_catalog_model"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_catalog_extraction_job_requested_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_extraction_job")),
        sa.UniqueConstraint("catalog_document_id", "extractor_version", name="uq_catalog_extraction_job_document_version"),
    )
    op.create_index("ix_catalog_extraction_job_catalog_document_id", "catalog_extraction_job", ["catalog_document_id"])
    op.create_index(
        "ix_catalog_extraction_job_pending",
        "catalog_extraction_job",
        ["status", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "catalog_extraction_candidate",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=40), nullable=False),
        sa.Column("value_numeric", sa.Numeric(18, 6), nullable=True),
        sa.Column("value_max", sa.Numeric(18, 6), nullable=True),
        sa.Column("value_text", sa.String(length=128), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("raw_value", sa.String(length=128), nullable=False),
        sa.Column("raw_unit", sa.String(length=32), server_default="", nullable=False),
        sa.Column("source_text", sa.String(length=500), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("flags", JSONB(), server_default="[]", nullable=False),
        sa.Column("model_context", sa.String(length=64), nullable=True),
        sa.Column("model_match", sa.String(length=16), nullable=False),
        sa.Column("conflict_group_key", sa.String(length=160), nullable=True),
        sa.Column("review_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("model_attribution_confirmed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("method IN ('native', 'table', 'ocr')", name=op.f("ck_catalog_extraction_candidate_method_allowed")),
        sa.CheckConstraint(
            "model_match IN ('target', 'other', 'unattributed')", name=op.f("ck_catalog_extraction_candidate_model_match_allowed")
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'accepted', 'rejected')",
            name=op.f("ck_catalog_extraction_candidate_review_status_allowed"),
        ),
        sa.CheckConstraint("page_number >= 1", name=op.f("ck_catalog_extraction_candidate_page_number_positive")),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_catalog_extraction_candidate_confidence_range")),
        sa.CheckConstraint(
            "value_numeric IS NOT NULL OR value_text IS NOT NULL", name=op.f("ck_catalog_extraction_candidate_has_value")
        ),
        sa.CheckConstraint(
            "(review_status = 'pending') = (reviewed_by_user_id IS NULL AND reviewed_at IS NULL)",
            name=op.f("ck_catalog_extraction_candidate_review_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["catalog_extraction_job.id"],
            name=op.f("fk_catalog_extraction_candidate_job_id_catalog_extraction_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_catalog_extraction_candidate_reviewed_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_extraction_candidate")),
    )
    op.create_index("ix_catalog_extraction_candidate_job_id", "catalog_extraction_candidate", ["job_id"])
    op.create_index("ix_catalog_extraction_candidate_conflict_group_key", "catalog_extraction_candidate", ["conflict_group_key"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_validate_catalog_extraction_job() RETURNS trigger AS $$
        DECLARE
          v_model UUID;
          v_sha TEXT;
          v_scan TEXT;
        BEGIN
          SELECT catalog_model_id, sha256, scan_status INTO v_model, v_sha, v_scan
            FROM catalog_document WHERE id = NEW.catalog_document_id;
          IF v_model IS NULL OR v_model IS DISTINCT FROM NEW.catalog_model_id THEN
            RAISE EXCEPTION 'extraction job must target the catalog model of document %', NEW.catalog_document_id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF v_sha IS DISTINCT FROM NEW.document_sha256 THEN
            RAISE EXCEPTION 'extraction job checksum does not match document %', NEW.catalog_document_id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF v_scan NOT IN ('clean', 'skipped') THEN
            RAISE EXCEPTION 'document % has not passed malware scanning', NEW.catalog_document_id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_extraction_job_validate BEFORE INSERT ON catalog_extraction_job "
        "FOR EACH ROW EXECUTE FUNCTION fn_validate_catalog_extraction_job();"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_extraction_job() RETURNS trigger AS $$
        BEGIN
          IF OLD.status = 'completed' AND
             (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at') THEN
            RAISE EXCEPTION 'completed extraction job % is immutable', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.catalog_document_id IS DISTINCT FROM OLD.catalog_document_id
             OR NEW.catalog_model_id IS DISTINCT FROM OLD.catalog_model_id
             OR NEW.document_sha256 IS DISTINCT FROM OLD.document_sha256
             OR NEW.extractor_version IS DISTINCT FROM OLD.extractor_version
             OR NEW.unit_registry_version IS DISTINCT FROM OLD.unit_registry_version
             OR NEW.target_names IS DISTINCT FROM OLD.target_names
             OR NEW.requested_by_user_id IS DISTINCT FROM OLD.requested_by_user_id
             OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
            RAISE EXCEPTION 'extraction job % identity is immutable', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_extraction_job_guard BEFORE UPDATE ON catalog_extraction_job "
        "FOR EACH ROW EXECUTE FUNCTION fn_guard_catalog_extraction_job();"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_extraction_candidate() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - 'updated_at' - 'review_status' - 'reviewed_by_user_id' - 'reviewed_at' - 'review_note'
              - 'model_attribution_confirmed') IS DISTINCT FROM
             (to_jsonb(OLD) - 'updated_at' - 'review_status' - 'reviewed_by_user_id' - 'reviewed_at' - 'review_note'
              - 'model_attribution_confirmed') THEN
            RAISE EXCEPTION 'extraction candidate % is immutable', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.review_status <> 'pending' AND
             (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at') THEN
            RAISE EXCEPTION 'extraction candidate % was already reviewed', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_extraction_candidate_guard BEFORE UPDATE ON catalog_extraction_candidate "
        "FOR EACH ROW EXECUTE FUNCTION fn_guard_catalog_extraction_candidate();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_extraction_candidate_guard ON catalog_extraction_candidate")
    op.execute("DROP FUNCTION IF EXISTS fn_guard_catalog_extraction_candidate()")
    op.drop_table("catalog_extraction_candidate")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_extraction_job_guard ON catalog_extraction_job")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_extraction_job_validate ON catalog_extraction_job")
    op.execute("DROP FUNCTION IF EXISTS fn_guard_catalog_extraction_job()")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_catalog_extraction_job()")
    op.drop_table("catalog_extraction_job")
