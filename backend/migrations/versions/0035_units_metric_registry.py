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
    op.add_column("integration_metric_mapping", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("telemetry_reading", sa.Column("raw_value", sa.Numeric(18, 8), nullable=True))
    op.add_column("telemetry_reading", sa.Column("raw_unit", sa.String(32), nullable=True))
    op.add_column("telemetry_reading", sa.Column("source_scale", sa.Numeric(18, 8), nullable=True))
    op.add_column("telemetry_reading", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("daily_telemetry_aggregate", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("alarm_rule", sa.Column("unit", sa.String(32), nullable=True))
    op.add_column("alarm_rule", sa.Column("registry_version", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("alarm_rule", "registry_version")
    op.drop_column("alarm_rule", "unit")
    op.drop_column("daily_telemetry_aggregate", "registry_version")
    op.drop_column("telemetry_reading", "registry_version")
    op.drop_column("telemetry_reading", "source_scale")
    op.drop_column("telemetry_reading", "raw_unit")
    op.drop_column("telemetry_reading", "raw_value")
    op.drop_column("integration_metric_mapping", "registry_version")
