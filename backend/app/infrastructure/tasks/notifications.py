"""Celery tasks for Issue #103 notifications, on the existing `notifications` queue."""

import uuid
from datetime import UTC, datetime

from app.application import collector_health_sweep, notification_service
from app.core.logging import get_logger
from app.infrastructure.celery_app import celery_app
from app.infrastructure.tasks.async_runner import run_with_session

logger = get_logger(__name__)
NOTIFICATIONS_QUEUE = "notifications"


@celery_app.task(name="app.infrastructure.tasks.notifications.deliver_notification")
def deliver_notification(delivery_id: str) -> str:
    outcome = {"value": "skipped"}

    async def _body(db) -> None:
        outcome["value"] = await notification_service.deliver(db, uuid.UUID(delivery_id))

    run_with_session(_body)
    return outcome["value"]


@celery_app.task(name="app.infrastructure.tasks.notifications.redispatch_due_notifications")
def redispatch_due_notifications() -> int:
    """Pick up retries that are due and deliveries whose first dispatch never ran or whose lease expired."""
    found: list[str] = []

    async def _body(db) -> None:
        found.extend(str(i) for i in await notification_service.due_delivery_ids(db, datetime.now(UTC)))

    run_with_session(_body)
    for delivery_id in found:
        try:
            deliver_notification.apply_async(args=[delivery_id], queue=NOTIFICATIONS_QUEUE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification_redispatch_failed", error_code=type(exc).__name__)
    return len(found)


@celery_app.task(name="app.infrastructure.tasks.notifications.sweep_collector_health")
def sweep_collector_health() -> int:
    """Scheduled collector offline/online sweep. Provider problems cannot reach it: it only queues rows."""
    count = {"n": 0}

    async def _body(db) -> None:
        results = await collector_health_sweep.sweep_collector_states(db)
        count["n"] = len(results)
        notification_service.dispatch_pending(db)

    run_with_session(_body)
    return count["n"]
