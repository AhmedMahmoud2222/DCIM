"""Align network-table runtime defaults with the authoritative ORM mapping.

Revision ID: 0016_network_runtime_defaults
Revises: 0015_network_topology
"""

import sqlalchemy as sa
from alembic import op


revision = "0016_network_runtime_defaults"
down_revision = "0015_network_topology"
branch_labels = None
depends_on = None


_NETWORK_TABLES = ("network_device", "network_interface", "network_connection")


def upgrade() -> None:
    """0015 created non-null identity/timestamp columns without their model defaults.

    This is deliberately forward-only: production databases may already have applied
    0015, and the defaults are required for normal ORM inserts to be executable.
    """
    for table_name in _NETWORK_TABLES:
        op.alter_column(table_name, "created_at", server_default=sa.text("now()"))
        op.alter_column(table_name, "updated_at", server_default=sa.text("now()"))
    for table_name in ("network_interface", "network_connection"):
        op.alter_column(table_name, "id", server_default=sa.text("gen_random_uuid()"))


def downgrade() -> None:
    for table_name in ("network_interface", "network_connection"):
        op.alter_column(table_name, "id", server_default=None)
    for table_name in _NETWORK_TABLES:
        op.alter_column(table_name, "updated_at", server_default=None)
        op.alter_column(table_name, "created_at", server_default=None)
