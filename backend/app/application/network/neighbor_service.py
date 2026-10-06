"""Neighbor evidence ingestion and operator reconciliation.

Ingestion (`ingest_neighbor`) is an idempotent upsert keyed on the evidence identity: a
neighbor seen on every poll updates `last_seen_at` and nothing else grows. It is safe under
concurrent and out-of-order delivery because the upsert is a single `INSERT .. ON CONFLICT`
whose update only applies to observations newer than the stored one. It writes only
`discovered_neighbor`.

Operator decisions (`confirm_neighbor`, `reject_neighbor`, `revoke_neighbor`) are the only
way `local_port_id`/`remote_port_id` are ever set or cleared. They serialize on the
topology lock, re-check authoritative conflicts inside the lock, are audited and emit an
outbox event. Re-observation never alters a confirmed or rejected decision.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match
from app.application.network.neighbor_evidence import NeighborEvidence, parse_neighbor
from app.application.network.neighbor_reconciliation import authoritative_links, evaluate_neighbor, fingerprint
from app.application.network.topology_lock import acquire_topology_lock
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.network.discovery_models import OPEN_RECONCILIATION_STATES, DiscoveredNeighbor
from app.domain.physical.ports import EquipmentPort

MAX_FUTURE_SKEW = timedelta(minutes=5)
DEFAULT_STALE_FLOOR_SECONDS = 15 * 60


def _clamp(moment: datetime) -> datetime:
    """A collector clock far in the future must not pin `last_seen_at` ahead forever."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return min(moment, datetime.now(UTC) + MAX_FUTURE_SKEW)


_EVIDENCE_COLUMNS = (
    "scan_id", "source_collector_id", "local_port_name", "local_port_ref", "remote_chassis_ident", "remote_chassis_subtype",
    "remote_port_ident", "remote_port_subtype", "remote_port_description", "remote_system_name",
    "remote_system_description", "remote_platform", "remote_management_address", "capabilities", "native_vlan",
    "ttl_seconds", "raw_evidence",
)


async def ingest_neighbor(
    db: AsyncSession, *, collector_id: uuid.UUID, integration_id: uuid.UUID, occurred_at: datetime, raw_attributes: dict,
) -> tuple[DiscoveredNeighbor, bool]:
    """Returns `(neighbor, created)`. Raises `InvalidNeighborPayload` for unusable evidence."""
    evidence: NeighborEvidence = parse_neighbor(raw_attributes)
    scan_id = raw_attributes.get("scan_id")
    seen = _clamp(occurred_at)
    values = {
        "id": uuid.uuid4(), "integration_id": integration_id, "protocol": evidence.protocol,
        "identity_key": evidence.identity_key(integration_id), "first_seen_at": seen, "last_seen_at": seen,
        "status": "active", "reconciliation_state": "unmatched", "match_evidence": {}, "version": 1,
        "scan_id": scan_id[:64] if isinstance(scan_id, str) else None, "source_collector_id": collector_id,
        "local_port_name": evidence.local_port_name, "local_port_ref": evidence.local_port_ref,
        "remote_chassis_ident": evidence.remote_chassis_ident, "remote_chassis_subtype": evidence.remote_chassis_subtype,
        "remote_port_ident": evidence.remote_port_ident, "remote_port_subtype": evidence.remote_port_subtype,
        "remote_port_description": evidence.remote_port_description, "remote_system_name": evidence.remote_system_name,
        "remote_system_description": evidence.remote_system_description, "remote_platform": evidence.remote_platform,
        "remote_management_address": evidence.remote_management_address, "capabilities": evidence.capabilities,
        "native_vlan": evidence.native_vlan, "ttl_seconds": evidence.ttl_seconds, "raw_evidence": evidence.raw,
    }
    table = DiscoveredNeighbor.__table__
    insert = pg_insert(table).values(**values)
    newer = insert.excluded.last_seen_at >= table.c.last_seen_at
    update_set = {name: case((newer, insert.excluded[name]), else_=table.c[name]) for name in _EVIDENCE_COLUMNS}
    update_set["last_seen_at"] = func.greatest(table.c.last_seen_at, insert.excluded.last_seen_at)
    update_set["first_seen_at"] = func.least(table.c.first_seen_at, insert.excluded.first_seen_at)
    update_set["status"] = case((newer, "active"), else_=table.c.status)
    update_set["updated_at"] = func.now()
    stmt = (
        insert.on_conflict_do_update(index_elements=[table.c.identity_key], set_=update_set)
        .returning(table.c.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await db.execute(stmt)).one()
    neighbor = (
        await db.execute(
            select(DiscoveredNeighbor).where(DiscoveredNeighbor.id == row.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    created = bool(row.inserted)
    if neighbor.reconciliation_state in OPEN_RECONCILIATION_STATES and (
        created or neighbor.match_evidence.get("fingerprint") != fingerprint(neighbor)
    ):
        await apply_match(db, neighbor)
    return neighbor, created


async def apply_match(db: AsyncSession, neighbor: DiscoveredNeighbor) -> DiscoveredNeighbor:
    """Re-evaluate an open neighbor. Never changes confirmed or rejected neighbors."""
    if neighbor.reconciliation_state not in OPEN_RECONCILIATION_STATES:
        return neighbor
    outcome = await evaluate_neighbor(db, neighbor)
    neighbor.reconciliation_state = outcome.state
    neighbor.match_evidence = outcome.evidence
    await db.flush()
    return neighbor


async def apply_scan_marker(
    db: AsyncSession, *, integration_id: uuid.UUID, protocol: str, scan_started_at: datetime, complete: bool,
) -> int:
    """A completed scan of (integration, protocol) proves that neighbors not re-observed
    since it began are gone. Marks them stale; never deletes and never touches decisions."""
    if not complete or protocol not in ("lldp", "cdp"):
        return 0
    started = _clamp(scan_started_at)
    result = await db.execute(
        update(DiscoveredNeighbor)
        .where(
            DiscoveredNeighbor.integration_id == integration_id, DiscoveredNeighbor.protocol == protocol,
            DiscoveredNeighbor.status == "active", DiscoveredNeighbor.last_seen_at < started,
        )
        .values(status="stale", updated_at=func.now())
    )
    return result.rowcount or 0


def stale_after_seconds(poll_interval_seconds: int | None) -> int:
    return max(DEFAULT_STALE_FLOOR_SECONDS, 3 * (poll_interval_seconds or 0))


def effective_status(neighbor: DiscoveredNeighbor, poll_interval_seconds: int | None, now: datetime | None = None) -> str:
    """`stale` when a completed scan said so, or when nothing has been seen for 3 poll intervals."""
    if neighbor.status == "stale":
        return "stale"
    reference = now or datetime.now(UTC)
    age = (reference - neighbor.last_seen_at).total_seconds()
    return "stale" if age > stale_after_seconds(poll_interval_seconds) else "active"


# --------------------------------------------------------------------- operator actions
async def _locked(db: AsyncSession, neighbor_id: uuid.UUID) -> DiscoveredNeighbor:
    await acquire_topology_lock(db)
    neighbor = (
        await db.execute(
            select(DiscoveredNeighbor).where(DiscoveredNeighbor.id == neighbor_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if neighbor is None:
        raise NotFoundError(f"DiscoveredNeighbor {neighbor_id} not found.")
    return neighbor


def _snapshot(neighbor: DiscoveredNeighbor) -> dict:
    return {
        "state": neighbor.reconciliation_state, "local_port_id": str(neighbor.local_port_id) if neighbor.local_port_id else None,
        "remote_port_id": str(neighbor.remote_port_id) if neighbor.remote_port_id else None, "version": neighbor.version,
    }


async def confirm_neighbor(
    db: AsyncSession, *, neighbor_id: uuid.UUID, local_port_id: uuid.UUID | None, remote_port_id: uuid.UUID | None,
    expected_version: int, reason: str | None, actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> DiscoveredNeighbor:
    """Link the neighbor to two inventory ports. With no ports given, the stored proposal
    is used; an operator may also name ports explicitly to resolve an ambiguous or
    unmatched neighbor (recorded as an override)."""
    neighbor = await _locked(db, neighbor_id)
    check_version_match(expected=expected_version, actual=neighbor.version)
    if neighbor.reconciliation_state == "confirmed":
        raise ConflictError("This neighbor is already confirmed.")
    if neighbor.reconciliation_state == "rejected":
        raise ConflictError("A rejected neighbor must be reopened (revoke) before it can be confirmed.")
    proposal = (neighbor.match_evidence or {}).get("proposal") or {}
    chosen_local = local_port_id or (uuid.UUID(proposal["local_port_id"]) if proposal.get("local_port_id") else None)
    chosen_remote = remote_port_id or (uuid.UUID(proposal["remote_port_id"]) if proposal.get("remote_port_id") else None)
    if chosen_local is None or chosen_remote is None:
        raise ApiError(
            status_code=422, title="Ports Required",
            detail="Both ports are required: this neighbor has no proposal, so name the local and remote port explicitly.",
        )
    if chosen_local == chosen_remote:
        raise ApiError(status_code=422, title="Invalid Ports", detail="A neighbor cannot link a port to itself.")
    for port_id in (chosen_local, chosen_remote):
        if await db.get(EquipmentPort, port_id) is None:
            raise NotFoundError(f"EquipmentPort {port_id} not found.")
    links = await authoritative_links(db, [chosen_local, chosen_remote], exclude_neighbor_id=neighbor.id)
    pair = sorted([str(chosen_local), str(chosen_remote)])
    conflicts = [link for link in links if link["ports"] != pair]
    if conflicts:
        raise ConflictError(
            "A port is already part of a different authoritative link "
            f"({conflicts[0]['kind'].replace('_', ' ')}); resolve that first. Nothing was changed."
        )
    override = (chosen_local, chosen_remote) != (
        uuid.UUID(proposal["local_port_id"]) if proposal.get("local_port_id") else None,
        uuid.UUID(proposal["remote_port_id"]) if proposal.get("remote_port_id") else None,
    )
    before = _snapshot(neighbor)
    neighbor.local_port_id, neighbor.remote_port_id = chosen_local, chosen_remote
    neighbor.reconciliation_state = "confirmed"
    neighbor.decided_by_user_id, neighbor.decided_at, neighbor.decision_reason = actor_user_id, datetime.now(UTC), reason
    neighbor.match_evidence = {**(neighbor.match_evidence or {}), "decision": {"override": override}}
    neighbor.version += 1
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="neighbor.confirm", entity_type="discovered_neighbor", entity_id=neighbor.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=_snapshot(neighbor) | {"override": override},
        reason=reason,
    )
    await write_outbox_event(
        db, event_type="NeighborConfirmed", aggregate_type="discovered_neighbor", aggregate_id=neighbor.id,
        payload={"local_port_id": str(chosen_local), "remote_port_id": str(chosen_remote)}, correlation_id=correlation_id,
        causation_id=request_id,
    )
    await db.refresh(neighbor)
    return neighbor


async def reject_neighbor(
    db: AsyncSession, *, neighbor_id: uuid.UUID, expected_version: int, reason: str | None, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> DiscoveredNeighbor:
    neighbor = await _locked(db, neighbor_id)
    check_version_match(expected=expected_version, actual=neighbor.version)
    if neighbor.reconciliation_state not in OPEN_RECONCILIATION_STATES:
        raise ConflictError(f"A {neighbor.reconciliation_state} neighbor cannot be rejected; revoke it first.")
    before = _snapshot(neighbor)
    neighbor.reconciliation_state = "rejected"
    neighbor.decided_by_user_id, neighbor.decided_at, neighbor.decision_reason = actor_user_id, datetime.now(UTC), reason
    neighbor.version += 1
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="neighbor.reject", entity_type="discovered_neighbor", entity_id=neighbor.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=_snapshot(neighbor), reason=reason,
    )
    await db.refresh(neighbor)
    return neighbor


async def revoke_neighbor(
    db: AsyncSession, *, neighbor_id: uuid.UUID, expected_version: int, reason: str | None, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> DiscoveredNeighbor:
    """Undo a confirmation or rejection. Clears the links, re-evaluates the evidence, and
    leaves any physical cable created from it untouched (cables are retired separately)."""
    neighbor = await _locked(db, neighbor_id)
    check_version_match(expected=expected_version, actual=neighbor.version)
    if neighbor.reconciliation_state not in ("confirmed", "rejected"):
        raise ConflictError("Only a confirmed or rejected neighbor can be revoked.")
    before = _snapshot(neighbor)
    neighbor.local_port_id = neighbor.remote_port_id = None
    neighbor.reconciliation_state = "unmatched"
    neighbor.decided_by_user_id, neighbor.decided_at, neighbor.decision_reason = actor_user_id, datetime.now(UTC), reason
    neighbor.version += 1
    await db.flush()
    await apply_match(db, neighbor)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="neighbor.revoke", entity_type="discovered_neighbor", entity_id=neighbor.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=_snapshot(neighbor), reason=reason,
    )
    await db.refresh(neighbor)
    return neighbor


async def rematch_neighbor(
    db: AsyncSession, *, neighbor_id: uuid.UUID, actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> DiscoveredNeighbor:
    neighbor = await _locked(db, neighbor_id)
    if neighbor.reconciliation_state not in OPEN_RECONCILIATION_STATES:
        raise ConflictError("Only an unresolved neighbor can be re-evaluated.")
    before = _snapshot(neighbor)
    await apply_match(db, neighbor)
    neighbor.version += 1
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="neighbor.rematch", entity_type="discovered_neighbor", entity_id=neighbor.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=_snapshot(neighbor),
    )
    await db.refresh(neighbor)
    return neighbor
