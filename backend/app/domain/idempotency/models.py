"""Idempotency-Key ledger for POST endpoints with external side effects (§21, §36).
Distinct from request_id (one HTTP call), event_id (one outbox event), and job_id (one
Celery task) — this is specifically the client-supplied identity of one logical write,
used to make a retried request return the original result instead of repeating the write.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class IdempotencyKey(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "idempotency_key"
    __table_args__ = (Index("ix_idempotency_key_key_endpoint", "key", "endpoint", unique=True),)

    key: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
