"""OutboxEvent per ARCHITECTURE_REVIEW.md §22. Written in the same transaction as the
domain mutation and audit row it accompanies; a separate dispatcher (app/infrastructure/
tasks/outbox_dispatcher.py) reads pending rows and delivers them at-least-once to
idempotent handlers. Never a replacement for the synchronous AuditLog write — see §22's
"Important distinction" for why the two are not the same mechanism."""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

STATUS_VALUES = ("pending", "processing", "dispatched", "failed")


class OutboxEvent(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "outbox_event"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUS_VALUES!r}", name="status_allowed"),
        Index("ix_outbox_event_status_next_attempt", "status", "next_attempt_at"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(unique=True, nullable=False, server_default=func.gen_random_uuid())
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    causation_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(2000))
