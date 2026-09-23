"""Phase 10A PR-1: Manufacturer, CatalogModel, CatalogModelRevision — the reusable
asset-catalog identity/revision aggregate (docs/superpowers/specs/
2026-09-23-phase-10a-asset-catalog-designer-design.md §4.1/§4.2, aligned plan §3.1).

Purely additive — no existing table, column, or constraint is touched. Follows migration
0006's own precedent: `op.create_table` for ordinary tables/columns/FKs/simple CHECKs, raw
`op.execute` SQL for the triggers declarative mapping cannot express (matching migration
0003's `trg_managed_asset_replacement_acyclic` precedent for hand-written raw-SQL
triggers).

Three trigger functions, per spec §5.4:

- `fn_reject_write_on_non_draft_revision()` — the shared child-immutability guard. Created
  here (needed by this migration's own `CatalogModel` identity-lock use is unrelated; it is
  created here because this is where the parent table `catalog_model_revision` it locks
  first exists) but not yet attached to any table — migrations 0018/0019 attach it to each
  new child table `BEFORE INSERT OR UPDATE OR DELETE`.
- `fn_guard_catalog_model_revision_lifecycle()` — the narrower trigger on
  `catalog_model_revision` itself: `draft` rows are fully mutable; `published`/`retired`
  rows reject any column change outside a small per-transition allowlist, compared via
  `to_jsonb(NEW) - allowed_keys IS DISTINCT FROM to_jsonb(OLD) - allowed_keys` rather than
  an explicit column-by-column comparison, so a later migration (0020) can add new nullable
  columns (the legacy bridge FKs) without this function needing to change — an added column
  defaults to protected (compared, not exempted) unless explicitly added to an allowed-keys
  list, which is the safe default.
- `fn_reject_catalog_model_identity_change()` — `CatalogModel.manufacturer_id`/`category`/
  `model_name`/`model_number` become immutable once any of its revisions has been
  published; `description`/`tags`/`status` are never locked (spec §5.4: "draft-only
  editorial metadata").
- `fn_reject_manufacturer_name_change()` — `Manufacturer.name` becomes immutable once any
  of its models has a published revision (spec §5.4's same paragraph).

Revision ID: 0017_catalog_model
Revises: 0016_network_runtime_defaults
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_catalog_model"
down_revision = "0016_network_runtime_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- Manufacturer
    op.create_table(
        "manufacturer",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'deprecated')", name=op.f("ck_manufacturer_status_allowed")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manufacturer")),
        sa.UniqueConstraint("name", name=op.f("uq_manufacturer_name")),
    )

    # ---------------------------------------------------------------- CatalogModel
    op.create_table(
        "catalog_model",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("subtype", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_number", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "category IN ('rack', 'equipment', 'network_device', 'pdu', 'ups', 'power_panel', 'sensor')",
            name=op.f("ck_catalog_model_category_allowed"),
        ),
        sa.CheckConstraint("status IN ('active', 'deprecated')", name=op.f("ck_catalog_model_status_allowed")),
        sa.ForeignKeyConstraint(
            ["manufacturer_id"], ["manufacturer.id"], name=op.f("fk_catalog_model_manufacturer_id_manufacturer"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_model")),
        sa.UniqueConstraint(
            "manufacturer_id", "model_name", name="uq_catalog_model_manufacturer_id_model_name"
        ),
    )
    op.create_index("ix_catalog_model_manufacturer_id", "catalog_model", ["manufacturer_id"])

    # ---------------------------------------------------------------- CatalogModelRevision
    op.create_table(
        "catalog_model_revision",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("dimension_unit", sa.String(length=4), nullable=True),
        sa.Column("width_value", sa.Numeric(10, 3), nullable=True),
        sa.Column("height_value", sa.Numeric(10, 3), nullable=True),
        sa.Column("depth_value", sa.Numeric(10, 3), nullable=True),
        sa.Column("rack_unit_height", sa.Integer(), nullable=True),
        sa.Column("weight_unit", sa.String(length=4), nullable=True),
        sa.Column("weight_value", sa.Numeric(10, 3), nullable=True),
        sa.Column("mounting_orientation", sa.String(length=32), nullable=True),
        sa.Column("supported_placement_types", postgresql.JSONB(), nullable=True),
        sa.Column("airflow_direction", sa.String(length=16), nullable=True),
        sa.Column("rated_power_w", sa.Numeric(10, 2), nullable=True),
        sa.Column("typical_power_w", sa.Numeric(10, 2), nullable=True),
        sa.Column("max_power_w", sa.Numeric(10, 2), nullable=True),
        sa.Column("heat_dissipation_btu_hr", sa.Numeric(10, 2), nullable=True),
        sa.Column("power_redundancy_mode", sa.String(length=8), nullable=True),
        sa.Column("cloned_from_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retirement_reason", sa.String(length=1000), nullable=True),
        sa.Column("allow_installation_when_retired", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft', 'published', 'retired')",
            name=op.f("ck_catalog_model_revision_lifecycle_status_allowed"),
        ),
        sa.CheckConstraint(
            "dimension_unit IS NULL OR dimension_unit IN ('mm', 'in')",
            name=op.f("ck_catalog_model_revision_dimension_unit_allowed"),
        ),
        sa.CheckConstraint(
            "rack_unit_height IS NULL OR rack_unit_height > 0",
            name=op.f("ck_catalog_model_revision_rack_unit_height_positive"),
        ),
        sa.CheckConstraint(
            "weight_unit IS NULL OR weight_unit IN ('kg', 'lb')",
            name=op.f("ck_catalog_model_revision_weight_unit_allowed"),
        ),
        sa.CheckConstraint(
            "airflow_direction IS NULL OR airflow_direction IN "
            "('front_to_rear', 'front_to_top', 'side_to_side', 'other')",
            name=op.f("ck_catalog_model_revision_airflow_direction_allowed"),
        ),
        sa.CheckConstraint(
            "rated_power_w IS NULL OR rated_power_w >= 0", name=op.f("ck_catalog_model_revision_rated_power_w_non_negative")
        ),
        sa.CheckConstraint(
            "typical_power_w IS NULL OR typical_power_w >= 0",
            name=op.f("ck_catalog_model_revision_typical_power_w_non_negative"),
        ),
        sa.CheckConstraint(
            "max_power_w IS NULL OR max_power_w >= 0", name=op.f("ck_catalog_model_revision_max_power_w_non_negative")
        ),
        sa.CheckConstraint(
            "power_redundancy_mode IS NULL OR power_redundancy_mode IN ('single', '1+1', 'n+1')",
            name=op.f("ck_catalog_model_revision_power_redundancy_mode_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_id"], ["catalog_model.id"], name=op.f("fk_catalog_model_revision_catalog_model_id_catalog_model"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cloned_from_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_catalog_model_revision_cloned_from_revision_id_catalog_model_revision"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["app_user.id"], name=op.f("fk_catalog_model_revision_created_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"], ["app_user.id"], name=op.f("fk_catalog_model_revision_published_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_user_id"], ["app_user.id"], name=op.f("fk_catalog_model_revision_retired_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_model_revision")),
        sa.UniqueConstraint(
            "catalog_model_id", "revision_number", name="uq_catalog_model_revision_catalog_model_id"
        ),
    )
    op.create_index("ix_catalog_model_revision_catalog_model_id", "catalog_model_revision", ["catalog_model_id"])

    # ---------------------------------------------------------------- Triggers (§5.4)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_reject_write_on_non_draft_revision() RETURNS trigger AS $$
        DECLARE
          v_status TEXT;
          v_revision_id UUID;
        BEGIN
          v_revision_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.catalog_model_revision_id ELSE NEW.catalog_model_revision_id END;
          IF TG_OP = 'UPDATE' AND NEW.catalog_model_revision_id <> OLD.catalog_model_revision_id THEN
            RAISE EXCEPTION 'moving a catalog child between revisions is forbidden';
          END IF;
          SELECT lifecycle_status INTO v_status FROM catalog_model_revision WHERE id = v_revision_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'parent revision missing'; END IF;
          IF v_status <> 'draft' THEN
            RAISE EXCEPTION 'catalog_model_revision % is not a draft (status=%): child rows are immutable',
              v_revision_id, v_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_model_revision_lifecycle() RETURNS trigger AS $$
        DECLARE
          allowed_change_keys TEXT[];
        BEGIN
          IF OLD.lifecycle_status = 'draft' THEN
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
    op.execute(
        """
        CREATE TRIGGER trg_catalog_model_revision_lifecycle
        BEFORE UPDATE ON catalog_model_revision
        FOR EACH ROW EXECUTE FUNCTION fn_guard_catalog_model_revision_lifecycle();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_reject_catalog_model_identity_change() RETURNS trigger AS $$
        DECLARE
          v_published_exists BOOLEAN;
        BEGIN
          IF NEW.manufacturer_id IS DISTINCT FROM OLD.manufacturer_id
             OR NEW.category IS DISTINCT FROM OLD.category
             OR NEW.model_name IS DISTINCT FROM OLD.model_name
             OR NEW.model_number IS DISTINCT FROM OLD.model_number THEN
            SELECT EXISTS (
              SELECT 1 FROM catalog_model_revision
              WHERE catalog_model_id = OLD.id AND lifecycle_status IN ('published', 'retired')
            ) INTO v_published_exists;
            IF v_published_exists THEN
              RAISE EXCEPTION 'catalog_model % identity fields are immutable once any revision has been published', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_model_identity_lock
        BEFORE UPDATE ON catalog_model
        FOR EACH ROW EXECUTE FUNCTION fn_reject_catalog_model_identity_change();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_reject_manufacturer_name_change() RETURNS trigger AS $$
        DECLARE
          v_published_exists BOOLEAN;
        BEGIN
          IF NEW.name IS DISTINCT FROM OLD.name THEN
            SELECT EXISTS (
              SELECT 1 FROM catalog_model_revision cmr
              JOIN catalog_model cm ON cm.id = cmr.catalog_model_id
              WHERE cm.manufacturer_id = OLD.id AND cmr.lifecycle_status IN ('published', 'retired')
            ) INTO v_published_exists;
            IF v_published_exists THEN
              RAISE EXCEPTION 'manufacturer % name is immutable while any of its models has a published revision', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_manufacturer_name_lock
        BEFORE UPDATE OF name ON manufacturer
        FOR EACH ROW EXECUTE FUNCTION fn_reject_manufacturer_name_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_manufacturer_name_lock ON manufacturer")
    op.execute("DROP FUNCTION IF EXISTS fn_reject_manufacturer_name_change()")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_model_identity_lock ON catalog_model")
    op.execute("DROP FUNCTION IF EXISTS fn_reject_catalog_model_identity_change()")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_model_revision_lifecycle ON catalog_model_revision")
    op.execute("DROP FUNCTION IF EXISTS fn_guard_catalog_model_revision_lifecycle()")
    op.execute("DROP FUNCTION IF EXISTS fn_reject_write_on_non_draft_revision()")
    op.drop_table("catalog_model_revision")
    op.drop_table("catalog_model")
    op.drop_table("manufacturer")
