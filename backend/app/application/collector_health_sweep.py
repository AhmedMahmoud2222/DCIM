"""Collector offline/online transitions (Issue #103, area B).

A collector's health stays derived from `CollectorHeartbeat` rows (`classify_collector_health`); this module
only *records transitions* of that derived state, so an operator and the notification workflow can react to
a change instead of polling for a level. It reuses the approved thresholds from `collector_service`:

* age <= HEARTBEAT_STALE_AFTER_SECONDS      healthy
* age <= HEARTBEAT_OFFLINE_AFTER_SECONDS    stale (still online)
* age >  HEARTBEAT_OFFLINE_AFTER_SECONDS    offline

so a collector whose last heartbeat is exactly 300 s old is still online and one 300.001 s old is offline.
A collector that has never sent a heartbeat is judged by the age of its registration instead.

Recovery needs a heartbeat strictly newer than the one the offline decision rested on, not older than the
offline threshold, and not further in the future than a small clock skew. A late-arriving old row therefore
cannot revive a collector, and neither can the same heartbeat seen twice.

Transitions are written with a compare-and-swap on `collector_state.generation` under a transaction-scoped
advisory lock; `(collector_id, generation)` is also unique in `collector_transition`, so even a sweep that
somehow bypassed the lock could not record the same transition twice. The sweep only reads heartbeats and
writes its own tables: it never touches ingestion, alarms or the heartbeat history."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.collector_service import HEARTBEAT_OFFLINE_AFTER_SECONDS
from app.application.outbox_service import write_outbox_event
from app.domain.integration.models import Collector, CollectorHeartbeat
from app.domain.operations.models import CollectorState, CollectorTransition

CLOCK_SKEW_SECONDS = 60
SWEEP_LOCK_KEY = 1_030_001


@dataclass(frozen=True)
class TransitionResult:
    collector_id: uuid.UUID
    transition_id: uuid.UUID
    from_state: str
    to_state: str
    generation: int


def desired_state(
    *, current: str | None, last_heartbeat_at: datetime | None, offline_basis_heartbeat_at: datetime | None,
    registered_at: datetime, now: datetime,
) -> str:
    """Pure decision used by the sweep and by the boundary tests.

    `current` is the stored state (None when never evaluated); `offline_basis_heartbeat_at` is the latest
    heartbeat the stored offline decision rested on."""
    reference = last_heartbeat_at or registered_at
    age = (now - reference).total_seconds()
    if age > HEARTBEAT_OFFLINE_AFTER_SECONDS:
        return "offline"
    if current == "offline":
        if last_heartbeat_at is None:
            return "offline"
        if offline_basis_heartbeat_at is not None and last_heartbeat_at <= offline_basis_heartbeat_at:
            return "offline"
        if last_heartbeat_at > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
            return "offline"
    return "online"


async def sweep_collector_states(db: AsyncSession, *, now: datetime | None = None) -> list[TransitionResult]:
    now = now or datetime.now(UTC)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SWEEP_LOCK_KEY})
    collectors = (
        await db.execute(select(Collector).where(Collector.status == "active").order_by(Collector.id))
    ).scalars().all()
    if not collectors:
        await db.commit()
        return []
    ids = [c.id for c in collectors]
    latest = {
        row.collector_id: row.ts
        for row in (
            await db.execute(
                select(CollectorHeartbeat.collector_id, CollectorHeartbeat.ts)
                .where(CollectorHeartbeat.collector_id.in_(ids))
                .distinct(CollectorHeartbeat.collector_id)
                .order_by(CollectorHeartbeat.collector_id, CollectorHeartbeat.ts.desc())
            )
        ).all()
    }
    states = {
        s.collector_id: s
        for s in (await db.execute(select(CollectorState).where(CollectorState.collector_id.in_(ids)))).scalars()
    }
    results: list[TransitionResult] = []
    for collector in collectors:
        stored = states.get(collector.id)
        last_hb = latest.get(collector.id)
        decision = desired_state(
            current=stored.state if stored else None, last_heartbeat_at=last_hb,
            offline_basis_heartbeat_at=stored.last_heartbeat_at if stored else None,
            registered_at=collector.created_at, now=now,
        )
        if stored is None:
            if decision == "online":
                db.add(CollectorState(collector_id=collector.id, state="online", generation=1, since=now,
                                      last_heartbeat_at=last_hb, updated_at=now))
                continue
            first = await _record(db, collector, None, "offline", 1, last_hb, now)
            if first is not None:
                results.append(first)
            continue
        if decision == stored.state:
            if last_hb != stored.last_heartbeat_at and stored.state == "online":
                stored.last_heartbeat_at = last_hb
                stored.updated_at = now
            continue
        result = await _record(db, collector, stored, decision, stored.generation + 1, last_hb, now)
        if result is not None:
            results.append(result)
    await db.commit()
    return results


async def _record(
    db: AsyncSession, collector: Collector, stored: CollectorState | None, to_state: str, generation: int,
    heartbeat_at: datetime | None, now: datetime,
) -> TransitionResult | None:
    from_state = stored.state if stored else "unknown"
    if stored is not None:
        won = (
            await db.execute(
                update(CollectorState)
                .where(CollectorState.collector_id == collector.id, CollectorState.generation == stored.generation)
                .values(state=to_state, generation=generation, since=now, last_heartbeat_at=heartbeat_at, updated_at=now)
                .returning(CollectorState.generation)
            )
        ).first()
        if won is None:
            return None  # another sweep already moved this collector
    else:
        db.add(CollectorState(collector_id=collector.id, state=to_state, generation=generation, since=now,
                              last_heartbeat_at=heartbeat_at, updated_at=now))
    correlation_id = f"collector-health:{collector.id}:{generation}"
    transition = CollectorTransition(
        id=uuid.uuid4(), collector_id=collector.id, collector_name=collector.name, site_id=collector.site_id,
        generation=generation, from_state=from_state, to_state=to_state, detected_at=now, heartbeat_at=heartbeat_at,
        never_heartbeat=heartbeat_at is None, correlation_id=correlation_id,
    )
    db.add(transition)
    await db.flush()
    event_type = "CollectorOffline" if to_state == "offline" else "CollectorOnline"
    await write_outbox_event(
        db, event_type=event_type, aggregate_type="collector", aggregate_id=collector.id,
        payload={
            "collector_id": str(collector.id), "collector_name": collector.name,
            "site_id": str(collector.site_id) if collector.site_id else None, "generation": generation,
            "from_state": from_state, "to_state": to_state, "detected_at": now.isoformat(),
            "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        },
        correlation_id=correlation_id,
    )
    await write_audit_log(
        db, actor_user_id=None, action=f"collector.state.{to_state}", entity_type="collector", entity_id=collector.id,
        request_id=None, correlation_id=correlation_id, source="system",
        after={"state": to_state, "generation": generation, "from_state": from_state},
    )
    from app.application.notification_service import enqueue_for_collector_transition

    await enqueue_for_collector_transition(db, transition)
    return TransitionResult(collector.id, transition.id, from_state, to_state, generation)
