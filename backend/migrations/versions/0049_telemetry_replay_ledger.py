"""Issue #128 / G4-R: consumed-key ledger so a redelivered record is not counted twice after its raw row is gone.

Revision ID: 0049_telemetry_replay_ledger
Revises: 0048_daily_aggregate_exact_sum

Additive. Creates:

* telemetry_consumed_key(key_hash BYTEA PRIMARY KEY, consumed_day DATE): one tombstone per source record whose raw row was
  consumed into a daily aggregate. key_hash is the first 16 bytes of SHA-256 over collector_id || 0x00 || dedup_key.
* telemetry_replay_ledger_state: one row. ledger_complete_since is seeded with this migration's time; keys consumed before it
  have no tombstone and nothing is invented for them (the consumed raw rows are gone). pruning_begun_at records the first
  prune: once set, the horizon can no longer be cleared back to NULL.
* monitoring_policy.late_replay_horizon_days INTEGER NULL (>= 1). NULL, the default, means no age-based rejection and no
  pruning: the ledger then grows without bound (documented in docs/implementation/TELEMETRY_REPLAY_LEDGER.md).

Locks: both tables are new, and monitoring_policy has exactly one row, so the ACCESS EXCLUSIVE lock that ALTER TABLE takes on
it covers a metadata-only change to a one-row table. No existing large table is altered, so no commit boundary is needed.

Downgrade refuses while the ledger holds any tombstone or a finite horizon is configured: dropping them would discard the only
record that protects aggregated days from double counting.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_telemetry_replay_ledger"
down_revision = "0048_daily_aggregate_exact_sum"
branch_labels = None
depends_on = None

POLICY_CHECK = "ck_monitoring_policy_late_replay_horizon_days_positive"


def upgrade() -> None:
    op.create_table(
        "telemetry_consumed_key",
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("consumed_day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_telemetry_consumed_key")),
        sa.CheckConstraint("octet_length(key_hash) = 16", name=op.f("ck_telemetry_consumed_key_key_hash_length")),
    )
    op.create_index("ix_telemetry_consumed_key_consumed_day", "telemetry_consumed_key", ["consumed_day"])
    op.create_table(
        "telemetry_replay_ledger_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ledger_complete_since", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_pruned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_pruned_count", sa.BigInteger(), nullable=True),
        sa.Column("pruning_begun_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_replay_ledger_state")),
        sa.CheckConstraint("id = 1", name=op.f("ck_telemetry_replay_ledger_state_single_row")),
    )
    op.execute("INSERT INTO telemetry_replay_ledger_state (id, ledger_complete_since) VALUES (1, now())")
    op.add_column("monitoring_policy", sa.Column("late_replay_horizon_days", sa.Integer(), nullable=True))
    op.execute(
        f"ALTER TABLE monitoring_policy ADD CONSTRAINT {POLICY_CHECK} "
        "CHECK (late_replay_horizon_days IS NULL OR late_replay_horizon_days >= 1)"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE telemetry_consumed_key, monitoring_policy IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM telemetry_consumed_key)")):
        raise RuntimeError(
            "Cannot downgrade 0049: telemetry_consumed_key holds tombstones that stop redelivered records from being "
            "counted twice after compaction. Dropping the table would discard that protection."
        )
    if bind.scalar(sa.text("SELECT late_replay_horizon_days IS NOT NULL FROM monitoring_policy WHERE id = 1")):
        raise RuntimeError(
            "Cannot downgrade 0049: monitoring_policy.late_replay_horizon_days is set. Clear it explicitly first."
        )
    # Raw SQL: op.drop_constraint would apply the metadata naming convention to the already-conventional name.
    op.execute(f"ALTER TABLE monitoring_policy DROP CONSTRAINT {POLICY_CHECK}")
    op.drop_column("monitoring_policy", "late_replay_horizon_days")
    op.drop_table("telemetry_replay_ledger_state")
    op.drop_index("ix_telemetry_consumed_key_consumed_day", table_name="telemetry_consumed_key")
    op.drop_table("telemetry_consumed_key")
