"""SEC-05: time-leading indexes for bounded global collector retention scans.

The existing nonce index starts with collector_id, and the heartbeat composite index
starts with collector_id. Neither supports an all-collector timestamp range scan
ordered by timestamp. Create these B-tree indexes concurrently to avoid holding
ordinary write-blocking locks while deploying to a populated PostgreSQL database.

Revision ID: 0025_collector_retention
Revises: 0024_telemetry_impact_map
"""

from alembic import op

revision = "0025_collector_retention"
down_revision = "0024_telemetry_impact_map"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_collector_request_nonce_seen_at", "collector_request_nonce",
            ["seen_at"], postgresql_concurrently=True,
        )
        op.create_index(
            "ix_collector_heartbeat_ts", "collector_heartbeat",
            ["ts"], postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_collector_heartbeat_ts", table_name="collector_heartbeat",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_collector_request_nonce_seen_at", table_name="collector_request_nonce",
            postgresql_concurrently=True,
        )
