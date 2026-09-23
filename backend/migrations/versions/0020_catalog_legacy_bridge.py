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

PR-1 follow-up: `fn_guard_catalog_model_revision_lifecycle()` (defined in migration 0017)
is `CREATE OR REPLACE`d here to extend its `draft -> published` branch: now that the bridge
columns exist, that branch also requires exactly the bridge column matching
`catalog_model.category` (looked up with a plain, non-locking `SELECT` — see 0017's module
docstring's "Revision-lock discipline" note for why no explicit lock is needed there) to be
set, and the other left NULL; a category outside `('rack', 'equipment')` cannot publish in
this phase at all (matching spec §4.1's stated API-layer scope guard, enforced here too as
defense in depth). A service can still create the legacy revision row and publish in one
transaction: it inserts the new `rack_model_revision`/`equipment_model_revision` row first,
then issues the single `catalog_model_revision` `UPDATE` that sets `lifecycle_status =
'published'` together with the matching `legacy_*_revision_id` — the trigger sees both new
values in the same `NEW` row, so this does not force a second statement or a different
transaction shape than §5.3/§4.7 already describe. `downgrade()` restores the 0017-only
function body (without the bridge check) before dropping the bridge columns, so a partial
downgrade never leaves the trigger referencing a column that no longer exists.

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

    # Restore the 0017-only body (no bridge-column reference) before the columns it reads
    # are dropped below — otherwise a subsequent draft -> published UPDATE would error at
    # runtime trying to read a column that no longer exists.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_model_revision_lifecycle() RETURNS trigger AS $$
        DECLARE
          allowed_change_keys TEXT[];
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
