"""Collector identity, capability, assignment, and health (ARCHITECTURE_REVIEW.md §18).
Reuses existing cross-cutting infrastructure throughout, per the master prompt's
explicit "before adding authentication/authorization/idempotency/audit/outbox/retry/
observability, search the repository and reuse the existing implementation":
- `app.application.audit_service.write_audit_log` for every state-changing action.
- `app.application.outbox_service.write_outbox_event` for lifecycle events
  (`CollectorRegistered`, `IntegrationEnabled`, `IntegrationDisabled`,
  `DeviceDiscovered`, `ReconciliationRequired`) -- the existing transactional Outbox,
  never a second event bus.
- `app.application.collector_auth` for the collector's own machine credential.

Health is deliberately never a stored column (master prompt §13: "Never collapse
device connectivity, collector health, and integration health into one status") --
`classify_collector_health` derives it from the most recent `CollectorHeartbeat.ts`
every time it's asked, so there is exactly one place this logic can be wrong, and no
stale cached status can ever exist."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.collector_auth import generate_collector_secret
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.core.secrets import encrypt_secret
from app.domain.integration.models import (
    Collector,
    CollectorAssignment,
    CollectorCapability,
    CollectorHeartbeat,
    Integration,
)

# OPEN DECISION (PHASE8_EDGE_COLLECTOR_CONTRACT.md): the exact heartbeat timeout is not
# yet an operations-approved production value -- these are reasoned defaults for this
# phase's foundation, made explicit and named rather than buried as a magic number, so
# a later decision can change them without a code archaeology exercise.
HEARTBEAT_STALE_AFTER_SECONDS = 90
HEARTBEAT_OFFLINE_AFTER_SECONDS = 300


@dataclass
class CollectorHealth:
    state: str  # "healthy" | "stale" | "offline"
    last_heartbeat_at: datetime | None
    seconds_since_heartbeat: float | None


def _classify_from_latest_ts(latest_ts: datetime | None) -> CollectorHealth:
    if latest_ts is None:
        return CollectorHealth(state="offline", last_heartbeat_at=None, seconds_since_heartbeat=None)
    age_seconds = (datetime.now(UTC) - latest_ts).total_seconds()
    if age_seconds <= HEARTBEAT_STALE_AFTER_SECONDS:
        state = "healthy"
    elif age_seconds <= HEARTBEAT_OFFLINE_AFTER_SECONDS:
        state = "stale"
    else:
        state = "offline"
    return CollectorHealth(state=state, last_heartbeat_at=latest_ts, seconds_since_heartbeat=age_seconds)


async def classify_collector_health(db: AsyncSession, collector_id: uuid.UUID) -> CollectorHealth:
    stmt = (
        select(CollectorHeartbeat.ts)
        .where(CollectorHeartbeat.collector_id == collector_id)
        .order_by(CollectorHeartbeat.ts.desc())
        .limit(1)
    )
    latest_ts = (await db.execute(stmt)).scalar_one_or_none()
    return _classify_from_latest_ts(latest_ts)


async def classify_collector_health_bulk(db: AsyncSession, collector_ids: list[uuid.UUID]) -> dict[uuid.UUID, CollectorHealth]:
    """Batch equivalent of `classify_collector_health` -- ONE query for every collector
    in `collector_ids`, never one query per collector in a loop (the exact N+1 shape
    Phase 3's own `PowerGraphSnapshot` batch-loading correction exists to prevent;
    PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding N1). Uses PostgreSQL's
    `DISTINCT ON` to fetch only the single latest heartbeat row per collector, not
    every heartbeat row ever recorded."""
    if not collector_ids:
        return {}
    stmt = (
        select(CollectorHeartbeat.collector_id, CollectorHeartbeat.ts)
        .where(CollectorHeartbeat.collector_id.in_(collector_ids))
        .distinct(CollectorHeartbeat.collector_id)
        .order_by(CollectorHeartbeat.collector_id, CollectorHeartbeat.ts.desc())
    )
    rows = (await db.execute(stmt)).all()
    latest_by_collector = {row.collector_id: row.ts for row in rows}
    return {cid: _classify_from_latest_ts(latest_by_collector.get(cid)) for cid in collector_ids}


async def register_collector(
    db: AsyncSession,
    *,
    name: str,
    collector_type: str,
    site_id: uuid.UUID | None,
    version_string: str | None,
    actor_user_id: uuid.UUID,
    request_id: str | None,
    correlation_id: str | None,
) -> tuple[Collector, str]:
    """Returns (collector, plaintext_secret) -- the plaintext secret is returned
    exactly once and never retrievable again (matching the same "show it once" UX every
    API-key-issuing system uses); only its Fernet ciphertext is persisted."""
    if collector_type == "central" and site_id is not None:
        raise ApiError(status_code=422, title="Invalid Collector", detail="A central collector must not have a site_id.")
    if collector_type == "edge" and site_id is None:
        raise ApiError(status_code=422, title="Invalid Collector", detail="An edge collector must have a site_id.")

    plaintext_secret = generate_collector_secret()
    collector = Collector(
        id=uuid.uuid4(),
        name=name,
        collector_type=collector_type,
        site_id=site_id,
        status="active",
        version_string=version_string,
        secret_ciphertext=encrypt_secret(plaintext_secret),
        secret_rotated_at=datetime.now(UTC),
    )
    db.add(collector)
    await db.flush()

    await write_outbox_event(
        db, event_type="CollectorRegistered", aggregate_type="collector", aggregate_id=collector.id,
        payload={"name": name, "collector_type": collector_type, "site_id": str(site_id) if site_id else None},
        correlation_id=correlation_id, causation_id=request_id,
    )
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="collector.register", entity_type="collector", entity_id=collector.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"name": name, "collector_type": collector_type, "site_id": str(site_id) if site_id else None},
    )
    return collector, plaintext_secret


async def record_heartbeat(
    db: AsyncSession, *, collector: Collector, queue_depth: int | None, cpu_pct: float | None, mem_pct: float | None, status: str
) -> CollectorHeartbeat:
    hb = CollectorHeartbeat(
        id=uuid.uuid4(), collector_id=collector.id, ts=datetime.now(UTC),
        queue_depth=queue_depth, cpu_pct=cpu_pct, mem_pct=mem_pct, status=status,
    )
    db.add(hb)
    await write_outbox_event(
        db, event_type="CollectorHeartbeatReceived", aggregate_type="collector", aggregate_id=collector.id,
        payload={"queue_depth": queue_depth, "status": status},
        correlation_id=None, causation_id=None,
    )
    await db.commit()
    return hb


async def declare_capabilities(
    db: AsyncSession, *, collector_id: uuid.UUID, protocol_codes: list[str],
    actor_user_id: uuid.UUID | None = None, request_id: str | None = None, correlation_id: str | None = None,
) -> list[CollectorCapability]:
    """Idempotent: re-declaring the same set of capabilities is a no-op for codes
    already present, never a duplicate row (the unique constraint would reject a raw
    re-insert; this checks first so re-declaration is a normal, expected operation, not
    an error a caller must special-case)."""
    existing = {
        row.protocol_code
        for row in (
            await db.execute(select(CollectorCapability).where(CollectorCapability.collector_id == collector_id))
        ).scalars().all()
    }
    created = []
    for code in protocol_codes:
        if code in existing:
            continue
        cap = CollectorCapability(id=uuid.uuid4(), collector_id=collector_id, protocol_code=code)
        db.add(cap)
        created.append(cap)
    await db.flush()
    if created:
        await write_audit_log(
            db, actor_user_id=actor_user_id, action="collector.declare_capabilities", entity_type="collector",
            entity_id=collector_id, request_id=request_id, correlation_id=correlation_id,
            after={"protocol_codes": [c.protocol_code for c in created]},
        )
    return created


async def assign_integration(
    db: AsyncSession, *, integration_id: uuid.UUID, collector_id: uuid.UUID, actor_user_id: uuid.UUID,
    request_id: str | None = None, correlation_id: str | None = None,
) -> CollectorAssignment:
    """Reassignment = close the current open row (if any) and open a new one in the
    same transaction -- mirrors `RackPlacement`'s own move operation exactly (never an
    in-place UPDATE of a `collector_id` column, which would destroy assignment
    history). Validates the target collector actually declares the integration's
    required protocol capability -- an integration must never be silently assigned to
    a collector that cannot actually run it (master prompt §4: "Capabilities must be
    declarative rather than hardcoded... Avoid ambiguous ownership").

    Also validates site association (PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding
    N2): an edge collector scoped to one site must never be assigned an integration
    explicitly scoped to a different site -- an edge collector's whole reason for
    existing is that it can reach devices on its own site's local network, so a
    cross-site assignment would either silently never work or (worse) misattribute
    which site a discovered device actually belongs to. A `None` site on either side
    (a central collector, or a site-agnostic integration reachable over the public
    internet) is not a mismatch -- only two explicit, different site_ids are rejected."""
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError(f"Integration {integration_id} not found.")
    collector = await db.get(Collector, collector_id)
    if collector is None:
        raise NotFoundError(f"Collector {collector_id} not found.")
    if collector.status != "active":
        raise ApiError(status_code=409, title="Collector Not Active", detail="Cannot assign to a non-active collector.")
    if collector.site_id is not None and integration.site_id is not None and collector.site_id != integration.site_id:
        raise ApiError(
            status_code=422, title="Site Mismatch",
            detail=f"Collector {collector_id} is scoped to a different site than integration {integration_id}.",
        )

    has_capability = (
        await db.execute(
            select(CollectorCapability).where(
                CollectorCapability.collector_id == collector_id,
                CollectorCapability.protocol_code == integration.integration_type,
            )
        )
    ).scalar_one_or_none()
    if has_capability is None:
        raise ApiError(
            status_code=422, title="Missing Capability",
            detail=f"Collector {collector_id} does not declare the '{integration.integration_type}' capability.",
        )

    now = datetime.now(UTC)
    current = (
        await db.execute(
            select(CollectorAssignment).where(
                CollectorAssignment.integration_id == integration_id, CollectorAssignment.effective_to.is_(None)
            )
        )
    ).scalar_one_or_none()
    if current is not None:
        if current.collector_id == collector_id:
            return current  # already assigned to this exact collector -- no-op
        current.effective_to = now

    assignment = CollectorAssignment(
        id=uuid.uuid4(), collector_id=collector_id, integration_id=integration_id, effective_from=now, effective_to=None,
    )
    db.add(assignment)
    try:
        await db.flush()
    except Exception as exc:
        # The partial-unique-index race (two concurrent reassignments of the SAME
        # integration) surfaces here as an IntegrityError -- translated to a clean 409
        # by the app-wide IntegrityError handler (app/core/errors.py), never a 500.
        raise ConflictError("This integration was reassigned concurrently by another request.") from exc
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="collector.assign_integration", entity_type="collector_assignment",
        entity_id=assignment.id, request_id=request_id, correlation_id=correlation_id,
        before={"previous_collector_id": str(current.collector_id) if current is not None else None},
        after={"collector_id": str(collector_id), "integration_id": str(integration_id)},
    )
    await db.commit()
    return assignment


async def current_assignment(db: AsyncSession, integration_id: uuid.UUID) -> CollectorAssignment | None:
    return (
        await db.execute(
            select(CollectorAssignment).where(
                CollectorAssignment.integration_id == integration_id, CollectorAssignment.effective_to.is_(None)
            )
        )
    ).scalar_one_or_none()


async def current_assignments_bulk(db: AsyncSession, integration_ids: list[uuid.UUID]) -> dict[uuid.UUID, CollectorAssignment]:
    """Batch equivalent of `current_assignment` -- ONE query for every integration in
    `integration_ids`, never one query per integration in a loop (the same N+1 shape
    `classify_collector_health_bulk` closes for collectors; PHASE8_IMPLEMENTATION_
    RED_TEAM_SCOPE.md finding N1). The partial unique index on
    `(integration_id) WHERE effective_to IS NULL` guarantees at most one row per
    integration, so no further reduction is needed after the single query."""
    if not integration_ids:
        return {}
    rows = (
        await db.execute(
            select(CollectorAssignment).where(
                CollectorAssignment.integration_id.in_(integration_ids), CollectorAssignment.effective_to.is_(None)
            )
        )
    ).scalars().all()
    return {row.integration_id: row for row in rows}


@dataclass
class PollOutcome:
    integration_id: uuid.UUID
    succeeded: bool
    error: str | None = None
    external_identifier: str | None = None


async def run_polling_cycle(db: AsyncSession, *, collector_id: uuid.UUID) -> list[PollOutcome]:
    """§14 of the master prompt ("Failure Isolation"): iterates every integration
    currently assigned to `collector_id` and polls each with its own driver, in its own
    try/except -- one integration's `DriverConnectionError` (or any other exception a
    misbehaving driver raises) is caught, recorded on THAT integration's own failure
    counters, and does not prevent the remaining integrations in this cycle from being
    polled. This is the one place `app.application.drivers.get_driver_class` is called
    -- never an `if integration_type == ...` branch anywhere else in the codebase.

    Deliberately does NOT write telemetry/readings anywhere (master prompt §17: "Do Not
    Build Telemetry Yet") -- a successful poll's `AcquisitionResult` is only used to
    call `discovery_service.ingest_discovery`, recording that *something* was observed,
    never a metric value history."""
    from app.application.discovery_service import ingest_discovery
    from app.application.drivers import get_driver_class
    from app.application.drivers.base import DriverConnectionError
    from app.core.secrets import decrypt_secret

    assignments = (
        await db.execute(
            select(CollectorAssignment).where(
                CollectorAssignment.collector_id == collector_id, CollectorAssignment.effective_to.is_(None)
            )
        )
    ).scalars().all()
    # One batch query for every assigned integration, not one `db.get` per assignment
    # in the loop below -- the same N+1 shape `classify_collector_health_bulk`/
    # `current_assignments_bulk` close elsewhere in this module.
    integrations_by_id = {
        row.id: row
        for row in (
            await db.execute(select(Integration).where(Integration.id.in_([a.integration_id for a in assignments])))
        ).scalars().all()
    }

    outcomes: list[PollOutcome] = []
    for assignment in assignments:
        integration = integrations_by_id.get(assignment.integration_id)
        if integration is None or not integration.enabled:
            continue

        integration.last_poll_at = datetime.now(UTC)
        try:
            driver_class = get_driver_class(integration.integration_type)
            driver = driver_class(integration.id)
            credential = decrypt_secret(integration.credential_ciphertext) if integration.credential_ciphertext else None
            await driver.connect(
                target_host=integration.target_host, target_port=integration.target_port,
                config=integration.config, credential=credential,
            )
            try:
                result = await driver.poll()
            finally:
                await driver.disconnect()

            integration.last_success_at = datetime.now(UTC)
            integration.consecutive_failures = 0
            await ingest_discovery(
                db, integration_id=integration.id, external_identifier=result.external_identifier,
                raw_attributes=result.raw_attributes, correlation_id=None, causation_id=None,
            )
            outcomes.append(
                PollOutcome(integration_id=integration.id, succeeded=True, external_identifier=result.external_identifier)
            )
        except DriverConnectionError as exc:
            integration.last_failure_at = datetime.now(UTC)
            integration.consecutive_failures += 1
            outcomes.append(PollOutcome(integration_id=integration.id, succeeded=False, error=exc.reason))
        except Exception as exc:  # noqa: BLE001 -- deliberate: a misbehaving/buggy
            # driver must never take down the rest of this collector's polling cycle;
            # this is the one intentional broad catch in the whole Phase 8 codebase,
            # and it only ever affects the ONE integration whose driver raised, never
            # anything wider. Logged, not silently swallowed.
            integration.last_failure_at = datetime.now(UTC)
            integration.consecutive_failures += 1
            outcomes.append(PollOutcome(integration_id=integration.id, succeeded=False, error=f"Unexpected driver error: {exc}"))

    await db.commit()
    return outcomes
