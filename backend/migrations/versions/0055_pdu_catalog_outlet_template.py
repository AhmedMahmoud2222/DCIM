"""PDU catalog outlet template, electrical attributes and the PDU branch of the
lifecycle guard (Issue #150, M2+M3).

Revision ID: 0055_pdu_catalog_outlet_template
Revises: 0054_role_assignment_scope_types
Create Date: 2026-10-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_pdu_catalog_outlet_template"
down_revision = "0051_aggregate_lineage"
branch_labels = None
depends_on = None

# `fn_guard_catalog_model_revision_lifecycle()` (created in 0017, extended in 0020 for the
# rack/equipment legacy bridge) gains a `pdu` branch in Issue #150 M3: a PDU revision
# publishes designer-side with BOTH legacy bridge columns NULL - PDU has no legacy bridge
# by design (§4.1 / Issue #150 architecture decision) - while still refusing a PDU publish
# that somehow carries one. Every other category keeps the exact 0020 behavior/message
# (tests/integration/test_catalog_designer_schema.py asserts it verbatim).
_LIFECYCLE_GUARD_FN = """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_model_revision_lifecycle() RETURNS trigger AS $$
        DECLARE
          allowed_change_keys TEXT[];
          v_category TEXT;
        BEGIN
          IF OLD.lifecycle_status = 'draft' AND NEW.lifecycle_status = 'draft' THEN
            RETURN NEW;
          END IF;

          IF OLD.lifecycle_status = 'draft' AND NEW.lifecycle_status = 'published' THEN
            IF NEW.published_at IS NULL OR NEW.published_by_user_id IS NULL THEN
              RAISE EXCEPTION 'catalog_model_revision % cannot publish without published_at and published_by_user_id', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.retired_at IS NOT NULL OR NEW.retired_by_user_id IS NOT NULL OR NEW.retirement_reason IS NOT NULL THEN
              RAISE EXCEPTION 'catalog_model_revision % cannot set retirement fields while publishing', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT category INTO v_category FROM catalog_model WHERE id = NEW.catalog_model_id;
            IF v_category = 'rack' THEN
              IF NEW.legacy_rack_model_revision_id IS NULL OR NEW.legacy_equipment_model_revision_id IS NOT NULL THEN
                RAISE EXCEPTION
                  'catalog_model_revision % must set exactly legacy_rack_model_revision_id to publish a rack model',
                  OLD.id
                  USING ERRCODE = 'integrity_constraint_violation';
              END IF;
            ELSIF v_category = 'equipment' THEN
              IF NEW.legacy_equipment_model_revision_id IS NULL OR NEW.legacy_rack_model_revision_id IS NOT NULL THEN
                RAISE EXCEPTION
                  'catalog_model_revision % must set exactly legacy_equipment_model_revision_id to publish an equipment model',
                  OLD.id
                  USING ERRCODE = 'integrity_constraint_violation';
              END IF;
            ELSIF v_category = 'pdu' THEN
              IF NEW.legacy_rack_model_revision_id IS NOT NULL OR NEW.legacy_equipment_model_revision_id IS NOT NULL THEN
                RAISE EXCEPTION
                  'catalog_model_revision % must not set a legacy bridge column to publish a pdu model',
                  OLD.id
                  USING ERRCODE = 'integrity_constraint_violation';
              END IF;
            ELSE
              RAISE EXCEPTION
                'catalog_model_revision % cannot publish: category % has no legacy bridge in this phase',
                OLD.id, v_category
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            RETURN NEW;
          END IF;

          IF OLD.lifecycle_status = 'published' AND NEW.lifecycle_status = 'published' THEN
            allowed_change_keys := ARRAY['updated_at'];
          ELSIF OLD.lifecycle_status = 'published' AND NEW.lifecycle_status = 'retired' THEN
            allowed_change_keys := ARRAY['lifecycle_status', 'retired_at', 'retired_by_user_id',
                                          'retirement_reason', 'allow_installation_when_retired', 'updated_at'];
          ELSIF OLD.lifecycle_status = 'retired' AND NEW.lifecycle_status = 'retired' THEN
            allowed_change_keys := ARRAY['allow_installation_when_retired', 'updated_at'];
          ELSE
            RAISE EXCEPTION 'catalog_model_revision % cannot transition from % to %',
              OLD.id, OLD.lifecycle_status, NEW.lifecycle_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;

          IF (to_jsonb(NEW) - allowed_change_keys) IS DISTINCT FROM (to_jsonb(OLD) - allowed_change_keys) THEN
            RAISE EXCEPTION 'catalog_model_revision % (status=%) rejects this column change', OLD.id, OLD.lifecycle_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """

# Byte-for-byte the 0020 definition (pre-pdu), so `downgrade` restores the exact prior
# behavior instead of leaving the M3 function behind after the PDU columns are dropped.
_LIFECYCLE_GUARD_FN_PRE_PDU = """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_model_revision_lifecycle() RETURNS trigger AS $$
        DECLARE
          allowed_change_keys TEXT[];
          v_category TEXT;
        BEGIN
          IF OLD.lifecycle_status = 'draft' AND NEW.lifecycle_status = 'draft' THEN
            RETURN NEW;
          END IF;

          IF OLD.lifecycle_status = 'draft' AND NEW.lifecycle_status = 'published' THEN
            IF NEW.published_at IS NULL OR NEW.published_by_user_id IS NULL THEN
              RAISE EXCEPTION 'catalog_model_revision % cannot publish without published_at and published_by_user_id', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.retired_at IS NOT NULL OR NEW.retired_by_user_id IS NOT NULL OR NEW.retirement_reason IS NOT NULL THEN
              RAISE EXCEPTION 'catalog_model_revision % cannot set retirement fields while publishing', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT category INTO v_category FROM catalog_model WHERE id = NEW.catalog_model_id;
            IF v_category = 'rack' THEN
              IF NEW.legacy_rack_model_revision_id IS NULL OR NEW.legacy_equipment_model_revision_id IS NOT NULL THEN
                RAISE EXCEPTION
                  'catalog_model_revision % must set exactly legacy_rack_model_revision_id to publish a rack model',
                  OLD.id
                  USING ERRCODE = 'integrity_constraint_violation';
              END IF;
            ELSIF v_category = 'equipment' THEN
              IF NEW.legacy_equipment_model_revision_id IS NULL OR NEW.legacy_rack_model_revision_id IS NOT NULL THEN
                RAISE EXCEPTION
                  'catalog_model_revision % must set exactly legacy_equipment_model_revision_id to publish an equipment model',
                  OLD.id
                  USING ERRCODE = 'integrity_constraint_violation';
              END IF;
            ELSE
              RAISE EXCEPTION
                'catalog_model_revision % cannot publish: category % has no legacy bridge in this phase',
                OLD.id, v_category
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            RETURN NEW;
          END IF;

          IF OLD.lifecycle_status = 'published' AND NEW.lifecycle_status = 'published' THEN
            allowed_change_keys := ARRAY['updated_at'];
          ELSIF OLD.lifecycle_status = 'published' AND NEW.lifecycle_status = 'retired' THEN
            allowed_change_keys := ARRAY['lifecycle_status', 'retired_at', 'retired_by_user_id',
                                          'retirement_reason', 'allow_installation_when_retired', 'updated_at'];
          ELSIF OLD.lifecycle_status = 'retired' AND NEW.lifecycle_status = 'retired' THEN
            allowed_change_keys := ARRAY['allow_installation_when_retired', 'updated_at'];
          ELSE
            RAISE EXCEPTION 'catalog_model_revision % cannot transition from % to %',
              OLD.id, OLD.lifecycle_status, NEW.lifecycle_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;

          IF (to_jsonb(NEW) - allowed_change_keys) IS DISTINCT FROM (to_jsonb(OLD) - allowed_change_keys) THEN
            RAISE EXCEPTION 'catalog_model_revision % (status=%) rejects this column change', OLD.id, OLD.lifecycle_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """


def upgrade() -> None:
    op.add_column("catalog_model_revision", sa.Column("pdu_input_voltage_v", sa.Numeric(6, 1), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_input_current_a", sa.Numeric(6, 2), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_phase", sa.String(8), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_rated_capacity_kw", sa.Numeric(10, 3), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_plug_type", sa.String(32), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_monitoring_protocol", sa.String(16), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_monitoring_endpoint", sa.String(255), nullable=True))

    op.create_table(
        "pdu_outlet_template",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            # UUIDPkMixin declares server_default=gen_random_uuid() - without it here the
            # ORM INSERTs a NULL id and every outlet-template insert fails (found by the
            # Issue #150 M2 PostgreSQL API tests).
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "catalog_model_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("catalog_model_revision.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stable_key", sa.String(64), nullable=False),
        sa.Column("outlet_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("outlet_type", sa.String(16), nullable=False, server_default="other"),
        sa.Column("outlet_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(8), nullable=False, server_default="single"),
        sa.Column("rated_voltage_v", sa.Numeric(6, 1), nullable=True),
        sa.Column("rated_current_a", sa.Numeric(6, 2), nullable=True),
        sa.Column("position", sa.String(32), nullable=True),
        sa.Column("side", sa.String(8), nullable=False, server_default="front"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("catalog_model_revision_id", "stable_key", name="uq_pdu_outlet_template_cmr_sk"),
        sa.UniqueConstraint("catalog_model_revision_id", "outlet_number", name="uq_pdu_outlet_template_cmr_on"),
        # Names mirror PDUOutletTemplate.__table_args__ / index=True exactly (ORM-schema
        # alignment): the model declares both, so the migrated database must too.
        # op.f() pins the literal name (repo convention - see migration 0017) so the
        # naming convention can't wrap it a second time.
        sa.CheckConstraint("outlet_count > 0", name=op.f("ck_pdu_outlet_template_outlet_count_positive")),
    )
    op.create_index(
        "ix_pdu_outlet_template_catalog_model_revision_id",
        "pdu_outlet_template",
        ["catalog_model_revision_id"],
    )

    # Extend the lifecycle guard so `category='pdu'` can publish designer-side (M3).
    op.execute(_LIFECYCLE_GUARD_FN)

    # Same immutability guard every other catalog child template got in 0018: published
    # PDU outlet rows reject direct INSERT/UPDATE/DELETE, so the draft-only contract
    # holds even against tools that bypass the API (Issue #150 M3). The shared function
    # keys off `catalog_model_revision_id`, which pdu_outlet_template already carries.
    op.execute(
        """
        CREATE TRIGGER trg_pdu_outlet_template_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON pdu_outlet_template
        FOR EACH ROW EXECUTE FUNCTION fn_reject_write_on_non_draft_revision();
        """
    )


def downgrade() -> None:
    # Restore the pre-PDU (0020) guard before the PDU columns disappear.
    op.execute(_LIFECYCLE_GUARD_FN_PRE_PDU)
    op.execute("DROP TRIGGER IF EXISTS trg_pdu_outlet_template_immutable ON pdu_outlet_template")
    op.drop_table("pdu_outlet_template")
    for c in [
        "pdu_monitoring_endpoint", "pdu_monitoring_protocol", "pdu_plug_type", "pdu_rated_capacity_kw",
        "pdu_phase", "pdu_input_current_a", "pdu_input_voltage_v",
    ]:
        op.drop_column("catalog_model_revision", c)
