"""Correct retention-series identity after hostile validation.

Revision ID: 0012_retention_series_identity
Revises: 0011_telemetry_daily_retention
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_retention_series_identity"
down_revision = "0011_telemetry_daily_retention"
branch_labels = None
depends_on = None


def _key_expression() -> str:
    return (
        "integration_id::text || ':' || "
        "COALESCE(managed_asset_id::text, 'unmanaged') || ':' || "
        "external_identifier || ':' || metric || ':' || unit"
    )


def upgrade() -> None:
    # Add nullable first so existing installations can be backfilled without a
    # destructive table rebuild.  The application never writes a blank key.
    op.add_column("telemetry_reading", sa.Column("series_key", sa.String(length=1024), nullable=True))
    op.execute(f"UPDATE telemetry_reading SET series_key = {_key_expression()}")
    op.alter_column("telemetry_reading", "series_key", nullable=False)
    op.create_index("ix_telemetry_reading_series_key", "telemetry_reading", ["series_key"])

    op.add_column("daily_telemetry_aggregate", sa.Column("series_key", sa.String(length=1024), nullable=True))
    op.execute(f"UPDATE daily_telemetry_aggregate SET series_key = {_key_expression()}")
    op.alter_column("daily_telemetry_aggregate", "series_key", nullable=False)
    op.drop_constraint("uq_daily_telemetry_sensor_metric_day", "daily_telemetry_aggregate", type_="unique")
    op.create_unique_constraint("uq_daily_telemetry_series_day", "daily_telemetry_aggregate", ["series_key", "day"])
    op.create_index("ix_daily_telemetry_series_day", "daily_telemetry_aggregate", ["series_key", "day"])


def downgrade() -> None:
    op.drop_index("ix_daily_telemetry_series_day", table_name="daily_telemetry_aggregate")
    op.drop_constraint("uq_daily_telemetry_series_day", "daily_telemetry_aggregate", type_="unique")
    op.create_unique_constraint(
        "uq_daily_telemetry_sensor_metric_day",
        "daily_telemetry_aggregate",
        ["integration_id", "external_identifier", "metric", "day"],
    )
    op.drop_column("daily_telemetry_aggregate", "series_key")
    op.drop_index("ix_telemetry_reading_series_key", table_name="telemetry_reading")
    op.drop_column("telemetry_reading", "series_key")
