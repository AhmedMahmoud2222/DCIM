"""Thin MVP metric mappings and authoritative telemetry readings.

Discovery remains an acquisition observation; telemetry is stored separately and never
mutates discovered or authoritative inventory state.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

CANONICAL_METRICS = ("temperature_c", "humidity_percent", "power_kw", "load_percent", "availability")


class IntegrationMetricMapping(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "integration_metric_mapping"
    __table_args__ = (
        UniqueConstraint("integration_id", "source_identifier", name="uq_metric_mapping_integration_source"),
        CheckConstraint(f"canonical_metric IN {CANONICAL_METRICS!r}", name="canonical_metric_allowed"),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    scale: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=1)
    label: Mapped[str | None] = mapped_column(String(128))


class TelemetryReading(Base, UUIDPkMixin):
    __tablename__ = "telemetry_reading"
    __table_args__ = (
        UniqueConstraint("collector_id", "dedup_key", name="uq_telemetry_reading_collector_dedup"),
        Index("ix_telemetry_reading_integration_metric_occurred", "integration_id", "metric", "occurred_at"),
        Index("ix_telemetry_reading_asset_metric_occurred", "managed_asset_id", "metric", "occurred_at"),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="RESTRICT"), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_metric_mapping.id", ondelete="RESTRICT"), nullable=False
    )
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"), index=True)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DailyTelemetryAggregate(Base, UUIDPkMixin):
    """Long-term, loss-aware daily representation of sensor telemetry only.

    Alarm history deliberately has no relationship to this table or the retention job.
    """

    __tablename__ = "daily_telemetry_aggregate"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_identifier", "metric", "day", name="uq_daily_telemetry_sensor_metric_day"),
        Index("ix_daily_telemetry_integration_metric_day", "integration_id", "metric", "day"),
        Index("ix_daily_telemetry_asset_metric_day", "managed_asset_id", "metric", "day"),
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"), nullable=True)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[date] = mapped_column(nullable=False)
    average_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    minimum_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    maximum_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    sample_count: Mapped[int] = mapped_column(nullable=False)


class MonitoringPolicy(Base):
    """Singleton persisted policy; cleanup is always an explicit controlled job."""
    __tablename__ = "monitoring_policy"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    default_poll_interval_seconds: Mapped[int] = mapped_column(nullable=False, default=300)
    raw_retention_days: Mapped[int] = mapped_column(nullable=False, default=365)
    daily_aggregate_retention_days: Mapped[int | None] = mapped_column(nullable=True)
    alarm_history_retention_days: Mapped[int | None] = mapped_column(nullable=True)
