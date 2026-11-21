"""Idempotency-Key handling (§21, §36). Distinguishes request identity (X-Request-Id,
one HTTP call), event identity (OutboxEvent.event_id), job identity (Celery task id), and
this — the *client-supplied* identity of one logical write, used so a retried POST
returns the original result instead of repeating the write.

Finding H1 correction (docs/history/PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
docs/history/PHASE1_CORRECTION_REPORT.md): the original implementation checked the idempotency cache,
then performed the write, then stored the response — with no serialization between the
check and the store, so N genuinely concurrent identical requests all passed the cache
check, all attempted the write, and N-1 of them surfaced the resulting database conflict
as an incorrect client-facing error instead of replaying the one write that succeeded.

The corrected flow uses `idempotency_key`'s unique `(principal, key, endpoint)` index as an
atomic claim, not just a cache (issue #337: the authenticated principal scopes every record):

1. Attempt `INSERT ... ON CONFLICT (principal, key, endpoint) DO NOTHING RETURNING id`, committed
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

Fencing (stale-owner race, NEW-1): a claimant that is slow rather than dead can resume after
its claim was reclaimed. Every claim therefore carries a monotonically increasing
`claim_generation`. Reclaiming bumps it in the same compare-and-swap UPDATE, and
`complete_claim()` / `release_claim()` only act on a row whose status is 'processing' AND whose
generation still equals the one the caller was issued. A fenced-out owner gets
`IdempotencyClaimLost` from `complete_claim()` (its caller's transaction, including the
domain write, is rolled back) and `release_claim()` becomes a no-op, so it can never complete,
overwrite or delete the replacement owner's claim.

Expiry (DB-03, issue #357): a ledger row is a bounded-window record, not a permanent one. A row whose
`expires_at <= now` (inclusive) is treated as absent *before* the request-hash comparison: the next request
for that key replaces it through a non-blocking compare-and-swap (`_try_replace_expired`) that bumps the
fencing generation once, clears the stored response, records the new hash and renews `expires_at`. After
expiry the same key therefore executes again; a create whose permanent business key already exists
answers 409, not the original 201 (docs/implementation/IDEMPOTENCY_EXPIRY.md). `expires_at` is renewed on
every transition into 'processing' (new claim, stale reclaim, expired replacement) and never on completion.

Two clocks are deliberately separate: `_now()` (injectable) decides expiry and staleness, while the polling
deadline uses `time.monotonic()` taken at function entry, so a frozen or skewed expiry clock can never
hang the loop and a blocked first INSERT consumes the same budget as polling.
"""

import asyncio
import hashlib
import json
import time
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

# Principals this module generates. Anything else (notably the migration's inert `legacy:unscoped`
# marker) is rejected so a caller can never adopt or replay a legacy row.
_ALLOWED_PRINCIPAL_PREFIXES = ("user:", "collector:")
_MAX_PRINCIPAL_LENGTH = 100


def _now() -> datetime:
    """Expiry/staleness clock. Injectable (monkeypatched in tests); never used for the polling deadline."""
    return datetime.now(UTC)


def _monotonic() -> float:
    """Polling-deadline clock. Independent of `_now()` so a frozen expiry clock cannot hang the loop."""
    return time.monotonic()


def _validate_principal(principal: str) -> None:
    if not isinstance(principal, str) or not principal or not principal.strip():
        raise ValueError("an idempotency claim requires a non-empty authenticated principal")
    if len(principal) > _MAX_PRINCIPAL_LENGTH or not principal.startswith(_ALLOWED_PRINCIPAL_PREFIXES):
        raise ValueError("an idempotency claim requires a user: or collector: principal")
    subject = principal.split(":", 1)[1]
    if not subject or any(ch.isspace() or ord(ch) < 32 for ch in subject):
        raise ValueError("an idempotency claim requires a well-formed principal")


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was reused with a different request body."""


class IdempotencyClaimLost(Exception):
    """This caller's claim was reclaimed by another request (its generation is stale) or no
    longer exists, so it must not complete it. The caller's transaction must be rolled back;
    the replacement owner (or a retry) performs the write."""


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


@dataclass(frozen=True)
class ClaimRef:
    """Plain-value identity of a claim (row id + fencing generation). Safe to hold across a
    rollback, which expires the ORM `IdempotencyKey` attributes."""

    id: uuid.UUID
    generation: int

    @classmethod
    def of(cls, claim: IdempotencyKey) -> "ClaimRef":
        return cls(id=claim.id, generation=claim.claim_generation)


@dataclass(frozen=True)
class Acquired:
    """Row id and the fencing generation a claim statement actually issued (RETURNING, not re-read)."""

    id: uuid.UUID
    generation: int


def hash_request_body(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def user_principal(user_id: uuid.UUID) -> str:
    """Namespaced principal for an authenticated end user (server-derived, never client-supplied)."""
    return f"user:{user_id}"


def collector_principal(collector_id: uuid.UUID) -> str:
    """Namespaced principal for a signature-authenticated collector."""
    return f"collector:{collector_id}"


async def _try_claim(
    db: AsyncSession, *, principal: str, key: str, endpoint: str, request_hash: str, ttl: timedelta
) -> "Acquired | None":
    table = _TABLE
    stmt = (
        pg_insert(table)
        .values(
            id=uuid.uuid4(),
            principal=principal,
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
            status="processing",
            response_status=None,
            response_body=None,
            expires_at=_now() + ttl,
        )
        .on_conflict_do_nothing(index_elements=["principal", "key", "endpoint"])
        .returning(table.c.id, table.c.claim_generation)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    await db.commit()
    return Acquired(row[0], row[1]) if row is not None else None


async def _try_reclaim_stale(
    db: AsyncSession, record: IdempotencyKey, *, ttl: timedelta = _DEFAULT_TTL
) -> "Acquired | None":
    """Compare-and-swap takeover of a non-expired 'processing' row whose owner went quiet. Renews
    `expires_at` (a new processing generation always starts with a full lifetime)."""
    table = _TABLE
    now = _now()
    stale_before = now - STALE_CLAIM_TIMEOUT
    stmt = (
        update(table)
        .where(
            table.c.id == record.id,
            table.c.status == "processing",
            table.c.claim_generation == record.claim_generation,
            table.c.updated_at < stale_before,
        )
        .values(
            status="processing", updated_at=now, expires_at=now + ttl,
            claim_generation=table.c.claim_generation + 1,
        )
        .returning(table.c.id, table.c.claim_generation)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    await db.commit()
    return Acquired(row[0], row[1]) if row is not None else None


async def _try_replace_expired(
    db: AsyncSession, record: IdempotencyKey, *, request_hash: str, now: datetime, ttl: timedelta
) -> "Acquired | None":
    """Non-blocking compare-and-swap that starts a new processing generation on an expired row.

    The row is matched by id AND the generation this caller observed AND the inclusive expiry predicate, and
    is locked with FOR UPDATE SKIP LOCKED: a row that is locked (a concurrent replacement, an owner
    completing, a purge) or that changed yields zero rows immediately, never a wait. Exactly one contender
    wins and bumps the generation once. No rollback is ever issued here (a session rollback would expire the
    caller's ORM objects, e.g. `ctx.user`), only an immediate commit."""
    table = _TABLE
    target = (
        select(table.c.id)
        .where(
            table.c.id == record.id,
            table.c.claim_generation == record.claim_generation,
            table.c.expires_at <= now,
        )
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(table)
        .where(table.c.id == target)
        .values(
            request_hash=request_hash, status="processing", response_status=None, response_body=None,
            claim_generation=table.c.claim_generation + 1, expires_at=now + ttl, updated_at=now,
        )
        .returning(table.c.id, table.c.claim_generation)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    await db.commit()
    return Acquired(row[0], row[1]) if row is not None else None


async def _load_claim(db: AsyncSession, acquired: "Acquired") -> IdempotencyKey:
    """Reload the row this caller just acquired, and prove it is still the caller's.

    The acquiring statement committed (releasing its connection) before this read, so another request may
    have reclaimed, replaced, completed or released the row in between. `populate_existing=True` is required
    (the identity map may hold an older generation), but it would also silently hand this caller the NEWER
    owner's generation, so the reload must match exactly the generation the statement returned and still be
    'processing'. Anything else means the claim was lost before it was ever used: the caller must not act on
    it, and complete_claim/release_claim (fenced on the issued generation) stay harmless to the new owner."""
    claim = await db.get(IdempotencyKey, acquired.id, populate_existing=True)
    if claim is None or claim.status != "processing" or claim.claim_generation != acquired.generation:
        raise IdempotencyClaimLost()
    return claim


async def get_or_claim(
    db: AsyncSession, *, principal: str, key: str, endpoint: str, request_hash: str,
    ttl: timedelta = _DEFAULT_TTL,
) -> IdempotencyOutcome:
    """Returns either a completed record to replay, or a claim this caller now owns.

    `principal` is the server-derived authenticated identity (user_principal / collector_principal).
    Every claim, lookup, replay, replacement and stale reclaim is scoped to it, so two principals sharing a
    key and endpoint can neither replay nor block each other (issue #337). `ttl` is the lifetime given to
    every claim generation this call starts (24 h for user endpoints; collector ingestion passes its own).

    Waiting is bounded by a monotonic deadline taken at entry: if the window is exhausted the caller gets
    IdempotencyStillProcessing. If the claim this call just acquired was taken over before it could be loaded
    (a pause between the claim statement's commit and the reload), IdempotencyClaimLost is raised: the caller
    never owns a claim, so it must not release anything. The database can still make an individual statement wait (a lock holder,
    the connection pool), which this deadline does not and cannot interrupt."""
    _validate_principal(principal)
    deadline = _monotonic() + _POLL_MAX_WAIT_SECONDS

    acquired = await _try_claim(
        db, principal=principal, key=key, endpoint=endpoint, request_hash=request_hash, ttl=ttl
    )
    if acquired is not None:
        return IdempotencyOutcome(claim=await _load_claim(db, acquired))

    select_stmt = (
        select(IdempotencyKey)
        .where(
            IdempotencyKey.principal == principal, IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint
        )
        .execution_options(populate_existing=True)
    )
    while True:
        record = (await db.execute(select_stmt)).scalar_one_or_none()
        now = _now()
        if record is None:
            # The prior claimant released it (its write failed) or a purge removed it: claim it fresh.
            acquired = await _try_claim(
                db, principal=principal, key=key, endpoint=endpoint, request_hash=request_hash, ttl=ttl
            )
            if acquired is not None:
                return IdempotencyOutcome(claim=await _load_claim(db, acquired))
        elif record.expires_at <= now:
            # Inclusive boundary, evaluated BEFORE the hash comparison: an expired row is not a conflict.
            acquired = await _try_replace_expired(db, record, request_hash=request_hash, now=now, ttl=ttl)
            if acquired is not None:
                return IdempotencyOutcome(claim=await _load_claim(db, acquired))
        elif record.request_hash != request_hash:
            raise IdempotencyConflict()
        elif record.status == "completed":
            return IdempotencyOutcome(cached=record)
        elif now - record.updated_at > STALE_CLAIM_TIMEOUT:
            acquired = await _try_reclaim_stale(db, record, ttl=ttl)
            if acquired is not None:
                return IdempotencyOutcome(claim=await _load_claim(db, acquired))

        # Every iteration that did not return passes through the deadline check, then the sleep: no
        # failed replace/reclaim/insert can spin without bound.
        if _monotonic() >= deadline:
            raise IdempotencyStillProcessing()
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def complete_claim(db: AsyncSession, claim: IdempotencyKey, *, response_status: int, response_body: dict) -> None:
    """Must be called within the same transaction as the write it is completing, before
    that transaction's own commit — so the domain write and the idempotency completion
    become visible to other transactions atomically together.

    Fenced: the UPDATE only matches while this caller still owns the claim (status
    'processing' and the generation it was issued). Otherwise it raises IdempotencyClaimLost
    and the caller's transaction, domain write included, must be rolled back."""
    table = _TABLE
    stmt = (
        update(table)
        .where(table.c.id == claim.id, table.c.status == "processing", table.c.claim_generation == claim.claim_generation)
        .values(status="completed", response_status=response_status, response_body=response_body)
        .returning(table.c.id)
    )
    if (await db.execute(stmt)).scalar_one_or_none() is None:
        raise IdempotencyClaimLost()
    await db.refresh(claim)


async def release_claim(db: AsyncSession, claim: ClaimRef) -> None:
    """Called after the owning write failed and its transaction was rolled back — deletes
    the claim in a fresh transaction so the key does not become permanently poisoned and a
    retry (or a request that was waiting on it) can claim it again.

    Fenced: a caller that lost its claim deletes nothing, so it cannot remove the
    replacement owner's in-flight claim."""
    table = _TABLE
    await db.execute(
        table.delete().where(
            table.c.id == claim.id, table.c.status == "processing", table.c.claim_generation == claim.generation
        )
    )
    await db.commit()
