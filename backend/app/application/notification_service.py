"""Notification queueing and delivery (Issue #103, area C).

Queueing happens inside the transaction that records the event (a collector transition, a correlation
incident) by inserting `notification_delivery` rows; nothing is ever sent from there. A Celery task on the
`notifications` queue sends each row afterwards, so a slow or failing provider cannot delay or fail telemetry
ingestion, heartbeat processing or alarm evaluation.

Guarantees:
* one delivery per (policy, event): `dedup_key` is unique and inserted with ON CONFLICT DO NOTHING;
* at most one worker owns an attempt: the claim is a single UPDATE that bumps `claim_generation`, and every
  later write carries that generation, so a worker that lost its lease writes nothing (the stale-owner race
  fixed for idempotency keys in #122 is not reintroduced);
* bounded retries with exponential backoff and a fixed failure-code vocabulary; provider text, URLs and
  secrets are never stored or logged;
* the body sent is the sanitized payload stored at queue time, not a fresh read of mutable state."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.outbound_http import OutboundError, OutboundResponse, outbound_request
from app.core.logging import get_logger
from app.core.secrets import SecretDecryptionError, decrypt_secret
from app.domain.operations.models import (
    CollectorTransition,
    CorrelationIncident,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
)

logger = get_logger(__name__)

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

Sender = Callable[[str, bytes, dict[str, str]], Awaitable[OutboundResponse]]


def backoff_seconds(attempts: int) -> int:
    """Delay after the `attempts`-th failed attempt: 30 s, 60 s, 120 s ... capped at one hour."""
    return min(BASE_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)


async def _matching_policies(db: AsyncSession, event_type: str, site_id: uuid.UUID | None, confidence: str | None):
    rows = (
        await db.execute(
            select(NotificationPolicy, NotificationChannel)
            .join(NotificationChannel, NotificationChannel.id == NotificationPolicy.channel_id)
            .where(NotificationPolicy.enabled.is_(True), NotificationChannel.enabled.is_(True))
            .order_by(NotificationPolicy.id)
        )
    ).all()
    out = []
    for policy, channel in rows:
        if event_type not in (policy.event_types or []):
            continue
        if policy.site_id is not None and policy.site_id != site_id:
            continue
        if policy.min_confidence and confidence is not None:
            if CONFIDENCE_RANK[confidence] < CONFIDENCE_RANK[policy.min_confidence]:
                continue
        out.append((policy, channel))
    return out


async def enqueue_notifications(
    db: AsyncSession, *, event_type: str, source_type: str, source_id: uuid.UUID, site_id: uuid.UUID | None,
    payload: dict, correlation_id: str, incident_id: uuid.UUID | None = None, confidence: str | None = None,
    dedup_suffix: str = "", now: datetime | None = None,
) -> list[uuid.UUID]:
    """Insert deliveries for every matching policy. Returns the ids that were newly created; ids that
    already existed (a replay) are not returned, so a replay dispatches nothing."""
    now = now or datetime.now(UTC)
    created: list[uuid.UUID] = []
    for policy, channel in await _matching_policies(db, event_type, site_id, confidence):
        key = f"{policy.id}:{event_type}:{source_id}{dedup_suffix}"
        stmt = (
            insert(NotificationDelivery)
            .values(
                id=uuid.uuid4(), policy_id=policy.id, channel_id=channel.id, incident_id=incident_id, dedup_key=key,
                event_type=event_type, source_type=source_type, source_id=source_id, correlation_id=correlation_id,
                status="pending", attempts=0, max_attempts=MAX_ATTEMPTS, next_attempt_at=now, claim_generation=0,
                final_failure=False, payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_notification_delivery_dedup_key")
            .returning(NotificationDelivery.id)
        )
        row = (await db.execute(stmt)).first()
        if row is not None:
            created.append(row[0])
            await write_audit_log(
                db, actor_user_id=None, action="notification.delivery.queued", entity_type="notification_delivery",
                entity_id=row[0], request_id=None, correlation_id=correlation_id, source="system",
                after={"event_type": event_type, "policy_id": str(policy.id)},
            )
    db.info.setdefault("pending_dispatch", []).extend(created)
    return created


async def enqueue_for_collector_transition(db: AsyncSession, transition: CollectorTransition) -> list[uuid.UUID]:
    event_type = "collector.offline" if transition.to_state == "offline" else "collector.online"
    payload = {
        "event": event_type, "collector_id": str(transition.collector_id), "collector_name": transition.collector_name,
        "site_id": str(transition.site_id) if transition.site_id else None, "state": transition.to_state,
        "generation": transition.generation, "detected_at": transition.detected_at.isoformat(),
        "heartbeat_at": transition.heartbeat_at.isoformat() if transition.heartbeat_at else None,
        "correlation_id": transition.correlation_id,
    }
    return await enqueue_notifications(
        db, event_type=event_type, source_type="collector_transition", source_id=transition.id,
        site_id=transition.site_id, payload=payload, correlation_id=transition.correlation_id or str(transition.id),
        now=transition.detected_at,
    )


async def enqueue_for_incident(
    db: AsyncSession, incident: CorrelationIncident, *, event_type: str = "incident.opened", suffix: str = ""
) -> list[uuid.UUID]:
    payload = {
        "event": event_type, "incident_id": str(incident.id), "rule": incident.rule, "confidence": incident.confidence,
        "probable_cause": incident.cause_label, "rationale": incident.rationale,
        "site_id": str(incident.site_id) if incident.site_id else None, "opened_at": incident.opened_at.isoformat(),
        "correlation_id": incident.correlation_id,
    }
    return await enqueue_notifications(
        db, event_type=event_type, source_type="incident", source_id=incident.id, site_id=incident.site_id,
        payload=payload, correlation_id=incident.correlation_id, incident_id=incident.id,
        confidence=incident.confidence, dedup_suffix=suffix,
    )


def dispatch_pending(db: AsyncSession) -> int:
    """Hand newly queued delivery ids to Celery. Call after the transaction that queued them has committed.
    A broker failure is swallowed on purpose: the rows are durable and the sweeper re-dispatches them."""
    ids = db.info.pop("pending_dispatch", [])
    if not ids:
        return 0
    from app.infrastructure.tasks.notifications import deliver_notification

    sent = 0
    for delivery_id in ids:
        try:
            deliver_notification.apply_async(args=[str(delivery_id)], queue="notifications")
            sent += 1
        except Exception as exc:  # noqa: BLE001 - provider and broker failures must not reach callers
            logger.warning("notification_dispatch_failed", error_code=type(exc).__name__)
    return sent


# ----------------------------------------------------------------------------------------- delivery


@dataclass
class _Claim:
    delivery_id: uuid.UUID
    generation: int
    attempts: int
    max_attempts: int
    channel_id: uuid.UUID
    payload: dict
    correlation_id: str


async def _claim(db: AsyncSession, delivery_id: uuid.UUID, now: datetime) -> _Claim | None:
    stmt = (
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            or_(
                and_(NotificationDelivery.status.in_(("pending", "retry")), NotificationDelivery.next_attempt_at <= now),
                and_(NotificationDelivery.status == "sending", NotificationDelivery.lease_expires_at < now),
            ),
        )
        .values(
            status="sending", claim_generation=NotificationDelivery.claim_generation + 1,
            attempts=NotificationDelivery.attempts + 1, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        )
        .returning(
            NotificationDelivery.claim_generation, NotificationDelivery.attempts, NotificationDelivery.max_attempts,
            NotificationDelivery.channel_id, NotificationDelivery.payload, NotificationDelivery.correlation_id,
        )
    )
    row = (await db.execute(stmt)).first()
    await db.commit()
    if row is None:
        return None
    return _Claim(delivery_id, row[0], row[1], row[2], row[3], row[4], row[5])


async def _finish(
    db: AsyncSession, claim: _Claim, *, status: str, now: datetime, failure_code: str | None = None,
    http_status: int | None = None, next_attempt_at: datetime | None = None,
) -> bool:
    """Fenced write: matches only while this worker still owns the claim."""
    values: dict = {
        "status": status, "lease_expires_at": None, "last_http_status": http_status, "failure_code": failure_code,
        "final_failure": status == "failed",
    }
    if status == "sent":
        values["sent_at"] = now
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    won = (
        await db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == claim.delivery_id,
                NotificationDelivery.claim_generation == claim.generation,
                NotificationDelivery.status == "sending",
            )
            .values(**values)
            .returning(NotificationDelivery.id)
        )
    ).first()
    if won is not None and status in ("sent", "failed"):
        await write_audit_log(
            db, actor_user_id=None, action=f"notification.delivery.{status}", entity_type="notification_delivery",
            entity_id=claim.delivery_id, request_id=None, correlation_id=claim.correlation_id, source="system",
            after={"attempts": claim.attempts, "failure_code": failure_code, "http_status": http_status},
        )
    await db.commit()
    return won is not None


def classify_response(status_code: int) -> tuple[bool, str | None, bool]:
    """(success, failure_code, retryable)."""
    if 200 <= status_code < 300:
        return True, None, False
    if status_code == 429:
        return False, "RATE_LIMITED", True
    if status_code == 408 or status_code >= 500:
        return False, "HTTP_5XX", True
    return False, "HTTP_4XX", False  # includes unfollowed 3xx redirects and 401/403/404


def sign_body(secret: str, body: bytes, timestamp: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def default_sender(
    url: str, body: bytes, headers: dict[str, str], transport: httpx.AsyncBaseTransport | None = None
) -> OutboundResponse:
    return await outbound_request("POST", url, headers=headers, content=body, transport=transport)


async def deliver(
    db: AsyncSession, delivery_id: uuid.UUID, *, now: datetime | None = None, sender: Sender | None = None
) -> str:
    """Send one delivery. Returns sent | retry | failed | skipped. Never raises for a provider failure."""
    now = now or datetime.now(UTC)
    claim = await _claim(db, delivery_id, now)
    if claim is None:
        return "skipped"
    if claim.attempts > claim.max_attempts:
        await _finish(db, claim, status="failed", now=now, failure_code="ATTEMPTS_EXHAUSTED")
        return "failed"
    channel = await db.get(NotificationChannel, claim.channel_id)
    if channel is None or not channel.enabled:
        await _finish(db, claim, status="failed", now=now, failure_code="CHANNEL_DISABLED")
        return "failed"
    try:
        url = decrypt_secret(channel.url_ciphertext)
        secret = decrypt_secret(channel.secret_ciphertext) if channel.secret_ciphertext else None
    except SecretDecryptionError:
        await _finish(db, claim, status="failed", now=now, failure_code="INTERNAL_ERROR")
        return "failed"

    body = json.dumps(claim.payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(now.timestamp()))
    headers = {
        "Content-Type": "application/json", "X-DCIM-Delivery-Id": str(claim.delivery_id),
        "X-DCIM-Correlation-Id": claim.correlation_id, "X-DCIM-Timestamp": timestamp,
    }
    if secret:
        headers["X-DCIM-Signature"] = "sha256=" + sign_body(secret, body, timestamp)
    send = sender or default_sender
    failure_code: str | None
    http_status: int | None = None
    retry_after: int | None = None
    try:
        response = await send(url, body, headers)
        http_status = response.status_code
        retry_after = response.retry_after_seconds
        ok, failure_code, retryable = classify_response(response.status_code)
        if ok:
            await _finish(db, claim, status="sent", now=now, http_status=http_status)
            return "sent"
    except OutboundError as exc:
        failure_code = exc.code if exc.code in ("TIMEOUT", "CONNECTION_ERROR", "TARGET_BLOCKED") else "CONNECTION_ERROR"
        retryable = failure_code in ("TIMEOUT", "CONNECTION_ERROR")
    except Exception as exc:  # noqa: BLE001 - an adapter bug must not leak text or crash the worker
        logger.error("notification_delivery_unexpected_error", delivery_id=str(claim.delivery_id), error_code=type(exc).__name__)
        failure_code, retryable = "INTERNAL_ERROR", True

    if retryable and claim.attempts < claim.max_attempts:
        delay = max(backoff_seconds(claim.attempts), retry_after or 0)
        await _finish(
            db, claim, status="retry", now=now, failure_code=failure_code, http_status=http_status,
            next_attempt_at=now + timedelta(seconds=min(delay, MAX_BACKOFF_SECONDS)),
        )
        logger.info("notification_delivery_retry", delivery_id=str(claim.delivery_id), attempts=claim.attempts, code=failure_code)
        return "retry"
    final_code = "ATTEMPTS_EXHAUSTED" if retryable else failure_code
    await _finish(db, claim, status="failed", now=now, failure_code=final_code, http_status=http_status)
    logger.warning("notification_delivery_failed", delivery_id=str(claim.delivery_id), code=final_code)
    return "failed"


async def requeue_failed(db: AsyncSession, delivery_id: uuid.UUID, *, now: datetime | None = None) -> bool:
    """Operator retry of a final failure: one more bounded round, fenced by the same claim generation."""
    now = now or datetime.now(UTC)
    won = (
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id, NotificationDelivery.status == "failed")
            .values(
                status="pending", attempts=0, next_attempt_at=now, final_failure=False, failure_code=None,
                lease_expires_at=None,
            )
            .returning(NotificationDelivery.id)
        )
    ).first()
    return won is not None


async def due_delivery_ids(db: AsyncSession, now: datetime, limit: int = 100) -> list[uuid.UUID]:
    rows = await db.execute(
        select(NotificationDelivery.id)
        .where(
            or_(
                and_(NotificationDelivery.status.in_(("pending", "retry")), NotificationDelivery.next_attempt_at <= now),
                and_(NotificationDelivery.status == "sending", NotificationDelivery.lease_expires_at < now),
            )
        )
        .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
        .limit(limit)
    )
    return list(rows.scalars())
