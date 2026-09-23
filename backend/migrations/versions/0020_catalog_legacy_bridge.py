"""Phase 10A PR-1: the legacy bridge (spec §4.7, aligned plan §3.1) — the mechanism that
keeps this design non-breaking toward the existing `Rack`/`Equipment` FK relationships.

Purely additive, nullable columns on the two pre-existing legacy revision tables
(`rack_model_revision`, `equipment_model_revision`) — zero change to any existing row,
existing query, existing `NOT NULL RESTRICT` FK on `Rack`/`Equipment`, or existing test.
Those two tables are never dropped, renamed, or altered in shape beyond this one additive
column each.

Ordering note (aligned plan §1.3 item 2, correcting an earlier version of this plan's
stated rationale): `catalog_model_revision.legacy_rack_model_revision_id`/
`legacy_equipment_model_revision_id` could technically have been added in migration 0017
alongside the table itself, since `rack_model_revision`/`equipment_model_revision` already
existed at head `0016`. They are added here anyway, together with the reverse
`bridged_from_catalog_revision_id` columns (which genuinely cannot exist before
`catalog_model_revision` does), so both halves of the bidirectional bridge land as one
reviewable, easily-reverted unit.

A new trigger, `fn_reject_bridged_legacy_revision_update()`, rejects any `UPDATE` on a
`rack_model_revision`/`equipment_model_revision` row once its
`bridged_from_catalog_revision_id` is set — closing at the DB level what was previously
only an application convention (no PATCH endpoint exists, but nothing in the schema
prevented a direct write). Pre-existing, unbridged rows (`bridged_from_catalog_revision_id
IS NULL` — every row created before Phase 10A, and any legacy row created going forward
outside the bridge) are untouched by this trigger and keep their present behavior exactly.
`DELETE` needs no new guard: the existing `ondelete="RESTRICT"` FK from
`catalog_model_revision.legacy_*_revision_id` already prevents deleting a referenced row.

Revision ID: 0020_catalog_legacy_bridge
Revises: 0019_catalog_graphics
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_catalog_legacy_bridge"
down_revision = "0019_catalog_graphics"
branch_labels = None
depends_on = None

_LEGACY_TABLES = ("rack_model_revision", "equipment_model_revision")


def upgrade() -> None:
    for table_name in _LEGACY_TABLES:
        op.add_column(table_name, sa.Column("bridged_from_catalog_revision_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table_name}_bridged_from_catalog_revision_id_catalog_model_revision"),
            table_name,
            "catalog_model_revision",
            ["bridged_from_catalog_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_unique_constraint(
            op.f(f"uq_{table_name}_bridged_from_catalog_revision_id"), table_name, ["bridged_from_catalog_revision_id"]
        )

    op.add_column("catalog_model_revision", sa.Column("legacy_rack_model_revision_id", sa.Uuid(), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("legacy_equipment_model_revision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_catalog_model_revision_legacy_rack_model_revision_id_rack_model_revision"),
        "catalog_model_revision",
        "rack_model_revision",
        ["legacy_rack_model_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_catalog_model_revision_legacy_equipment_model_revision_id_equipment_model_revision"),
        "catalog_model_revision",
        "equipment_model_revision",
        ["legacy_equipment_model_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_catalog_model_revision_legacy_rack_model_revision_id"),
        "catalog_model_revision",
        ["legacy_rack_model_revision_id"],
    )
    op.create_unique_constraint(
        op.f("uq_catalog_model_revision_legacy_equipment_model_revision_id"),
        "catalog_model_revision",
        ["legacy_equipment_model_revision_id"],
    )
    op.create_check_constraint(
        op.f("ck_catalog_model_revision_legacy_bridge_exclusive"),
        "catalog_model_revision",
        "NOT (legacy_rack_model_revision_id IS NOT NULL AND legacy_equipment_model_revision_id IS NOT NULL)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_reject_bridged_legacy_revision_update() RETURNS trigger AS $$
        BEGIN
          IF OLD.bridged_from_catalog_revision_id IS NOT NULL THEN
            RAISE EXCEPTION '% % is bridged from a published catalog_model_revision and is immutable', TG_TABLE_NAME, OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in _LEGACY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_bridged_immutable
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION fn_reject_bridged_legacy_revision_update();
            """
        )


def downgrade() -> None:
    for table_name in _LEGACY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_bridged_immutable ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS fn_reject_bridged_legacy_revision_update()")

    op.drop_constraint(
        op.f("ck_catalog_model_revision_legacy_bridge_exclusive"), "catalog_model_revision", type_="check"
    )
    op.drop_constraint(
        op.f("uq_catalog_model_revision_legacy_equipment_model_revision_id"),
        "catalog_model_revision",
        type_="unique",
    )
    op.drop_constraint(
        op.f("uq_catalog_model_revision_legacy_rack_model_revision_id"), "catalog_model_revision", type_="unique"
    )
    op.drop_constraint(
        op.f("fk_catalog_model_revision_legacy_equipment_model_revision_id_equipment_model_revision"),
        "catalog_model_revision",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_catalog_model_revision_legacy_rack_model_revision_id_rack_model_revision"),
        "catalog_model_revision",
        type_="foreignkey",
    )
    op.drop_column("catalog_model_revision", "legacy_equipment_model_revision_id")
    op.drop_column("catalog_model_revision", "legacy_rack_model_revision_id")

    for table_name in _LEGACY_TABLES:
        op.drop_constraint(
            op.f(f"uq_{table_name}_bridged_from_catalog_revision_id"), table_name, type_="unique"
        )
        op.drop_constraint(
            op.f(f"fk_{table_name}_bridged_from_catalog_revision_id_catalog_model_revision"),
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "bridged_from_catalog_revision_id")
