"""Persistent MVP alarm rules and their deterministic lifecycle instances."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

RULE_TYPES = ("threshold_high", "threshold_low", "availability_unavailable")
ALARM_STATUSES = ("ACTIVE", "ACKNOWLEDGED", "CLEARED")


class AlarmRule(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "alarm_rule"
    __table_args__ = (
        CheckConstraint(f"rule_type IN {RULE_TYPES!r}", name="rule_type_allowed"),
        CheckConstraint("(rule_type = 'availability_unavailable' AND threshold IS NULL) OR "
                        "(rule_type != 'availability_unavailable' AND threshold IS NOT NULL)",
                        name="threshold_matches_rule_type"),
        Index("ix_alarm_rule_integration_metric", "integration_id", "metric"),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float | None] = mapped_column(Numeric(18, 8))
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Alarm(Base, UUIDPkMixin):
    __tablename__ = "alarm"
    __table_args__ = (
        CheckConstraint(f"status IN {ALARM_STATUSES!r}", name="status_allowed"),
        Index("ix_alarm_integration_status_opened", "integration_id", "status", "opened_at"),
        # PostgreSQL partial unique index: one open lifecycle per rule/subject.
        Index("uq_alarm_open_rule_subject", "rule_id", "subject_key", unique=True,
              postgresql_where="status IN ('ACTIVE', 'ACKNOWLEDGED')"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alarm_rule.id", ondelete="CASCADE"), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    telemetry_reading_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("telemetry_reading.id", ondelete="SET NULL"))
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"), index=True)
    subject_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    cleared_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
