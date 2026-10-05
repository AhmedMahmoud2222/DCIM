"""Add source telemetry provenance and registry version without rewriting history.

Revision ID: 0035_units_metric_registry
Revises: 0034_asset_decommission_guard
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_units_metric_registry"
down_revision = "0034_asset_decommission_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_metric_mapping", sa.Column("registry_version", sa.String(16), server_default="1", nullable=False))
    op.add_column("telemetry_reading", sa.Column("raw_value", sa.Numeric(18, 8), nullable=True))
    op.add_column("telemetry_reading", sa.Column("raw_unit", sa.String(32), nullable=True))
    op.add_column("telemetry_reading", sa.Column("registry_version", sa.String(16), server_default="1", nullable=False))


def downgrade() -> None:
    op.drop_column("telemetry_reading", "registry_version")
    op.drop_column("telemetry_reading", "raw_unit")
    op.drop_column("telemetry_reading", "raw_value")
    op.drop_column("integration_metric_mapping", "registry_version")
