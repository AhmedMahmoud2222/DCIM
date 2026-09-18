"""Daily telemetry aggregates for one-year raw retention.

Raw telemetry is deleted only by the retention service after its aggregate is flushed
inside the same transaction. Alarm tables are intentionally not referenced.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_telemetry_daily_retention"
down_revision = "0010_mvp_alarms"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "daily_telemetry_aggregate",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("managed_asset_id", sa.Uuid(), nullable=True),
        sa.Column("external_identifier", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False), sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("average_value", sa.Numeric(18, 8), nullable=False),
        sa.Column("minimum_value", sa.Numeric(18, 8), nullable=False),
        sa.Column("maximum_value", sa.Numeric(18, 8), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["integration_id"], ["integration.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_asset_id"], ["managed_asset.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("integration_id", "external_identifier", "metric", "day", name="uq_daily_telemetry_sensor_metric_day"),
    )
    op.create_index("ix_daily_telemetry_integration_metric_day", "daily_telemetry_aggregate", ["integration_id", "metric", "day"])
    op.create_index("ix_daily_telemetry_asset_metric_day", "daily_telemetry_aggregate", ["managed_asset_id", "metric", "day"])
    op.create_table("monitoring_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("default_poll_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("raw_retention_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("daily_aggregate_retention_days", sa.Integer(), nullable=True),
        sa.Column("alarm_history_retention_days", sa.Integer(), nullable=True),
        sa.CheckConstraint("id = 1", name="single_monitoring_policy"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO monitoring_policy (id) VALUES (1)")

def downgrade() -> None:
    op.drop_table("monitoring_policy")
    op.drop_index("ix_daily_telemetry_asset_metric_day", table_name="daily_telemetry_aggregate")
    op.drop_index("ix_daily_telemetry_integration_metric_day", table_name="daily_telemetry_aggregate")
    op.drop_table("daily_telemetry_aggregate")
