"""Extend catalog model revision lifecycle trigger to support 'pdu' category legacy bridge.

Revision ID: 0050_pdu_catalog_bridge
Revises: 0049_telemetry_replay_ledger
Create Date: 2026-03-30
"""

from alembic import op

revision = "0050_pdu_catalog_bridge"
down_revision = "0049_telemetry_replay_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
            ELSIF v_category IN ('equipment', 'pdu') THEN
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
    )


def downgrade() -> None:
    op.execute(
        """
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
    )
