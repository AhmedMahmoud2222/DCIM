"""Idempotency-Key handling (§21, §36). Distinguishes request identity (X-Request-Id,
one HTTP call), event identity (OutboxEvent.event_id), job identity (Celery task id), and
this — the *client-supplied* identity of one logical write, used so a retried POST
returns the original result instead of repeating the write.

Finding H1 correction (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
PHASE1_CORRECTION_REPORT.md): the original implementation checked the idempotency cache,
then performed the write, then stored the response — with no serialization between the
check and the store, so N genuinely concurrent identical requests all passed the cache
check, all attempted the write, and N-1 of them surfaced the resulting database conflict
as an incorrect client-facing error instead of replaying the one write that succeeded.

The corrected flow uses `idempotency_key`'s existing unique `(key, endpoint)` index as an
atomic claim, not just a cache:

1. Attempt `INSERT ... ON CONFLICT (key, endpoint) DO NOTHING RETURNING id`, committed
   immediately (so the claim is visible to other transactions under MVCC, not just at the
   end of the caller's own transaction). PostgreSQL's unique index serializes this
   correctly across true concurrency — exactly one concurrent INSERT can win.
2. If this request won the claim, the caller performs the write and calls
   `complete_claim()`, which flips the row to 'completed' with the real response, in the
   *same* transaction as the write (so the domain write and the idempotency record become
   visible atomically together).
3. If this request lost the claim, it waits (short, bounded polling — READ COMMITTED
   means each re-SELECT sees newly committed data without needing a fresh transaction)
   for the winner's row to reach 'completed', then replays that response. A losing
   request whose key was used with a *different* request body raises IdempotencyConflict
   immediately, without waiting.
4. If the claim owner's write fails, `release_claim()` deletes the row (in a fresh
   transaction, since the caller's own transaction is being rolled back) so the key is not
   permanently poisoned and a retry can claim it fresh.
5. If the original claimant crashed or hung (row stuck in 'processing' past
   STALE_CLAIM_TIMEOUT), a waiting request may reclaim it via a compare-and-swap UPDATE,
   mirroring the outbox dispatcher's stale-processing reclaim pattern
   (app/infrastructure/tasks/outbox_dispatcher.py).
"""

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency.models import IdempotencyKey

_TABLE = cast(Table, IdempotencyKey.__table__)

_DEFAULT_TTL = timedelta(hours=24)
STALE_CLAIM_TIMEOUT = timedelta(seconds=30)
_POLL_INTERVAL_SECONDS = 0.05
_POLL_MAX_WAIT_SECONDS = 5.0


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was reused with a different request body."""


class IdempotencyStillProcessing(Exception):
    """The claim owner has not completed within the polling window — genuinely still in
    flight, not stale enough to reclaim. The caller should map this to a retryable
    response (e.g. 503), never to a false conflict."""


@dataclass
class IdempotencyOutcome:
    """Exactly one of `cached` (a completed record to replay) or `claim` (this caller now
    owns the key and must call complete_claim()/release_claim()) is set."""

    cached: IdempotencyKey | None = None
    claim: IdempotencyKey | None = None


def hash_request_body(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _try_claim(db: AsyncSession, *, key: str, endpoint: str, request_hash: str) -> uuid.UUID | None:
    table = _TABLE
    stmt = (
        pg_insert(table)
        .values(
            id=uuid.uuid4(),
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
            status="processing",
            response_status=None,
            response_body=None,
            expires_at=datetime.now(UTC) + _DEFAULT_TTL,
        )
        .on_conflict_do_nothing(index_elements=["key", "endpoint"])
        .returning(table.c.id)
    )
    result = await db.execute(stmt)
    claimed_id = result.scalar_one_or_none()
    await db.commit()
    return claimed_id


async def _try_reclaim_stale(db: AsyncSession, record: IdempotencyKey) -> uuid.UUID | None:
    table = _TABLE
    now = datetime.now(UTC)
    stale_before = now - STALE_CLAIM_TIMEOUT
    stmt = (
        update(table)
        .where(table.c.id == record.id, table.c.status == "processing", table.c.updated_at < stale_before)
        .values(status="processing", updated_at=now)
        .returning(table.c.id)
    )
    result = await db.execute(stmt)
    reclaimed_id = result.scalar_one_or_none()
    await db.commit()
    return reclaimed_id


async def get_or_claim(db: AsyncSession, *, key: str, endpoint: str, request_hash: str) -> IdempotencyOutcome:
    """Returns either a completed record to replay, or a claim this caller now owns."""
    claimed_id = await _try_claim(db, key=key, endpoint=endpoint, request_hash=request_hash)
    if claimed_id is not None:
        claim = await db.get(IdempotencyKey, claimed_id)
        assert claim is not None
        return IdempotencyOutcome(claim=claim)

    deadline = datetime.now(UTC) + timedelta(seconds=_POLL_MAX_WAIT_SECONDS)
    select_stmt = (
        select(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint)
        .execution_options(populate_existing=True)
    )
    while True:
        record = (await db.execute(select_stmt)).scalar_one_or_none()
        if record is None:
            # The prior claimant released it (its write failed) — try to claim it fresh.
            claimed_id = await _try_claim(db, key=key, endpoint=endpoint, request_hash=request_hash)
            if claimed_id is not None:
                claim = await db.get(IdempotencyKey, claimed_id)
                assert claim is not None
                return IdempotencyOutcome(claim=claim)
            continue

        if record.request_hash != request_hash:
            raise IdempotencyConflict()

        if record.status == "completed":
            return IdempotencyOutcome(cached=record)

        now = datetime.now(UTC)
        if now - record.updated_at > STALE_CLAIM_TIMEOUT:
            reclaimed_id = await _try_reclaim_stale(db, record)
            if reclaimed_id is not None:
                claim = await db.get(IdempotencyKey, reclaimed_id, populate_existing=True)
                assert claim is not None
                return IdempotencyOutcome(claim=claim)
            continue  # someone else reclaimed it first; re-check its new state

        if now > deadline:
            raise IdempotencyStillProcessing()

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def complete_claim(db: AsyncSession, claim: IdempotencyKey, *, response_status: int, response_body: dict) -> None:
    """Must be called within the same transaction as the write it is completing, before
    that transaction's own commit — so the domain write and the idempotency completion
    become visible to other transactions atomically together."""
    claim.status = "completed"
    claim.response_status = response_status
    claim.response_body = response_body
    await db.flush()


async def release_claim(db: AsyncSession, claim_id: uuid.UUID) -> None:
    """Called after the owning write failed and its transaction was rolled back — deletes
    the claim in a fresh transaction so the key does not become permanently poisoned and a
    retry (or a request that was waiting on it) can claim it again."""
    table = _TABLE
    await db.execute(table.delete().where(table.c.id == claim_id, table.c.status == "processing"))
    await db.commit()
