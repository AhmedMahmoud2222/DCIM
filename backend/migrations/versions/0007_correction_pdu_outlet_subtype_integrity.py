"""Phase 3 correction: F-M3 subtype integrity for pdu_outlet.pdu_asset_id
(PHASE3_HOSTILE_SELF_AUDIT.md F-M3; PHASE3_CORRECTION_DESIGN.md Part 11).

`pdu_outlet.pdu_asset_id` was a plain FK to `managed_asset.id` -- satisfied by ANY
ManagedAsset row (a Rack, a piece of Equipment, a UPS, ...), not specifically a PDU. The
API layer (`create_pdu_outlet` in app/api/v1/power.py) already checks this by loading
`db.get(PDU, body.pdu_asset_id)` before creating the outlet, but nothing at the database
level enforced it, so a direct SQL insert (or a future code path that forgets the API
check) could attach a PDUOutlet to a non-PDU asset.

This migration closes that gap using PostgreSQL's standard "typed foreign key" pattern,
which requires no trigger:

1. `managed_asset` gains `UNIQUE (id, asset_type)` -- harmless in addition to its
   existing PK on `id` alone (a composite unique constraint over an already-unique
   column plus one more is always satisfiable and rejects nothing `managed_asset`
   didn't already accept).
2. `pdu_outlet` gains a generated column, `pdu_asset_expected_type`, fixed to the
   literal `'pdu'` (`GENERATED ALWAYS AS ... STORED` -- PostgreSQL computes and stores
   it, the application never writes to it).
3. `pdu_outlet.pdu_asset_id`'s FK is replaced with a composite FK on
   `(pdu_asset_id, pdu_asset_expected_type)` referencing `managed_asset (id,
   asset_type)` -- which can only be satisfied when the referenced `managed_asset` row's
   own `asset_type` is literally `'pdu'`.

Per the correction design's explicit instruction not to assume existing data is clean:
`upgrade()` runs a pre-flight check counting any existing `pdu_outlet` row whose
`pdu_asset_id` does not reference a `managed_asset` row with `asset_type = 'pdu'`, and
raises rather than silently applying the constraint if any are found (there should be
none in any Phase 3 deployment today, since `create_pdu_outlet` already enforces this
at the API layer, but this migration does not assume that holds without checking).

Only `pdu_outlet.pdu_asset_id` is corrected here. `power_node.owning_asset_id` (used by
`power_circuit`/`equipment_power_input` node types) has the same class of gap but does
not admit the same fixed-literal generated-column trick, because a single
`owning_asset_id` column is shared across three different `node_type` values, each
expecting a different target subtype -- PHASE3_CORRECTION_DESIGN.md Part 11 explicitly
classifies that half of F-M3 as `REQUIRES ARCHITECTURAL DECISION`, deferred, not
implemented here.

Revision ID: 0007_correction
Revises: 0006_phase3
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_correction"
down_revision = "0006_phase3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Pre-flight: do not silently apply a constraint that existing data would violate.
    violating = bind.execute(
        sa.text(
            "SELECT po.power_node_id, po.pdu_asset_id FROM pdu_outlet po "
            "LEFT JOIN managed_asset ma ON ma.id = po.pdu_asset_id AND ma.asset_type = 'pdu' "
            "WHERE ma.id IS NULL"
        )
    ).all()
    if violating:
        raise RuntimeError(
            "Migration 0007 aborted: found pdu_outlet row(s) whose pdu_asset_id does not "
            f"reference a managed_asset with asset_type='pdu': {violating!r}. Resolve this "
            "data before re-running the migration -- it is not safe to silently constrain "
            "or delete these rows."
        )

    op.create_unique_constraint("uq_managed_asset_id_asset_type", "managed_asset", ["id", "asset_type"])

    op.add_column(
        "pdu_outlet",
        sa.Column(
            "pdu_asset_expected_type",
            sa.Text(),
            sa.Computed("'pdu'", persisted=True),
            nullable=False,
        ),
    )

    op.drop_constraint("fk_pdu_outlet_pdu_asset_id_managed_asset", "pdu_outlet", type_="foreignkey")
    op.create_foreign_key(
        "fk_pdu_outlet_pdu_asset_id_managed_asset",
        "pdu_outlet",
        "managed_asset",
        ["pdu_asset_id", "pdu_asset_expected_type"],
        ["id", "asset_type"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_pdu_outlet_pdu_asset_id_managed_asset", "pdu_outlet", type_="foreignkey")
    op.create_foreign_key(
        "fk_pdu_outlet_pdu_asset_id_managed_asset",
        "pdu_outlet",
        "managed_asset",
        ["pdu_asset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("pdu_outlet", "pdu_asset_expected_type")
    op.drop_constraint("uq_managed_asset_id_asset_type", "managed_asset", type_="unique")
