"""Phase 2 targeted correction: RT-1 (HIGH) — the SpatialLayer lookup-then-create race in
accept_import_candidate. See PHASE2_INDEPENDENT_RED_TEAM_REPORT.md and
PHASE2_CORRECTION_REPORT.md.

The business rule this enforces: at most one canonical `layer_type='imported'`
SpatialLayer per FloorPlan (confirmed via source inspection — accept_import_candidate is
the *only* place SpatialLayer rows are ever created anywhere in this codebase, and it has
always intended exactly one "Imported" layer per floor plan to hold every accepted
candidate's SpatialObject). Other layer_type values ("background", "room_outline",
"racks", "equipment", "annotations") have no creation path in this codebase at all, so
this migration deliberately does not constrain them — scoping the uniqueness to
`layer_type = 'imported'` specifically encodes the actual rule, not a broader one this
migration has no basis to assert.

Data safety: a database that already hit the race (this repository's own red-team
validation reproduced it live) may already contain duplicate 'imported' SpatialLayer rows
for the same floor_plan_id. Before adding the constraint, this migration deterministically
merges any such duplicates — the oldest layer (by created_at, then id, for a stable
tie-break) is kept as canonical; every SpatialObject pointing at a newer duplicate is
re-pointed to the canonical layer; the duplicate layer rows are then deleted. No
SpatialObject is ever deleted or orphaned by this migration — only the redundant empty
container rows are removed, after their contents are preserved by re-pointing.

Revision ID: 0005_correction
Revises: 0004_phase2
Create Date: 2026-09-17
"""

from alembic import op

revision = "0005_correction"
down_revision = "0004_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- RT-1: deduplicate any pre-existing duplicate 'imported' SpatialLayer rows,
    # re-pointing their SpatialObjects to a single canonical layer per floor plan, before
    # the constraint below would otherwise refuse to apply to a table already violating it.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, floor_plan_id,
                   row_number() OVER (PARTITION BY floor_plan_id ORDER BY created_at, id) AS rn
            FROM spatial_layer
            WHERE layer_type = 'imported'
        ),
        canonical AS (
            SELECT floor_plan_id, id AS canonical_id FROM ranked WHERE rn = 1
        ),
        duplicates AS (
            SELECT r.id AS duplicate_id, c.canonical_id
            FROM ranked r
            JOIN canonical c ON c.floor_plan_id = r.floor_plan_id
            WHERE r.rn > 1
        )
        UPDATE spatial_object so
        SET spatial_layer_id = d.canonical_id
        FROM duplicates d
        WHERE so.spatial_layer_id = d.duplicate_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, floor_plan_id,
                   row_number() OVER (PARTITION BY floor_plan_id ORDER BY created_at, id) AS rn
            FROM spatial_layer
            WHERE layer_type = 'imported'
        )
        DELETE FROM spatial_layer WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    # --- The DB-enforced invariant itself: at most one 'imported' layer per floor plan.
    # A partial unique index (not a plain UniqueConstraint on floor_plan_id alone, which
    # would wrongly forbid other layer_type rows for the same floor plan if any creation
    # path for them is ever added later).
    op.execute(
        "CREATE UNIQUE INDEX uq_spatial_layer_one_imported_per_floor_plan "
        "ON spatial_layer (floor_plan_id) WHERE layer_type = 'imported'"
    )


def downgrade() -> None:
    # The deduplication above is not reversed on downgrade — merging duplicate layers is
    # a permanent data-quality fix, not a reversible schema change, and re-creating
    # arbitrary duplicate rows on downgrade would serve no purpose (same precedent as
    # 0004's own downgrade leaving seeded RBAC rows in place rather than surgically
    # reversing a data-level fix).
    op.execute("DROP INDEX IF EXISTS uq_spatial_layer_one_imported_per_floor_plan")
