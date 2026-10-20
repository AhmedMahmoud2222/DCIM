"""Operator API for Issue #103: correlation incidents, collector transitions, notification channels and
policies, delivery history, and ITSM connections and tickets.

Permissions reuse existing codes (no new ones): incidents need `alarm:read` / `alarm:manage`, collector
history `collector:read`, channels, policies, deliveries and ITSM `integration:read` / `integration:manage`.
None of these codes is site-aware, so a site-restricted user holds none of them and every endpoint here
answers 403 identically for existing and missing ids. Secrets (channel URLs and signing keys, ITSM
passwords) are write-only: they are accepted on create/update and never returned, logged or audited."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application import itsm_service, notification_service
from app.application.audit_service import write_audit_log
from app.application.collector_service import classify_collector_health_bulk
from app.application.concurrency import lock_versioned_row, require_if_match
from app.application.outbound_http import validate_url_shape
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.config import get_settings
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.core.secrets import encrypt_secret
from app.domain.alarm.models import Alarm
from app.domain.integration.models import Collector
from app.domain.location.models import Site
from app.domain.operations.models import (
    CHANNEL_KINDS,
    DELIVERY_STATUSES,
    INCIDENT_STATUSES,
    NOTIFICATION_EVENT_TYPES,
    CollectorState,
    CollectorTransition,
    CorrelationIncident,
    CorrelationIncidentMember,
    ItsmConnection,
    ItsmTicket,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
)

router = APIRouter(prefix="/operations", tags=["operations"])
CONFIDENCES = ("high", "medium", "low")


def _ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


def _bad(detail: str) -> ApiError:
    return ApiError(status_code=422, title="Unprocessable Entity", detail=detail)


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")


def _check_url(url: str, *, allow_path: bool) -> str:
    try:
        validate_url_shape(url, allow_path=allow_path)
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    if url.startswith("http://") and not get_settings().outbound_allow_http:
        raise _bad("URL must use https.")
    return url


# ------------------------------------------------------------------------------------ incidents


class IncidentOut(BaseModel):
    id: uuid.UUID
    rule: str
    cause_type: str
    cause_label: str
    confidence: str
    status: str
    site_id: uuid.UUID | None
    opened_at: datetime
    last_member_at: datetime
    member_count: int
    ticket_count: int
    version: int


class MemberOut(BaseModel):
    member_type: str
    role: str
    source_time: datetime
    alarm_id: uuid.UUID | None = None
    alarm_status: str | None = None
    alarm_subject: str | None = None
    alarm_opened_at: datetime | None = None
    alarm_cleared_at: datetime | None = None
    transition_id: uuid.UUID | None = None
    collector_name: str | None = None
    transition_to_state: str | None = None


class DeliverySummary(BaseModel):
    id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    failure_code: str | None
    sent_at: datetime | None
    channel_name: str


class TicketOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    connection_id: uuid.UUID
    connection_name: str
    status: str
    external_number: str | None
    external_state: str
    attempts: int
    next_attempt_at: datetime
    failure_code: str | None
    last_synced_at: datetime | None


class IncidentDetail(IncidentOut):
    rationale: str
    evidence: list[dict]
    correlation_id: str
    causation_id: str | None
    method_version: str
    resolved_at: datetime | None
    all_sources_cleared: bool
    members: list[MemberOut]
    notifications: list[DeliverySummary]
    tickets: list[TicketOut]


async def _counts(db: AsyncSession, ids: list[uuid.UUID]) -> tuple[dict, dict]:
    m = (
        dict(
            (
                await db.execute(
                    select(CorrelationIncidentMember.incident_id, func.count())
                    .where(CorrelationIncidentMember.incident_id.in_(ids))
                    .group_by(CorrelationIncidentMember.incident_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    t = (
        dict(
            (
                await db.execute(
                    select(ItsmTicket.incident_id, func.count())
                    .where(ItsmTicket.incident_id.in_(ids))
                    .group_by(ItsmTicket.incident_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    return m, t


def _hides_topology(i: CorrelationIncident, can_see_power: bool) -> bool:
    return i.rule == "shared_power_cause" and not can_see_power


def _incident_out(i: CorrelationIncident, members: int, tickets: int, can_see_power: bool = True) -> IncidentOut:
    hidden = _hides_topology(i, can_see_power)
    return IncidentOut(
        id=i.id,
        rule=i.rule,
        cause_type=i.cause_type,
        cause_label="Shared upstream power cause" if hidden else i.cause_label,
        confidence=i.confidence,
        status=i.status,
        site_id=i.site_id,
        opened_at=i.opened_at,
        last_member_at=i.last_member_at,
        member_count=members,
        ticket_count=tickets,
        version=i.version,
    )


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    status: str | None = None,
    site_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("alarm:read")),
) -> Page:
    if status is not None and status not in INCIDENT_STATUSES:
        raise _bad(f"status must be one of {INCIDENT_STATUSES}.")
    stmt = select(CorrelationIncident)
    if status:
        stmt = stmt.where(CorrelationIncident.status == status)
    if site_id:
        stmt = stmt.where(CorrelationIncident.site_id == site_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(CorrelationIncident.opened_at.desc(), CorrelationIncident.id)
                .offset(pagination.offset)
                .limit(pagination.limit)
            )
        )
        .scalars()
        .all()
    )
    mc, tc = await _counts(db, [r.id for r in rows])
    can_power = ctx.has_permission("power:read")
    return Page(
        items=[_incident_out(r, mc.get(r.id, 0), tc.get(r.id, 0), can_power) for r in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


async def _incident(db: AsyncSession, incident_id: uuid.UUID) -> CorrelationIncident:
    inc = await db.get(CorrelationIncident, incident_id)
    if inc is None:
        raise NotFoundError(f"Incident {incident_id} not found.")
    return inc


async def _ticket_out(db: AsyncSession, t: ItsmTicket) -> TicketOut:
    conn = await db.get(ItsmConnection, t.connection_id)
    return TicketOut(
        id=t.id,
        incident_id=t.incident_id,
        connection_id=t.connection_id,
        connection_name=conn.name if conn else "",
        status=t.status,
        external_number=t.external_number,
        external_state=t.external_state,
        attempts=t.attempts,
        next_attempt_at=t.next_attempt_at,
        failure_code=t.failure_code,
        last_synced_at=t.last_synced_at,
    )


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:read"))
) -> IncidentDetail:
    inc = await _incident(db, incident_id)
    mrows = (
        (
            await db.execute(
                select(CorrelationIncidentMember)
                .where(CorrelationIncidentMember.incident_id == inc.id)
                .order_by(
                    CorrelationIncidentMember.role.desc(), CorrelationIncidentMember.source_time, CorrelationIncidentMember.id
                )
            )
        )
        .scalars()
        .all()
    )
    alarm_ids = [m.alarm_id for m in mrows if m.alarm_id]
    alarms = {a.id: a for a in (await db.execute(select(Alarm).where(Alarm.id.in_(alarm_ids)))).scalars()} if alarm_ids else {}
    trans_ids = [m.transition_id for m in mrows if m.transition_id]
    trans = (
        {t.id: t for t in (await db.execute(select(CollectorTransition).where(CollectorTransition.id.in_(trans_ids)))).scalars()}
        if trans_ids
        else {}
    )
    members = []
    for m in mrows:
        a = alarms.get(m.alarm_id) if m.alarm_id else None
        t = trans.get(m.transition_id) if m.transition_id else None
        members.append(
            MemberOut(
                member_type=m.member_type,
                role=m.role,
                source_time=m.source_time,
                alarm_id=m.alarm_id,
                alarm_status=a.status if a else None,
                alarm_subject=a.subject_key if a else None,
                alarm_opened_at=a.opened_at if a else None,
                alarm_cleared_at=a.cleared_at if a else None,
                transition_id=m.transition_id,
                collector_name=t.collector_name if t else None,
                transition_to_state=t.to_state if t else None,
            )
        )
    drows = (
        await db.execute(
            select(NotificationDelivery, NotificationChannel.name)
            .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
            .where(NotificationDelivery.incident_id == inc.id)
            .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
        )
    ).all()
    tickets = (
        (
            await db.execute(
                select(ItsmTicket).where(ItsmTicket.incident_id == inc.id).order_by(ItsmTicket.created_at, ItsmTicket.id)
            )
        )
        .scalars()
        .all()
    )
    # Topology-derived evidence is only shown to callers who may read the power topology.
    can_see_power = ctx.has_permission("power:read")
    evidence = [e for e in inc.evidence if e.get("scope") != "power" or can_see_power]
    alarm_members = [a for a in alarms.values()]
    cleared = bool(alarm_members) and all(a.status == "CLEARED" for a in alarm_members)
    base = _incident_out(inc, len(mrows), len(tickets), can_see_power)
    hidden = _hides_topology(inc, can_see_power)
    return IncidentDetail(
        **base.model_dump(),
        rationale="Probable cause derived from the power topology (details require power:read)." if hidden else inc.rationale,
        evidence=evidence,
        correlation_id=inc.correlation_id,
        causation_id=None if hidden else inc.causation_id,
        method_version=inc.method_version,
        resolved_at=inc.resolved_at,
        all_sources_cleared=cleared,
        members=members,
        notifications=[
            DeliverySummary(
                id=d.id,
                event_type=d.event_type,
                status=d.status,
                attempts=d.attempts,
                max_attempts=d.max_attempts,
                next_attempt_at=d.next_attempt_at,
                failure_code=d.failure_code,
                sent_at=d.sent_at,
                channel_name=name,
            )
            for d, name in drows
        ],
        tickets=[await _ticket_out(db, t) for t in tickets],
    )


async def _transition_incident(
    db: AsyncSession, request: Request, ctx, incident_id: uuid.UUID, version: int, *, to: str
) -> IncidentOut:
    await _incident(db, incident_id)
    inc = await lock_versioned_row(db, CorrelationIncident, incident_id, expected_version=version, label="Incident")
    if to == "acknowledged" and inc.status != "open":
        raise ConflictError(detail=f"Only an open incident can be acknowledged (it is {inc.status}).")
    if to == "resolved" and inc.status == "resolved":
        raise ConflictError(detail="The incident is already resolved.")
    before = {"status": inc.status, "version": inc.version}
    inc.status = to
    if to == "resolved":
        inc.resolved_at, inc.resolved_by = datetime.now(UTC), ctx.user.id
    inc.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    # Only the incident row changes. The alarms it references are deliberately left exactly as they are.
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action=f"correlation.incident.{to}",
        entity_type="correlation_incident",
        entity_id=inc.id,
        request_id=request_id,
        correlation_id=correlation_id,
        before=before,
        after={"status": inc.status, "version": inc.version},
    )
    await write_outbox_event(
        db,
        event_type=f"Incident{to.capitalize()}",
        aggregate_type="correlation_incident",
        aggregate_id=inc.id,
        payload={"status": to},
        correlation_id=inc.correlation_id,
        causation_id=correlation_id,
    )
    if to == "resolved":
        await itsm_service.request_update(db, inc.id)
    await db.commit()
    itsm_service.dispatch_pending_tickets(db)
    mc, tc = await _counts(db, [inc.id])
    return _incident_out(inc, mc.get(inc.id, 0), tc.get(inc.id, 0), ctx.has_permission("power:read"))


@router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentOut)
async def acknowledge_incident(
    incident_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("alarm:manage")),
    version: int = Depends(require_if_match),
) -> IncidentOut:
    return await _transition_incident(db, request, ctx, incident_id, version, to="acknowledged")


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(
    incident_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("alarm:manage")),
    version: int = Depends(require_if_match),
) -> IncidentOut:
    return await _transition_incident(db, request, ctx, incident_id, version, to="resolved")


class TicketRequest(BaseModel):
    connection_id: uuid.UUID


@router.post("/incidents/{incident_id}/tickets", response_model=TicketOut, status_code=202)
async def create_incident_ticket(
    incident_id: uuid.UUID,
    body: TicketRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("alarm:manage")),
) -> TicketOut:
    inc = await _incident(db, incident_id)
    conn = await db.get(ItsmConnection, body.connection_id)
    if conn is None:
        raise NotFoundError(f"ITSM connection {body.connection_id} not found.")
    if not conn.enabled:
        raise ConflictError(detail="The ITSM connection is disabled.")
    ticket_id, created = await itsm_service.ensure_ticket(db, inc, conn)
    request_id, correlation_id = _ids(request)
    if created:
        await write_audit_log(
            db,
            actor_user_id=ctx.user.id,
            action="itsm.ticket.requested",
            entity_type="itsm_ticket",
            entity_id=ticket_id,
            request_id=request_id,
            correlation_id=correlation_id,
            after={"incident_id": str(inc.id)},
        )
    await db.commit()
    itsm_service.dispatch_pending_tickets(db)
    ticket = await db.get(ItsmTicket, ticket_id)
    assert ticket is not None
    return await _ticket_out(db, ticket)


# ------------------------------------------------------------------------ collector transitions


class CollectorStateOut(BaseModel):
    collector_id: uuid.UUID
    name: str
    site_id: uuid.UUID | None
    state: str
    since: datetime
    generation: int
    last_heartbeat_at: datetime | None
    health: str


class TransitionOut(BaseModel):
    id: uuid.UUID
    collector_id: uuid.UUID
    collector_name: str
    site_id: uuid.UUID | None
    generation: int
    from_state: str
    to_state: str
    detected_at: datetime
    heartbeat_at: datetime | None
    never_heartbeat: bool
    correlation_id: str | None

    model_config = {"from_attributes": True}


@router.get("/collector-states", response_model=list[CollectorStateOut])
async def list_collector_states(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("collector:read"))
) -> list[CollectorStateOut]:
    rows = (
        await db.execute(
            select(CollectorState, Collector)
            .join(Collector, Collector.id == CollectorState.collector_id)
            .order_by(Collector.name)
        )
    ).all()
    health = await classify_collector_health_bulk(db, [c.id for _, c in rows])
    return [
        CollectorStateOut(
            collector_id=c.id,
            name=c.name,
            site_id=c.site_id,
            state=s.state,
            since=s.since,
            generation=s.generation,
            last_heartbeat_at=s.last_heartbeat_at,
            health=health[c.id].state,
        )
        for s, c in rows
    ]


@router.get("/collector-transitions", response_model=Page[TransitionOut])
async def list_collector_transitions(
    collector_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("collector:read")),
) -> Page:
    stmt = select(CollectorTransition)
    if collector_id:
        stmt = stmt.where(CollectorTransition.collector_id == collector_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(CollectorTransition.detected_at.desc(), CollectorTransition.id)
                .offset(pagination.offset)
                .limit(pagination.limit)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[TransitionOut.model_validate(r) for r in rows], total=total, limit=pagination.limit, offset=pagination.offset
    )


# ------------------------------------------------------------------------ notification channels


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: SecretStr
    signing_secret: SecretStr | None = None
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: SecretStr | None = None
    signing_secret: SecretStr | None = None
    clear_signing_secret: bool = False
    enabled: bool | None = None


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    url_display: str
    has_signing_secret: bool
    enabled: bool
    version: int


def _channel_out(c: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        url_display=c.url_display,
        has_signing_secret=c.secret_ciphertext is not None,
        enabled=c.enabled,
        version=c.version,
    )


async def _unique_name(db: AsyncSession, model, name: str, current: uuid.UUID | None = None) -> None:
    found = (await db.execute(select(model.id).where(model.name == name))).scalar_one_or_none()
    if found is not None and found != current:
        raise ConflictError(detail=f"A resource named {name!r} already exists.")


@router.post("/notification-channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    body: ChannelIn, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:manage"))
) -> ChannelOut:
    url = _check_url(body.url.get_secret_value(), allow_path=True)
    await _unique_name(db, NotificationChannel, body.name)
    ch = NotificationChannel(
        id=uuid.uuid4(),
        name=body.name,
        kind=CHANNEL_KINDS[0],
        url_display=_origin(url),
        url_ciphertext=encrypt_secret(url),
        secret_ciphertext=encrypt_secret(body.signing_secret.get_secret_value()) if body.signing_secret else None,
        enabled=body.enabled,
        created_by=ctx.user.id,
        version=1,
    )
    db.add(ch)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="notification.channel.create",
        entity_type="notification_channel",
        entity_id=ch.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"name": ch.name, "target": ch.url_display, "signed": ch.secret_ciphertext is not None, "enabled": ch.enabled},
    )
    await db.commit()
    return _channel_out(ch)


@router.get("/notification-channels", response_model=list[ChannelOut])
async def list_channels(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> list[ChannelOut]:
    rows = (await db.execute(select(NotificationChannel).order_by(NotificationChannel.name))).scalars().all()
    return [_channel_out(c) for c in rows]


@router.get("/notification-channels/{channel_id}", response_model=ChannelOut)
async def get_channel(
    channel_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> ChannelOut:
    ch = await db.get(NotificationChannel, channel_id)
    if ch is None:
        raise NotFoundError(f"Channel {channel_id} not found.")
    return _channel_out(ch)


@router.patch("/notification-channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
    version: int = Depends(require_if_match),
) -> ChannelOut:
    if await db.get(NotificationChannel, channel_id) is None:
        raise NotFoundError(f"Channel {channel_id} not found.")
    ch = await lock_versioned_row(db, NotificationChannel, channel_id, expected_version=version, label="Channel")
    changed: dict = {}
    if body.name is not None and body.name != ch.name:
        await _unique_name(db, NotificationChannel, body.name, ch.id)
        ch.name = body.name
        changed["name"] = body.name
    if body.url is not None:
        url = _check_url(body.url.get_secret_value(), allow_path=True)
        ch.url_ciphertext, ch.url_display = encrypt_secret(url), _origin(url)
        changed["target"] = ch.url_display
    if body.signing_secret is not None:
        ch.secret_ciphertext = encrypt_secret(body.signing_secret.get_secret_value())
        changed["signing_secret"] = "rotated"
    elif body.clear_signing_secret:
        ch.secret_ciphertext = None
        changed["signing_secret"] = "removed"
    if body.enabled is not None and body.enabled != ch.enabled:
        ch.enabled = body.enabled
        changed["enabled"] = body.enabled
    ch.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="notification.channel.update",
        entity_type="notification_channel",
        entity_id=ch.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after=changed,
    )
    await db.commit()
    return _channel_out(ch)


# ------------------------------------------------------------------------ notification policies


class PolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    channel_id: uuid.UUID
    event_types: list[str] = Field(min_length=1)
    site_id: uuid.UUID | None = None
    min_confidence: str | None = None
    enabled: bool = True

    @field_validator("event_types")
    @classmethod
    def _events(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if e not in NOTIFICATION_EVENT_TYPES]
        if bad:
            raise ValueError(f"event_types must be among {NOTIFICATION_EVENT_TYPES}.")
        return sorted(set(v))

    @field_validator("min_confidence")
    @classmethod
    def _conf(cls, v: str | None) -> str | None:
        if v is not None and v not in CONFIDENCES:
            raise ValueError(f"min_confidence must be one of {CONFIDENCES}.")
        return v


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    event_types: list[str] | None = Field(default=None, min_length=1)
    min_confidence: str | None = None
    enabled: bool | None = None


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    channel_id: uuid.UUID
    event_types: list[str]
    site_id: uuid.UUID | None
    min_confidence: str | None
    enabled: bool
    version: int

    model_config = {"from_attributes": True}


@router.post("/notification-policies", response_model=PolicyOut, status_code=201)
async def create_policy(
    body: PolicyIn, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:manage"))
) -> PolicyOut:
    if await db.get(NotificationChannel, body.channel_id) is None:
        raise NotFoundError(f"Channel {body.channel_id} not found.")
    if body.site_id is not None and await db.get(Site, body.site_id) is None:
        raise NotFoundError(f"Site {body.site_id} not found.")
    await _unique_name(db, NotificationPolicy, body.name)
    pol = NotificationPolicy(
        id=uuid.uuid4(),
        name=body.name,
        channel_id=body.channel_id,
        event_types=body.event_types,
        site_id=body.site_id,
        min_confidence=body.min_confidence,
        enabled=body.enabled,
        version=1,
    )
    db.add(pol)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="notification.policy.create",
        entity_type="notification_policy",
        entity_id=pol.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"name": pol.name, "events": pol.event_types, "channel_id": str(pol.channel_id)},
    )
    await db.commit()
    return PolicyOut.model_validate(pol)


@router.get("/notification-policies", response_model=list[PolicyOut])
async def list_policies(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> list[PolicyOut]:
    rows = (await db.execute(select(NotificationPolicy).order_by(NotificationPolicy.name))).scalars().all()
    return [PolicyOut.model_validate(r) for r in rows]


@router.patch("/notification-policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
    version: int = Depends(require_if_match),
) -> PolicyOut:
    if await db.get(NotificationPolicy, policy_id) is None:
        raise NotFoundError(f"Policy {policy_id} not found.")
    pol = await lock_versioned_row(db, NotificationPolicy, policy_id, expected_version=version, label="Policy")
    changed: dict = {}
    if body.name is not None and body.name != pol.name:
        await _unique_name(db, NotificationPolicy, body.name, pol.id)
        pol.name = changed["name"] = body.name
    if body.event_types is not None:
        events = PolicyIn(name="x", channel_id=pol.channel_id, event_types=body.event_types).event_types
        pol.event_types = changed["events"] = events
    if body.min_confidence is not None:
        if body.min_confidence not in CONFIDENCES:
            raise _bad(f"min_confidence must be one of {CONFIDENCES}.")
        pol.min_confidence = changed["min_confidence"] = body.min_confidence
    if body.enabled is not None:
        pol.enabled = changed["enabled"] = body.enabled
    pol.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="notification.policy.update",
        entity_type="notification_policy",
        entity_id=pol.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after=changed,
    )
    await db.commit()
    return PolicyOut.model_validate(pol)


# ---------------------------------------------------------------------------------- deliveries


class DeliveryOut(BaseModel):
    id: uuid.UUID
    policy_id: uuid.UUID
    channel_id: uuid.UUID
    incident_id: uuid.UUID | None
    event_type: str
    source_type: str
    source_id: uuid.UUID
    correlation_id: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_http_status: int | None
    failure_code: str | None
    final_failure: bool
    created_at: datetime
    sent_at: datetime | None

    model_config = {"from_attributes": True}


@router.get("/notification-deliveries", response_model=Page[DeliveryOut])
async def list_deliveries(
    status: str | None = None,
    incident_id: uuid.UUID | None = None,
    channel_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("integration:read")),
) -> Page:
    if status is not None and status not in DELIVERY_STATUSES:
        raise _bad(f"status must be one of {DELIVERY_STATUSES}.")
    stmt = select(NotificationDelivery)
    for col, val in (
        (NotificationDelivery.status, status),
        (NotificationDelivery.incident_id, incident_id),
        (NotificationDelivery.channel_id, channel_id),
    ):
        if val is not None:
            stmt = stmt.where(col == val)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id)
                .offset(pagination.offset)
                .limit(pagination.limit)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[DeliveryOut.model_validate(r) for r in rows], total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/notification-deliveries/{delivery_id}", response_model=DeliveryOut)
async def get_delivery(
    delivery_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> DeliveryOut:
    d = await db.get(NotificationDelivery, delivery_id)
    if d is None:
        raise NotFoundError(f"Delivery {delivery_id} not found.")
    return DeliveryOut.model_validate(d)


@router.post("/notification-deliveries/{delivery_id}/retry", response_model=DeliveryOut)
async def retry_delivery(
    delivery_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
) -> DeliveryOut:
    if await db.get(NotificationDelivery, delivery_id) is None:
        raise NotFoundError(f"Delivery {delivery_id} not found.")
    if not await notification_service.requeue_failed(db, delivery_id):
        raise ConflictError(detail="Only a failed delivery can be retried.")
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="notification.delivery.retry",
        entity_type="notification_delivery",
        entity_id=delivery_id,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    db.info.setdefault("pending_dispatch", []).append(delivery_id)
    await db.commit()
    notification_service.dispatch_pending(db)
    d = await db.get(NotificationDelivery, delivery_id, populate_existing=True)
    assert d is not None
    return DeliveryOut.model_validate(d)


# ------------------------------------------------------------------------------------- ITSM


class ItsmIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: str
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr
    enabled: bool = True
    auto_create: bool = False
    min_confidence: str | None = None
    site_id: uuid.UUID | None = None

    @field_validator("min_confidence")
    @classmethod
    def _conf(cls, v: str | None) -> str | None:
        if v is not None and v not in CONFIDENCES:
            raise ValueError(f"min_confidence must be one of {CONFIDENCES}.")
        return v


class ItsmUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr | None = None
    enabled: bool | None = None
    auto_create: bool | None = None
    min_confidence: str | None = None


class ItsmOut(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    base_url: str
    username: str
    has_password: bool = True
    enabled: bool
    auto_create: bool
    min_confidence: str | None
    site_id: uuid.UUID | None
    version: int

    model_config = {"from_attributes": True}


@router.post("/itsm-connections", response_model=ItsmOut, status_code=201)
async def create_itsm_connection(
    body: ItsmIn, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:manage"))
) -> ItsmOut:
    url = _check_url(body.base_url, allow_path=False).rstrip("/")
    await _unique_name(db, ItsmConnection, body.name)
    if body.site_id is not None and await db.get(Site, body.site_id) is None:
        raise NotFoundError(f"Site {body.site_id} not found.")
    c = ItsmConnection(
        id=uuid.uuid4(),
        name=body.name,
        provider="servicenow",
        base_url=url,
        username=body.username,
        secret_ciphertext=encrypt_secret(body.password.get_secret_value()),
        enabled=body.enabled,
        auto_create=body.auto_create,
        min_confidence=body.min_confidence,
        site_id=body.site_id,
        created_by=ctx.user.id,
        version=1,
    )
    db.add(c)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="itsm.connection.create",
        entity_type="itsm_connection",
        entity_id=c.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"name": c.name, "base_url": c.base_url, "username": c.username, "auto_create": c.auto_create},
    )
    await db.commit()
    return ItsmOut.model_validate(c)


@router.get("/itsm-connections", response_model=list[ItsmOut])
async def list_itsm_connections(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> list[ItsmOut]:
    return [ItsmOut.model_validate(c) for c in (await db.execute(select(ItsmConnection).order_by(ItsmConnection.name))).scalars()]


@router.patch("/itsm-connections/{connection_id}", response_model=ItsmOut)
async def update_itsm_connection(
    connection_id: uuid.UUID,
    body: ItsmUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
    version: int = Depends(require_if_match),
) -> ItsmOut:
    if await db.get(ItsmConnection, connection_id) is None:
        raise NotFoundError(f"ITSM connection {connection_id} not found.")
    c = await lock_versioned_row(db, ItsmConnection, connection_id, expected_version=version, label="ITSM connection")
    changed: dict = {}
    if body.name is not None and body.name != c.name:
        await _unique_name(db, ItsmConnection, body.name, c.id)
        c.name = changed["name"] = body.name
    if body.username is not None:
        c.username = changed["username"] = body.username
    if body.password is not None:
        c.secret_ciphertext = encrypt_secret(body.password.get_secret_value())
        changed["password"] = "rotated"
    if body.enabled is not None:
        c.enabled = changed["enabled"] = body.enabled
    if body.auto_create is not None:
        c.auto_create = changed["auto_create"] = body.auto_create
    if body.min_confidence is not None:
        if body.min_confidence not in CONFIDENCES:
            raise _bad(f"min_confidence must be one of {CONFIDENCES}.")
        c.min_confidence = changed["min_confidence"] = body.min_confidence
    c.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="itsm.connection.update",
        entity_type="itsm_connection",
        entity_id=c.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after=changed,
    )
    await db.commit()
    return ItsmOut.model_validate(c)


@router.get("/itsm-tickets", response_model=Page[TicketOut])
async def list_tickets(
    incident_id: uuid.UUID | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("integration:read")),
) -> Page:
    stmt = select(ItsmTicket)
    if incident_id:
        stmt = stmt.where(ItsmTicket.incident_id == incident_id)
    if status:
        stmt = stmt.where(ItsmTicket.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(ItsmTicket.created_at.desc(), ItsmTicket.id).offset(pagination.offset).limit(pagination.limit)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=[await _ticket_out(db, t) for t in rows], total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/itsm-tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("integration:read"))
) -> TicketOut:
    t = await db.get(ItsmTicket, ticket_id)
    if t is None:
        raise NotFoundError(f"Ticket {ticket_id} not found.")
    return await _ticket_out(db, t)


@router.post("/itsm-tickets/{ticket_id}/retry", response_model=TicketOut)
async def retry_ticket(
    ticket_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
) -> TicketOut:
    if await db.get(ItsmTicket, ticket_id) is None:
        raise NotFoundError(f"Ticket {ticket_id} not found.")
    if not await itsm_service.requeue_failed_ticket(db, ticket_id):
        raise ConflictError(detail="Only a failed ticket can be retried.")
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="itsm.ticket.retry",
        entity_type="itsm_ticket",
        entity_id=ticket_id,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    itsm_service.dispatch_pending_tickets(db)
    t = await db.get(ItsmTicket, ticket_id, populate_existing=True)
    assert t is not None
    return await _ticket_out(db, t)
