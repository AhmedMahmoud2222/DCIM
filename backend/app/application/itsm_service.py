"""Incident to ticket workflow (Issue #103, areas D, E, I). Provider-neutral; adapters live in `itsm/`.

Sync rules (outbound first, no inbound path exists):
* DCIM facts are authoritative. The ticket row stores only the remote id, number and a normalised state; that
  state is never read to change an incident, alarm, asset, topology or telemetry.
* Closing a remote ticket does not resolve the incident or clear any alarm. Resolving an incident in DCIM is an
  operator action that queues an outbound *update*; the provider's reaction is recorded, not obeyed.
* Everything sent is built from the incident record (cause, rationale, confidence, counts), so a ticket carries
  only what the incident's own scope already holds.

Idempotency: `correlation_key` is derived from (connection, incident) and is also written to the provider's
`correlation_id` field. A create first looks that key up, so an attempt that died after the provider accepted
the ticket but before DCIM committed is adopted on retry instead of duplicated. Claims are fenced by
`claim_generation` exactly like notification deliveries."""

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.itsm.base import CONFIDENCE_URGENCY, ItsmAdapter, ItsmError, RemoteTicket, TicketPayload
from app.application.itsm.servicenow import ServiceNowAdapter
from app.application.notification_service import CONFIDENCE_RANK, LEASE_SECONDS, MAX_ATTEMPTS, backoff_seconds
from app.core.logging import get_logger
from app.core.secrets import SecretDecryptionError, decrypt_secret
from app.domain.operations.models import (
    CorrelationIncident,
    CorrelationIncidentMember,
    ItsmConnection,
    ItsmTicket,
)

logger = get_logger(__name__)
MAX_BACKOFF = 3600

AdapterFactory = Callable[[ItsmConnection, str], ItsmAdapter]


def correlation_key_for(connection_id: uuid.UUID, incident_dedup_key: str) -> str:
    return "dcim-" + hashlib.sha256(f"{connection_id}:{incident_dedup_key}".encode()).hexdigest()[:27]


def build_payload(incident: CorrelationIncident, member_count: int, key: str) -> TicketPayload:
    urgency = CONFIDENCE_URGENCY.get(incident.confidence, 3)
    description = (
        f"DCIM incident {incident.id}\nRule: {incident.rule}\nConfidence: {incident.confidence}\n"
        f"Probable cause: {incident.cause_label}\n{incident.rationale}\nSource events: {member_count}\n"
        f"Status in DCIM: {incident.status}\nCorrelation: {incident.correlation_id}"
    )
    return TicketPayload(key, f"[DCIM] {incident.cause_label}"[:160], description[:4000], urgency, urgency)


def payload_hash(p: TicketPayload) -> str:
    return hashlib.sha256(
        "\x1f".join([p.correlation_key, p.short_description, p.description, str(p.urgency), str(p.impact)]).encode()
    ).hexdigest()


def default_adapter(connection: ItsmConnection, password: str) -> ItsmAdapter:
    return ServiceNowAdapter(connection.base_url, connection.username, password)


async def ensure_ticket(
    db: AsyncSession, incident: CorrelationIncident, connection: ItsmConnection, *, now: datetime | None = None
) -> tuple[uuid.UUID, bool]:
    """Create the ticket row for (connection, incident) once. Returns (ticket_id, newly_created)."""
    now = now or datetime.now(UTC)
    ticket_id = uuid.uuid4()
    row = (
        await db.execute(
            insert(ItsmTicket)
            .values(
                id=ticket_id,
                connection_id=connection.id,
                incident_id=incident.id,
                correlation_key=correlation_key_for(connection.id, incident.dedup_key),
                external_state="unknown",
                status="pending",
                pending_op="create",
                attempts=0,
                next_attempt_at=now,
                claim_generation=0,
            )
            .on_conflict_do_nothing(constraint="uq_itsm_ticket_connection_incident")
            .returning(ItsmTicket.id)
        )
    ).first()
    if row is None:
        existing = (
            await db.execute(
                select(ItsmTicket.id).where(ItsmTicket.connection_id == connection.id, ItsmTicket.incident_id == incident.id)
            )
        ).scalar_one()
        return existing, False
    await write_audit_log(
        db,
        actor_user_id=None,
        action="itsm.ticket.queued",
        entity_type="itsm_ticket",
        entity_id=ticket_id,
        request_id=None,
        correlation_id=incident.correlation_id,
        source="system",
        after={"incident_id": str(incident.id), "connection_id": str(connection.id)},
    )
    db.info.setdefault("pending_ticket_dispatch", []).append(ticket_id)
    return ticket_id, True


async def queue_for_incident(db: AsyncSession, incident: CorrelationIncident, *, now: datetime | None = None) -> list[uuid.UUID]:
    """Automatic creation for connections that opted in (`auto_create`), site and confidence permitting."""
    connections = (
        (
            await db.execute(
                select(ItsmConnection)
                .where(ItsmConnection.enabled.is_(True), ItsmConnection.auto_create.is_(True))
                .order_by(ItsmConnection.id)
            )
        )
        .scalars()
        .all()
    )
    out: list[uuid.UUID] = []
    for c in connections:
        if c.site_id is not None and c.site_id != incident.site_id:
            continue
        if c.min_confidence and CONFIDENCE_RANK[incident.confidence] < CONFIDENCE_RANK[c.min_confidence]:
            continue
        ticket_id, _ = await ensure_ticket(db, incident, c, now=now)
        out.append(ticket_id)
    return out


async def request_update(db: AsyncSession, incident_id: uuid.UUID, *, now: datetime | None = None) -> list[uuid.UUID]:
    """Queue an outbound update for tickets that already exist (an operator changed the incident)."""
    now = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                update(ItsmTicket)
                .where(ItsmTicket.incident_id == incident_id, ItsmTicket.status.in_(("synced", "failed")))
                .values(
                    status="pending",
                    pending_op="update",
                    attempts=0,
                    next_attempt_at=now,
                    failure_code=None,
                    lease_expires_at=None,
                )
                .returning(ItsmTicket.id)
            )
        )
        .scalars()
        .all()
    )
    db.info.setdefault("pending_ticket_dispatch", []).extend(rows)
    return list(rows)


def dispatch_pending_tickets(db: AsyncSession) -> int:
    ids = db.info.pop("pending_ticket_dispatch", [])
    if not ids:
        return 0
    from app.infrastructure.tasks.itsm import sync_itsm_ticket

    sent = 0
    for ticket_id in ids:
        try:
            sync_itsm_ticket.apply_async(args=[str(ticket_id)], queue="notifications")
            sent += 1
        except Exception as exc:  # noqa: BLE001 - the row is durable; the sweeper re-dispatches
            logger.warning("itsm_dispatch_failed", error_code=type(exc).__name__)
    return sent


async def _claim(db: AsyncSession, ticket_id: uuid.UUID, now: datetime):
    row = (
        await db.execute(
            update(ItsmTicket)
            .where(
                ItsmTicket.id == ticket_id,
                or_(
                    and_(ItsmTicket.status.in_(("pending", "retry")), ItsmTicket.next_attempt_at <= now),
                    and_(ItsmTicket.status == "syncing", ItsmTicket.lease_expires_at < now),
                ),
            )
            .values(
                status="syncing",
                claim_generation=ItsmTicket.claim_generation + 1,
                attempts=ItsmTicket.attempts + 1,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            )
            .returning(ItsmTicket.claim_generation, ItsmTicket.attempts, ItsmTicket.pending_op)
        )
    ).first()
    await db.commit()
    return row


async def _finish(db: AsyncSession, ticket_id: uuid.UUID, generation: int, **values) -> bool:
    values.setdefault("lease_expires_at", None)
    won = (
        await db.execute(
            update(ItsmTicket)
            .where(ItsmTicket.id == ticket_id, ItsmTicket.claim_generation == generation, ItsmTicket.status == "syncing")
            .values(**values)
            .returning(ItsmTicket.id)
        )
    ).first()
    await db.commit()
    return won is not None


async def sync_ticket(
    db: AsyncSession, ticket_id: uuid.UUID, *, now: datetime | None = None, adapter_factory: AdapterFactory | None = None
) -> str:
    """Create or update one ticket. Returns synced | retry | failed | skipped. Never raises for a provider failure."""
    now = now or datetime.now(UTC)
    claim = await _claim(db, ticket_id, now)
    if claim is None:
        return "skipped"
    generation, attempts, op = claim
    ticket = await db.get(ItsmTicket, ticket_id, populate_existing=True)
    assert ticket is not None
    connection = await db.get(ItsmConnection, ticket.connection_id)
    incident = await db.get(CorrelationIncident, ticket.incident_id)
    if connection is None or not connection.enabled or incident is None:
        await _finish(db, ticket_id, generation, status="failed", failure_code="CONNECTION_DISABLED")
        return "failed"
    if attempts > MAX_ATTEMPTS:
        await _finish(db, ticket_id, generation, status="failed", failure_code="ATTEMPTS_EXHAUSTED")
        return "failed"
    try:
        password = decrypt_secret(connection.secret_ciphertext)
    except SecretDecryptionError:
        await _finish(db, ticket_id, generation, status="failed", failure_code="INTERNAL_ERROR")
        return "failed"
    members = len(
        (await db.execute(select(CorrelationIncidentMember.id).where(CorrelationIncidentMember.incident_id == incident.id))).all()
    )
    payload = build_payload(incident, members, ticket.correlation_key)
    adapter = (adapter_factory or default_adapter)(connection, password)
    try:
        remote: RemoteTicket | None
        if op == "update" and ticket.external_id:
            if ticket.last_payload_hash == payload_hash(payload):
                await _finish(db, ticket_id, generation, status="synced", failure_code=None, last_synced_at=now)
                return "synced"
            remote = await adapter.update(ticket.external_id, payload)
        else:
            remote = await adapter.find(ticket.correlation_key)  # adopt a ticket an earlier attempt may have created
            if remote is None:
                remote = await adapter.create(payload)
            elif ticket.last_payload_hash != payload_hash(payload):
                remote = await adapter.update(remote.external_id, payload)
        await _finish(
            db,
            ticket_id,
            generation,
            status="synced",
            pending_op="update",
            external_id=remote.external_id,
            external_number=remote.number,
            external_state=remote.state,
            failure_code=None,
            last_http_status=remote.http_status,
            last_synced_at=now,
            last_payload_hash=payload_hash(payload),
        )
        await write_audit_log(
            db,
            actor_user_id=None,
            action="itsm.ticket.synced",
            entity_type="itsm_ticket",
            entity_id=ticket_id,
            request_id=None,
            correlation_id=incident.correlation_id,
            source="system",
            after={"number": remote.number, "state": remote.state, "op": op},
        )
        await db.commit()
        return "synced"
    except ItsmError as exc:
        code, retryable, http_status, retry_after = exc.code, exc.retryable, exc.http_status, exc.retry_after
    except Exception as exc:  # noqa: BLE001 - adapter bug: fixed code, no text
        logger.error("itsm_sync_unexpected_error", ticket_id=str(ticket_id), error_code=type(exc).__name__)
        code, retryable, http_status, retry_after = "INTERNAL_ERROR", True, None, None

    if retryable and attempts < MAX_ATTEMPTS:
        delay = min(max(backoff_seconds(attempts), retry_after or 0), MAX_BACKOFF)
        await _finish(
            db,
            ticket_id,
            generation,
            status="retry",
            failure_code=code,
            last_http_status=http_status,
            next_attempt_at=now + timedelta(seconds=delay),
        )
        logger.info("itsm_sync_retry", ticket_id=str(ticket_id), attempts=attempts, code=code)
        return "retry"
    final = "ATTEMPTS_EXHAUSTED" if retryable else code
    await _finish(db, ticket_id, generation, status="failed", failure_code=final, last_http_status=http_status)
    await write_audit_log(
        db,
        actor_user_id=None,
        action="itsm.ticket.failed",
        entity_type="itsm_ticket",
        entity_id=ticket_id,
        request_id=None,
        correlation_id=incident.correlation_id,
        source="system",
        after={"failure_code": final},
    )
    await db.commit()
    logger.warning("itsm_sync_failed", ticket_id=str(ticket_id), code=final)
    return "failed"


async def requeue_failed_ticket(db: AsyncSession, ticket_id: uuid.UUID, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    won = (
        await db.execute(
            update(ItsmTicket)
            .where(ItsmTicket.id == ticket_id, ItsmTicket.status == "failed")
            .values(status="pending", attempts=0, next_attempt_at=now, failure_code=None, lease_expires_at=None)
            .returning(ItsmTicket.id)
        )
    ).first()
    if won is not None:
        db.info.setdefault("pending_ticket_dispatch", []).append(ticket_id)
    return won is not None


async def due_ticket_ids(db: AsyncSession, now: datetime, limit: int = 100) -> list[uuid.UUID]:
    rows = await db.execute(
        select(ItsmTicket.id)
        .where(
            or_(
                and_(ItsmTicket.status.in_(("pending", "retry")), ItsmTicket.next_attempt_at <= now),
                and_(ItsmTicket.status == "syncing", ItsmTicket.lease_expires_at < now),
            )
        )
        .order_by(ItsmTicket.next_attempt_at, ItsmTicket.id)
        .limit(limit)
    )
    return list(rows.scalars())
