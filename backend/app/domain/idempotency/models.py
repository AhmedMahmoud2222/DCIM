"""Idempotency-Key ledger for POST endpoints with external side effects (§21, §36).
Distinct from request_id (one HTTP call), event_id (one outbox event), and job_id (one
Celery task) — this is specifically the client-supplied identity of one logical write,
used to make a retried request return the original result instead of repeating the write.

`status` was added in migration 0003 to close Finding H1
(PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md / PHASE1_CORRECTION_REPORT.md): the unique
`(key, endpoint)` index below is now used as an atomic claim — a request first INSERTs a
'processing' row (INSERT ... ON CONFLICT DO NOTHING, committed immediately so concurrent
requests observe it under MVCC) before doing any work, and only the request that won that
insert performs the actual operation and later flips the row to 'completed' with the
response. See app/application/idempotency.py."""

from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class IdempotencyKey(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "idempotency_key"
    __table_args__ = (
        Index("ix_idempotency_key_key_endpoint", "key", "endpoint", unique=True),
        CheckConstraint("status IN ('processing', 'completed')", name="status_allowed"),
    )

    key: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="completed")
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
