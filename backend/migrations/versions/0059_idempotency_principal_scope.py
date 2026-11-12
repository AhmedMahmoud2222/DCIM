"""Scope idempotency records to the authenticated principal (issue #337).

Revision ID: 0059_idempotency_principal
Revises: 0058_alarm_subject_key

Previously the ledger was unique on ``(key, endpoint)``, so two principals sharing a key and
endpoint interacted through one record (cross-principal replay / denial of service). The unique
index becomes ``(principal, key, endpoint)``.

Legacy-record policy (unexpired rows already in the table):
* ``collector_ingest`` rows are keyed ``<collector_id>:<dedup_key>`` by the service, so the owning
  collector is recoverable from the key and is backfilled as ``collector:<collector_id>``. Retried
  batches from that collector keep deduplicating across the upgrade.
* Every other row has no recoverable owner. It is stamped ``legacy:unscoped``, a value no
  authenticated caller can produce, so it can never be replayed to, or block, any principal. Such a
  row simply ages out at its 24 h TTL; at worst a client retrying across the upgrade repeats one
  write, which is the safe direction (no data from another principal is ever returned).

This migration does not touch, reorder or depend on the 0057 FK-index production hold; it only
revises 0058 and is subject to the same deployment gate as any pending migration.
"""
import sqlalchemy as sa
from alembic import op

revision = "0059_idempotency_principal"
down_revision = "0058_alarm_subject_key"
branch_labels = None
depends_on = None

_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def upgrade() -> None:
    op.add_column(
        "idempotency_key",
        sa.Column("principal", sa.String(length=100), nullable=False, server_default="legacy:unscoped"),
    )
    op.execute(
        f"""
        UPDATE idempotency_key
        SET principal = 'collector:' || lower(split_part(key, ':', 1))
        WHERE endpoint = 'collector_ingest' AND split_part(key, ':', 1) ~ '{_UUID_RE}'
        """
    )
    # New callers must always supply a principal; no silent default.
    op.alter_column("idempotency_key", "principal", server_default=None)
    # The new index is strictly finer than the old one, so it cannot conflict with existing rows.
    op.create_index(
        "ix_idempotency_key_principal_key_endpoint", "idempotency_key", ["principal", "key", "endpoint"], unique=True
    )
    op.drop_index("ix_idempotency_key_key_endpoint", table_name="idempotency_key")


def downgrade() -> None:
    # Re-narrowing the unique key can collide where two principals used one (key, endpoint).
    # Keep the most recently updated row of each such group (preferring completed) and drop the rest;
    # records are 24 h TTL replay caches, so this loses no durable data.
    op.execute(
        """
        DELETE FROM idempotency_key d
        USING (
            SELECT id, row_number() OVER (
                PARTITION BY key, endpoint
                ORDER BY (status = 'completed') DESC, updated_at DESC, id DESC
            ) AS rn
            FROM idempotency_key
        ) ranked
        WHERE d.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_index("ix_idempotency_key_key_endpoint", "idempotency_key", ["key", "endpoint"], unique=True)
    op.drop_index("ix_idempotency_key_principal_key_endpoint", table_name="idempotency_key")
    op.drop_column("idempotency_key", "principal")
