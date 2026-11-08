"""Issue #128 / G4-A: exact daily-aggregate sums.

Revision ID: 0048_daily_aggregate_exact_sum
Revises: 0047_telemetry_source_text

Additive. Three nullable columns on daily_telemetry_aggregate:

* sum_value NUMERIC(38, 8)      exact sum of every sample in the row.
* arithmetic_basis VARCHAR(24)  NULL (computed before G4-A with float arithmetic, unverified), 'decimal_exact', or
                                'legacy_seeded' (a legacy row that later received samples).
* legacy_sample_count INTEGER    legacy_seeded rows only: the sample count that existed when the row was seeded.

No default, so the column adds are metadata-only. The four CHECK constraints are added NOT VALID (no scan; every existing
row is NULL in all three columns and satisfies them) and then VALIDATEd.

Locking. migrations/env.py runs the whole upgrade in one transaction, and PostgreSQL keeps the ACCESS EXCLUSIVE lock taken
by ADD COLUMN / ADD CONSTRAINT until that transaction commits, so a VALIDATE in the same transaction would scan the table
while still blocking every reader and writer. VALIDATE therefore runs inside `autocommit_block()`, which COMMITS first (the
exclusive lock is released after the metadata-only DDL) and then runs each VALIDATE alone, where it holds only SHARE UPDATE
EXCLUSIVE: reads and writes proceed during the scan. Consequence, as for migration 0025: everything that `alembic upgrade`
had already applied in the same run is committed at that point.

Because the block commits, an interrupted run can leave the columns and constraints in place without the revision being
recorded. Every step before the commit is therefore idempotent (IF NOT EXISTS / existence-guarded), and VALIDATE is
idempotent, so re-running the migration completes it.

No row is read, rewritten or backfilled: a compacted day's raw samples are gone, so a legacy aggregate's exact sum cannot be
recovered and nothing is invented for it.

Downgrade refuses while any row carries an exact sum: dropping it would discard the only exact record of the samples.
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_daily_aggregate_exact_sum"
down_revision = "0047_telemetry_source_text"
branch_labels = None
depends_on = None

TABLE = "daily_telemetry_aggregate"
CHECKS = {
    "ck_daily_telemetry_aggregate_arithmetic_basis_allowed":
        "arithmetic_basis IS NULL OR arithmetic_basis IN ('decimal_exact', 'legacy_seeded')",
    "ck_daily_telemetry_aggregate_basis_has_sum": "(arithmetic_basis IS NULL) = (sum_value IS NULL)",
    "ck_daily_telemetry_aggregate_legacy_sample_count_scope":
        "(arithmetic_basis IS NOT DISTINCT FROM 'legacy_seeded') = (legacy_sample_count IS NOT NULL) AND "
        "(legacy_sample_count IS NULL OR legacy_sample_count BETWEEN 1 AND sample_count)",
    "ck_daily_telemetry_aggregate_exact_sum_within_extremes":
        "arithmetic_basis IS DISTINCT FROM 'decimal_exact' OR "
        "sum_value BETWEEN minimum_value * sample_count AND maximum_value * sample_count",
}


def upgrade() -> None:
    # Metadata-only DDL, idempotent so that an interrupted run (see the docstring) can be repeated.
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS sum_value NUMERIC(38, 8)")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS arithmetic_basis VARCHAR(24)")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS legacy_sample_count INTEGER")
    for name, expression in CHECKS.items():
        op.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{name}' AND conrelid = '{TABLE}'::regclass
                ) THEN
                    ALTER TABLE {TABLE} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID;
                END IF;
            END $$
        """)
    # Commit here so the ACCESS EXCLUSIVE lock above is released before any table scan.
    with op.get_context().autocommit_block():
        for name in CHECKS:
            op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE sum_value IS NOT NULL)")):
        raise RuntimeError(
            "Cannot downgrade 0048: daily aggregates carry exact sums in sum_value. "
            "Dropping the column would discard the only exact record of the aggregated samples."
        )
    for name in CHECKS:
        op.drop_constraint(op.f(name), TABLE, type_="check")
    op.drop_column(TABLE, "legacy_sample_count")
    op.drop_column(TABLE, "arithmetic_basis")
    op.drop_column(TABLE, "sum_value")
