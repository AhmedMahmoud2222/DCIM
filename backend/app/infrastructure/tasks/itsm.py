"""Celery tasks for ITSM ticket sync, on the `notifications` queue."""

import uuid
from datetime import UTC, datetime

from app.application import itsm_service
from app.core.logging import get_logger
from app.infrastructure.celery_app import celery_app
from app.infrastructure.tasks.async_runner import run_with_session

logger = get_logger(__name__)


@celery_app.task(name="app.infrastructure.tasks.itsm.sync_itsm_ticket")
def sync_itsm_ticket(ticket_id: str) -> str:
    outcome = {"value": "skipped"}

    async def _body(db) -> None:
        outcome["value"] = await itsm_service.sync_ticket(db, uuid.UUID(ticket_id))

    run_with_session(_body)
    return outcome["value"]


@celery_app.task(name="app.infrastructure.tasks.itsm.redispatch_due_tickets")
def redispatch_due_tickets() -> int:
    found: list[str] = []

    async def _body(db) -> None:
        found.extend(str(i) for i in await itsm_service.due_ticket_ids(db, datetime.now(UTC)))

    run_with_session(_body)
    for ticket_id in found:
        try:
            sync_itsm_ticket.apply_async(args=[ticket_id], queue="notifications")
        except Exception as exc:  # noqa: BLE001
            logger.warning("itsm_redispatch_failed", error_code=type(exc).__name__)
    return len(found)
