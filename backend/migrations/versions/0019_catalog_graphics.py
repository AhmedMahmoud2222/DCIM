"""Phase 10A PR-1: CatalogGraphic, CatalogGraphicMarker (spec §4.5, aligned plan §3.1).

`catalog_graphic` carries `catalog_model_revision_id` directly, so it reuses
`fn_reject_write_on_non_draft_revision()` unmodified, exactly like migration 0018's three
tables. `catalog_graphic_marker` does not — it only carries `catalog_graphic_id` — so it
gets its own `fn_validate_catalog_graphic_marker()`, which resolves the parent revision
through `catalog_graphic_id` first, then applies the same lock-and-check discipline, then
additionally rejects a marker whose `network_port_template_id`/`power_supply_template_id`
belongs to a different revision than its own graphic (spec §4.5: "The marker trigger
resolves its parent revision through `catalog_graphic_id`, locks that revision, and
rejects a target port/PSU belonging to a different revision").

Revision ID: 0019_catalog_graphics
Revises: 0018_catalog_templates
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_catalog_graphics"
down_revision = "0018_catalog_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- CatalogGraphic
    op.create_table(
        "catalog_graphic",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=16), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('front', 'rear')", name=op.f("ck_catalog_graphic_side_allowed")),
        sa.CheckConstraint(
            "mime_type IN ('image/png', 'image/jpeg')", name=op.f("ck_catalog_graphic_mime_type_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_catalog_graphic_catalog_model_revision_id_catalog_model_revision"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["app_user.id"], name=op.f("fk_catalog_graphic_uploaded_by_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_graphic")),
        sa.UniqueConstraint(
            "catalog_model_revision_id", "side", name="uq_catalog_graphic_catalog_model_revision_id"
        ),
    )
    op.create_index("ix_catalog_graphic_catalog_model_revision_id", "catalog_graphic", ["catalog_model_revision_id"])

    # ---------------------------------------------------------------- CatalogGraphicMarker
    op.create_table(
        "catalog_graphic_marker",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_graphic_id", sa.Uuid(), nullable=False),
        sa.Column("marker_type", sa.String(length=16), nullable=False),
        sa.Column("network_port_template_id", sa.Uuid(), nullable=True),
        sa.Column("power_supply_template_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("marker_x", sa.Numeric(6, 5), nullable=False),
        sa.Column("marker_y", sa.Numeric(6, 5), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "marker_type IN ('network_port', 'power_supply', 'module', 'other')",
            name=op.f("ck_catalog_graphic_marker_marker_type_allowed"),
        ),
        sa.CheckConstraint(
            "(marker_type = 'network_port' AND network_port_template_id IS NOT NULL AND power_supply_template_id IS NULL) OR "
            "(marker_type = 'power_supply' AND power_supply_template_id IS NOT NULL AND network_port_template_id IS NULL) OR "
            "(marker_type IN ('module', 'other') AND network_port_template_id IS NULL AND power_supply_template_id IS NULL)",
            name=op.f("ck_catalog_graphic_marker_marker_target_matches_type"),
        ),
        sa.CheckConstraint(
            "marker_x >= 0 AND marker_x <= 1", name=op.f("ck_catalog_graphic_marker_marker_x_normalized")
        ),
        sa.CheckConstraint(
            "marker_y >= 0 AND marker_y <= 1", name=op.f("ck_catalog_graphic_marker_marker_y_normalized")
        ),
        sa.ForeignKeyConstraint(
            ["catalog_graphic_id"], ["catalog_graphic.id"],
            name=op.f("fk_catalog_graphic_marker_catalog_graphic_id_catalog_graphic"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["network_port_template_id"], ["network_port_template.id"],
            name=op.f("fk_catalog_graphic_marker_network_port_template_id_network_port_template"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["power_supply_template_id"], ["power_supply_template.id"],
            name=op.f("fk_catalog_graphic_marker_power_supply_template_id_power_supply_template"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_graphic_marker")),
    )
    op.create_index("ix_catalog_graphic_marker_catalog_graphic_id", "catalog_graphic_marker", ["catalog_graphic_id"])

    # ---------------------------------------------------------------- Triggers
    op.execute(
        """
        CREATE TRIGGER trg_catalog_graphic_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_graphic
        FOR EACH ROW EXECUTE FUNCTION fn_reject_write_on_non_draft_revision();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_validate_catalog_graphic_marker() RETURNS trigger AS $$
        DECLARE
          v_graphic_id UUID;
          v_revision_id UUID;
          v_status TEXT;
          v_port_revision_id UUID;
          v_psu_revision_id UUID;
        BEGIN
          v_graphic_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.catalog_graphic_id ELSE NEW.catalog_graphic_id END;
          IF TG_OP = 'UPDATE' AND NEW.catalog_graphic_id <> OLD.catalog_graphic_id THEN
            RAISE EXCEPTION 'moving a marker between graphics is forbidden';
          END IF;

          SELECT catalog_model_revision_id INTO v_revision_id FROM catalog_graphic WHERE id = v_graphic_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'parent catalog_graphic missing'; END IF;

          SELECT lifecycle_status INTO v_status FROM catalog_model_revision WHERE id = v_revision_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'parent revision missing'; END IF;
          IF v_status <> 'draft' THEN
            RAISE EXCEPTION 'catalog_model_revision % is not a draft (status=%): markers are immutable', v_revision_id, v_status
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;

          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;

          IF NEW.network_port_template_id IS NOT NULL THEN
            SELECT catalog_model_revision_id INTO v_port_revision_id
              FROM network_port_template WHERE id = NEW.network_port_template_id;
            IF v_port_revision_id IS DISTINCT FROM v_revision_id THEN
              RAISE EXCEPTION 'marker network_port_template_id % belongs to a different revision than its graphic',
                NEW.network_port_template_id
                USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          IF NEW.power_supply_template_id IS NOT NULL THEN
            SELECT catalog_model_revision_id INTO v_psu_revision_id
              FROM power_supply_template WHERE id = NEW.power_supply_template_id;
            IF v_psu_revision_id IS DISTINCT FROM v_revision_id THEN
              RAISE EXCEPTION 'marker power_supply_template_id % belongs to a different revision than its graphic',
                NEW.power_supply_template_id
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
        CREATE TRIGGER trg_catalog_graphic_marker_validate
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_graphic_marker
        FOR EACH ROW EXECUTE FUNCTION fn_validate_catalog_graphic_marker();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_graphic_marker_validate ON catalog_graphic_marker")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_catalog_graphic_marker()")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_graphic_immutable ON catalog_graphic")
    op.drop_table("catalog_graphic_marker")
    op.drop_table("catalog_graphic")
