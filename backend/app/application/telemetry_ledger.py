"""Consumed-key ledger: stops a redelivered record from being counted twice once its raw row is gone (Issue #128 / G4-R).

Compaction and a late merge delete the raw reading, and the reading's unique ``(collector_id, dedup_key)`` key goes with it.
Each consumed record therefore leaves a tombstone in ``telemetry_consumed_key``. The tombstone insert
(``ON CONFLICT DO NOTHING RETURNING``) is the arbiter: a sample may be folded into an aggregate only by the transaction whose
insert succeeded, so two deliveries of one record, or a delivery racing a compaction, are counted once. Everything runs in the
caller's transaction, so a failure rolls back the tombstone together with the aggregate and the deletes.

Horizon (``monitoring_policy.late_replay_horizon_days``). NULL, the default, means no age-based rejection and no pruning.
A finite value is applied as ``min(horizon, now - ledger_complete_since)``: tombstones that were already pruned are never
trusted, so raising the horizon takes effect gradually. Logs carry fixed fields only, never keys, hashes or collector ids.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.telemetry.models import MonitoringPolicy, TelemetryConsumedKey, TelemetryReplayLedgerState

logger = get_logger(__name__)

# asyncpg allows 32,767 bind parameters per statement; one parameter pair per tombstone.
CLAIM_CHUNK = 5000


class ReplayHorizonExceeded(ValueError):
    """A late record is older than the configured replay horizon (permanent; the collector must not retry it)."""


class _KeyedRow(Protocol):
    id: uuid.UUID
    collector_id: uuid.UUID
    dedup_key: str


def consumed_key_hash(collector_id: uuid.UUID, dedup_key: str) -> bytes:
    """First 16 bytes of SHA-256 over ``collector_id || 0x00 || dedup_key`` (the collector id keeps collectors disjoint)."""
    digest = hashlib.sha256(str(collector_id).encode("ascii") + b"\x00" + dedup_key.encode("utf-8"))
    return digest.digest()[:16]


async def claim_consumed_keys(db: AsyncSession, rows: Sequence[_KeyedRow], consumed_day: date) -> set[uuid.UUID]:
    """Record the rows' keys as consumed and return the ids of the rows whose key was NOT already consumed.

    A row missing from the result is a duplicate: its key has a tombstone from an earlier consumption, so its value must not
    be counted again. Safe under concurrency: a concurrent claim of the same key blocks on the primary key until the other
    transaction ends, then sees the committed tombstone (or none, if it rolled back).
    """
    fresh: set[uuid.UUID] = set()
    for start in range(0, len(rows), CLAIM_CHUNK):
        chunk = rows[start:start + CLAIM_CHUNK]
        by_hash = {consumed_key_hash(row.collector_id, row.dedup_key): row.id for row in chunk}
        statement = (
            pg_insert(TelemetryConsumedKey)
            .values([{"key_hash": key_hash, "consumed_day": consumed_day} for key_hash in by_hash])
            .on_conflict_do_nothing(index_elements=[TelemetryConsumedKey.key_hash])
            .returning(TelemetryConsumedKey.key_hash)
        )
        fresh.update(by_hash[key_hash] for key_hash in (await db.execute(statement)).scalars().all())
    return fresh


async def effective_horizon(db: AsyncSession, *, now: datetime) -> timedelta | None:
    """The age beyond which late data is refused, or None when no finite horizon is configured.

    Reads the ledger state ``FOR SHARE`` so a prune run (which advances ``ledger_complete_since`` ``FOR UPDATE`` before it
    deletes anything) waits for in-flight merges and every later merge sees the advanced value.
    """
    configured = (
        await db.execute(
            select(MonitoringPolicy.late_replay_horizon_days).where(MonitoringPolicy.id == 1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if configured is None:
        return None
    state = await _locked_state(db, now=now, for_update=False)
    protected = max(now - state.ledger_complete_since, timedelta(0))
    return min(timedelta(days=configured), protected)


async def enforce_horizon(db: AsyncSession, *, occurred_at: datetime, now: datetime) -> None:
    horizon = await effective_horizon(db, now=now)
    if horizon is not None and now - occurred_at > horizon:
        logger.warning("telemetry_replay_horizon_refused", path="late_merge")
        raise ReplayHorizonExceeded("The record is older than the replay horizon and can no longer be merged.")


async def _locked_state(db: AsyncSession, *, now: datetime, for_update: bool) -> TelemetryReplayLedgerState:
    """The single state row, locked. A missing row (never created by the migration) is re-created conservatively as 'now'."""
    await db.execute(
        pg_insert(TelemetryReplayLedgerState)
        .values(id=1, ledger_complete_since=now)
        .on_conflict_do_nothing(index_elements=[TelemetryReplayLedgerState.id])
    )
    statement = (
        select(TelemetryReplayLedgerState).where(TelemetryReplayLedgerState.id == 1)
        .with_for_update(read=not for_update).execution_options(populate_existing=True)
    )
    return (await db.execute(statement)).scalar_one()


def note_duplicate_suppressed(*, path: str, count: int = 1) -> None:
    logger.info("telemetry_replay_duplicate_suppressed", path=path, count=count)

