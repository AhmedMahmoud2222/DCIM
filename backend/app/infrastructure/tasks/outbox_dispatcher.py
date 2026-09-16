"""Outbox dispatcher foundation (§19). Phase 1 has no domain-specific handlers yet (no
notification/capacity/alarm modules exist) — `process_outbox_event` is deliberately a
no-op-but-real dispatch (logs, marks dispatched) so the *mechanism* — at-least-once
delivery, per-event idempotency via event_id, retry with backoff, dead-letter after max
attempts — is real and testable without inventing a future phase's handler logic."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.sync_session import get_sync_db
from app.domain.outbox.models import OutboxEvent
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 100
PROCESSING_TIMEOUT = timedelta(minutes=5)


@celery_app.task(name="app.infrastructure.tasks.outbox_dispatcher.dispatch_pending_outbox_events")
def dispatch_pending_outbox_events() -> int:
    """Reads pending/retry-due rows and enqueues one idempotent task per event. Uses
    SKIP LOCKED so a second dispatcher run (or a second worker) never double-claims a row
    still being processed — this is the concrete mechanism behind §22's "commit order"
    reading, made race-safe (§46 M3 of the red-team report names exactly this class of
    concern)."""
    now = datetime.now(UTC)
    stale_processing_before = now - PROCESSING_TIMEOUT
    with get_sync_db() as db:
        stmt = (
            select(OutboxEvent)
            .where(
                (
                    (OutboxEvent.status == "pending")
                    & ((OutboxEvent.next_attempt_at.is_(None)) | (OutboxEvent.next_attempt_at <= now))
                )
                # Reclaims rows stuck in "processing" (e.g. the worker that claimed them
                # crashed, or the message was lost before it reached a task handler) —
                # without this, a lost delivery would stall that event forever, which is
                # not "at-least-once" (§19 of the Phase 1 prompt: "failure recovery").
                | ((OutboxEvent.status == "processing") & (OutboxEvent.updated_at < stale_processing_before))
            )
            .order_by(OutboxEvent.created_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        events = db.execute(stmt).scalars().all()
        for event in events:
            event.status = "processing"
        db.commit()
        event_ids = [str(e.event_id) for e in events]

    for event_id in event_ids:
        process_outbox_event.delay(event_id)
    return len(event_ids)


@celery_app.task(
    name="app.infrastructure.tasks.outbox_dispatcher.process_outbox_event",
    bind=True,
    max_retries=MAX_ATTEMPTS,
)
def process_outbox_event(self, event_id: str) -> None:
    with get_sync_db() as db:
        stmt = select(OutboxEvent).where(OutboxEvent.event_id == event_id)
        event = db.execute(stmt).scalar_one_or_none()
        if event is None:
            return
        if event.status == "dispatched":
            return  # already handled by a prior, duplicate delivery — idempotent no-op

        try:
            logger.info(
                "outbox_event_dispatched",
                event_id=str(event.event_id),
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=str(event.aggregate_id),
                correlation_id=event.correlation_id,
            )
            event.status = "dispatched"
            db.commit()
        except Exception as exc:  # noqa: BLE001 — this boundary must never crash the worker
            event.attempts += 1
            event.last_error = str(exc)[:2000]
            if event.attempts >= MAX_ATTEMPTS:
                event.status = "failed"
            else:
                event.status = "pending"
                event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2**event.attempts)
            db.commit()
            logger.error("outbox_event_dispatch_failed", event_id=str(event.event_id), error=str(exc))
