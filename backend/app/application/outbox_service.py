"""Writes OutboxEvent rows in the caller's own transaction (§22). The row must never be
inserted independently of the authoritative mutation it accompanies — always call this
inside the same `db` session/transaction as the domain write, before commit."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.outbox.models import OutboxEvent


async def write_outbox_event(
    db: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> OutboxEvent:
    event = OutboxEvent(
        event_id=uuid.uuid4(),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at=datetime.now(UTC),
        status="pending",
        attempts=0,
    )
    db.add(event)
    await db.flush()
    return event
