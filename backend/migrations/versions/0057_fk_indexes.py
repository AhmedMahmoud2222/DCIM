"""Index the referencing column of hot-path foreign keys that had none.

A foreign key does not create an index on the referencing side. Without one, every DELETE/UPDATE of a referenced row
(`ON DELETE RESTRICT` / `SET NULL` / `CASCADE`) scans the entire child table, and joins from the referenced row to its
children cannot use an index. The biggest exposure is `telemetry_reading` (`mapping_id` is `ON DELETE RESTRICT`), but the
cable / port-connection / notification / placement / power-topology FKs below had the same gap.

Deliberately NOT indexed: the `*_by_user_id` audit columns. They are only touched when a user row is deleted, so an
index would add write cost for no read benefit.

Locking. migrations/env.py runs the upgrade in one transaction, and CREATE INDEX CONCURRENTLY cannot run inside one, so the
builds run in `autocommit_block()` (as in migrations 0025 / 0048): reads and writes proceed during each build. Consequence,
as for those migrations: everything `alembic upgrade` already applied in the same run is committed at that point. An
interrupted run can leave an INVALID index behind; every statement is therefore `IF NOT EXISTS`, and upgrade first drops
any INVALID index of the same name so a re-run rebuilds it.

Revision ID: 0057_fk_indexes
Revises: 0056_pdu_vertical_placement
Create Date: 2026-10-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0057_fk_indexes"
down_revision = "0056_pdu_vertical_placement"
branch_labels = None
depends_on = None

# (table, column) — index name is `ix_<table>_<column>`, matching the declarations at the bottom of each model module.
FK_INDEXES = [
    ("telemetry_reading", "mapping_id"),
    ("telemetry_reading", "mapping_revision_id"),
    ("daily_telemetry_aggregate_lineage", "mapping_revision_id"),
    ("alarm", "telemetry_reading_id"),
    ("notification_delivery", "policy_id"),
    ("notification_delivery", "channel_id"),
    ("cable", "port_connection_id"),
    ("cable", "source_neighbor_id"),
    ("port_connection", "target_port_id"),
    ("port_connection", "target_power_node_id"),
    ("rack", "model_revision_id"),
    ("equipment", "model_revision_id"),
    ("power_node", "owning_asset_id"),
    ("ups", "room_id"),
    ("power_panel", "room_id"),
    ("generator", "site_id"),
    ("rack_placement", "room_id"),
    ("bulk_import_row", "target_managed_asset_id"),
    ("bulk_import_row", "target_catalog_model_id"),
    ("bulk_import_row", "target_catalog_revision_id"),
    ("role_permission", "permission_id"),
    ("user_group_permission", "permission_id"),
]


def _name(table: str, column: str) -> str:
    return f"ix_{table}_{column}"


def upgrade() -> None:
    bind = op.get_bind()
    # Commit first: CREATE INDEX CONCURRENTLY cannot run inside the migration transaction.
    with op.get_context().autocommit_block():
        for table, column in FK_INDEXES:
            name = _name(table, column)
            invalid = bind.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :n AND c.relnamespace = current_schema()::regnamespace AND NOT i.indisvalid)"
                ),
                {"n": name},
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in reversed(FK_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_name(table, column)}")
