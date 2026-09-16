"""Idempotency-Key handling (§21, §36). Distinguishes request identity (X-Request-Id,
one HTTP call), event identity (OutboxEvent.event_id), job identity (Celery task id), and
this — the *client-supplied* identity of one logical write, used so a retried POST
returns the original result instead of repeating the write."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency.models import IdempotencyKey

_DEFAULT_TTL = timedelta(hours=24)


def hash_request_body(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def get_cached_response(db: AsyncSession, *, key: str, endpoint: str) -> IdempotencyKey | None:
    stmt = select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint)
    return (await db.execute(stmt)).scalar_one_or_none()


async def store_response(
    db: AsyncSession,
    *,
    key: str,
    endpoint: str,
    request_hash: str,
    response_status: int,
    response_body: dict,
) -> IdempotencyKey:
    record = IdempotencyKey(
        id=uuid.uuid4(),
        key=key,
        endpoint=endpoint,
        request_hash=request_hash,
        response_status=response_status,
        response_body=response_body,
        expires_at=datetime.now(UTC) + _DEFAULT_TTL,
    )
    db.add(record)
    await db.flush()
    return record
