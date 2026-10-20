"""Physical cable lifecycle and its relation to logical port connections.

Rules:

* Every mutation takes the topology lock, so "a port has at most one live cable" and "a port
  has at most one logical connection" are checked race-free (the partial unique indexes are
  the backstop).
* A cable realizes exactly one `PortConnection`. An existing connection between the same
  two ports is linked; a connection to anything else is a 409; if the ports are free a
  connection is created and remembered (`port_connection_created`) so removal can undo
  exactly what the cable made and nothing else.
* Discovered adjacencies never become cables automatically. `create_cable_from_neighbor`
  accepts only a neighbor an operator has confirmed, and the resulting cable is marked
  `source="discovery_confirmed"` with the neighbor as provenance.
* Endpoints are fixed at creation (a database trigger forbids re-termination); changing
  where a cable goes means removing it and recording a new one.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, ensure_equipment_access
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match
from app.application.network.neighbor_reconciliation import authoritative_links
from app.application.network.neighbor_service import effective_status
from app.application.network.topology_lock import acquire_topology_lock
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.integration.models import Integration
from app.domain.network.cable_models import CABLE_MEDIA_COMPATIBILITY, Cable, CableEndpoint
from app.domain.network.discovery_models import DiscoveredNeighbor
from app.domain.physical.ports import EquipmentPort, PortConnection


def _invalid(detail: str) -> ApiError:
    return ApiError(status_code=422, title="Invalid Cable", detail=detail, type_="https://dcim.internal/errors/validation")


def snapshot(cable: Cable) -> dict:
    return {
        "label": cable.label, "cable_type": cable.cable_type, "status": cable.status, "source": cable.source,
        "version": cable.version, "port_connection_id": str(cable.port_connection_id) if cable.port_connection_id else None,
    }


async def get_cable(db: AsyncSession, cable_id: uuid.UUID, *, for_update: bool = False) -> Cable:
    stmt = select(Cable).where(Cable.id == cable_id).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    cable = (await db.execute(stmt)).scalar_one_or_none()
    if cable is None:
        raise NotFoundError(f"Cable {cable_id} not found.")
    return cable


async def cable_endpoints(db: AsyncSession, cable_id: uuid.UUID) -> dict[str, CableEndpoint]:
    rows = (await db.execute(select(CableEndpoint).where(CableEndpoint.cable_id == cable_id))).scalars().all()
    return {row.end_label: row for row in rows}


async def ensure_port_visible(db: AsyncSession, scope: AccessScope, port: EquipmentPort, *, what: str, ref: uuid.UUID) -> None:
    """404 naming only what the caller already supplied (`what ref`), never the hidden equipment's id."""
    try:
        await ensure_equipment_access(db, scope, port.equipment_id)
    except NotFoundError:
        raise NotFoundError(f"{what} {ref} not found.") from None


async def _ensure_visible(db: AsyncSession, scope: AccessScope, cable_id: uuid.UUID) -> dict[str, CableEndpoint]:
    """Mutations need *both* endpoints visible; anything else is a 404, never a hint."""
    endpoints = await cable_endpoints(db, cable_id)
    for endpoint in endpoints.values():
        port = await db.get(EquipmentPort, endpoint.equipment_port_id)
        assert port is not None
        await ensure_port_visible(db, scope, port, what="Cable", ref=cable_id)
    return endpoints


def _check_media(cable_type: str, ports: list[EquipmentPort]) -> None:
    allowed = CABLE_MEDIA_COMPATIBILITY.get(cable_type)
    if allowed is None:
        return
    for port in ports:
        if port.media_type not in allowed:
            raise _invalid(f"a {cable_type} cable cannot terminate on the {port.media_type} port '{port.display_name}'.")


async def _plan_port_connection(db: AsyncSession, port_a: EquipmentPort, port_b: EquipmentPort) -> PortConnection | None:
    """The logical connection this cable will link, or None if it must create one. Raises
    (before anything is written) if either port already has a connection to something else."""
    rows = (
        await db.execute(
            select(PortConnection).where(
                or_(
                    PortConnection.source_port_id.in_([port_a.id, port_b.id]),
                    PortConnection.target_port_id.in_([port_a.id, port_b.id]),
                )
            )
        )
    ).scalars().all()
    same_pair = [pc for pc in rows if {pc.source_port_id, pc.target_port_id} == {port_a.id, port_b.id}]
    if len(rows) > len(same_pair):
        raise ConflictError(
            "A port already has a logical connection to something else; disconnect it first. "
            "Existing connections are never overwritten by a cable."
        )
    return same_pair[0] if same_pair else None


async def _realize_port_connection(
    db: AsyncSession, cable: Cable, port_a: EquipmentPort, port_b: EquipmentPort, existing: PortConnection | None
) -> None:
    if existing is not None:
        cable.port_connection_created = False
        connection = existing
    else:
        connection = PortConnection(
            id=uuid.uuid4(), source_port_id=port_a.id, target_port_id=port_b.id, cable_id=cable.label[:64],
            status="active" if cable.status == "installed" else "planned",
        )
        db.add(connection)
        cable.port_connection_created = True
    await db.flush()
    cable.port_connection_id = connection.id


async def create_cable(
    db: AsyncSession, *, scope: AccessScope, label: str, cable_type: str, connector_a: str | None, connector_b: str | None,
    length_m: Decimal | None, route_metadata: dict, notes: str | None, port_a_id: uuid.UUID, port_b_id: uuid.UUID,
    status: str, installed_at: datetime | None, source: str, source_neighbor_id: uuid.UUID | None, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> Cable:
    if status not in ("planned", "installed"):
        raise _invalid("a new cable is either planned or installed.")
    if port_a_id == port_b_id:
        raise _invalid("a cable needs two different ports.")
    label = label.strip()
    await acquire_topology_lock(db)
    ports: list[EquipmentPort] = []
    for port_id in (port_a_id, port_b_id):
        port = await db.get(EquipmentPort, port_id)
        if port is None:
            raise NotFoundError(f"EquipmentPort {port_id} not found.")
        await ensure_port_visible(db, scope, port, what="EquipmentPort", ref=port_id)
        ports.append(port)
    port_a, port_b = ports
    _check_media(cable_type, ports)

    busy = (
        await db.execute(
            select(Cable.label)
            .join(CableEndpoint, CableEndpoint.cable_id == Cable.id)
            .where(Cable.is_live.is_(True), CableEndpoint.equipment_port_id.in_([port_a_id, port_b_id]))
        )
    ).first()
    if busy is not None:
        raise ConflictError(f"A port already has a live cable ({busy[0]}); remove it before recording another.")
    if (await db.execute(select(Cable.id).where(Cable.is_live.is_(True), func.lower(Cable.label) == label.lower()))).first():
        raise ConflictError(f"A live cable labelled '{label}' already exists.")
    pair = sorted([str(port_a_id), str(port_b_id)])
    for link in await authoritative_links(db, [port_a_id, port_b_id], exclude_neighbor_id=source_neighbor_id):
        if link["kind"] == "confirmed_neighbor" and link["ports"] != pair:
            raise ConflictError(
                "A confirmed discovery adjacency says one of these ports connects elsewhere; revoke that neighbor first."
            )

    existing_connection = await _plan_port_connection(db, port_a, port_b)  # may refuse; nothing written yet
    now = datetime.now(UTC)
    cable = Cable(
        id=uuid.uuid4(), label=label, cable_type=cable_type, connector_a=connector_a, connector_b=connector_b,
        length_m=length_m, route_metadata=route_metadata, status=status,
        installed_at=(installed_at or now) if status == "installed" else None, source=source,
        source_neighbor_id=source_neighbor_id, notes=notes, created_by_user_id=actor_user_id, version=1,
    )
    db.add(cable)
    await db.flush()
    await db.refresh(cable)  # the generated is_live column
    db.add_all([
        CableEndpoint(id=uuid.uuid4(), cable_id=cable.id, is_live=cable.is_live, end_label="A", equipment_port_id=port_a_id),
        CableEndpoint(id=uuid.uuid4(), cable_id=cable.id, is_live=cable.is_live, end_label="B", equipment_port_id=port_b_id),
    ])
    await db.flush()
    await _realize_port_connection(db, cable, port_a, port_b, existing_connection)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="cable.create", entity_type="cable", entity_id=cable.id,
        request_id=request_id, correlation_id=correlation_id,
        after=snapshot(cable) | {"endpoint_a": str(port_a_id), "endpoint_b": str(port_b_id)},
    )
    await write_outbox_event(
        db, event_type="CableRecorded", aggregate_type="cable", aggregate_id=cable.id,
        payload={"label": cable.label, "status": cable.status}, correlation_id=correlation_id, causation_id=request_id,
    )
    await db.refresh(cable)
    return cable


async def create_cable_from_neighbor(
    db: AsyncSession, *, scope: AccessScope, neighbor_id: uuid.UUID, label: str, cable_type: str, connector_a: str | None,
    connector_b: str | None, length_m: Decimal | None, route_metadata: dict, notes: str | None, status: str,
    installed_at: datetime | None, actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> Cable:
    """The explicit operator action that turns a *confirmed* adjacency into a physical cable."""
    await acquire_topology_lock(db)
    neighbor = (
        await db.execute(
            select(DiscoveredNeighbor).where(DiscoveredNeighbor.id == neighbor_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if neighbor is None:
        raise NotFoundError(f"DiscoveredNeighbor {neighbor_id} not found.")
    if neighbor.reconciliation_state != "confirmed" or neighbor.local_port_id is None or neighbor.remote_port_id is None:
        raise ConflictError("Only an operator-confirmed neighbor can be turned into a cable.")
    integration = await db.get(Integration, neighbor.integration_id)
    if effective_status(neighbor, integration.poll_interval_seconds if integration else None) != "active":
        raise ConflictError("This adjacency is stale (not seen recently); re-observe it or record the cable manually.")
    if (await db.execute(select(Cable.id).where(Cable.source_neighbor_id == neighbor_id, Cable.is_live.is_(True)))).first():
        raise ConflictError("A live cable already exists for this neighbor.")
    return await create_cable(
        db, scope=scope, label=label, cable_type=cable_type, connector_a=connector_a, connector_b=connector_b,
        length_m=length_m, route_metadata=route_metadata, notes=notes, port_a_id=neighbor.local_port_id,
        port_b_id=neighbor.remote_port_id, status=status, installed_at=installed_at, source="discovery_confirmed",
        source_neighbor_id=neighbor.id, actor_user_id=actor_user_id, request_id=request_id, correlation_id=correlation_id,
    )


async def update_cable(
    db: AsyncSession, *, scope: AccessScope, cable_id: uuid.UUID, changes: dict, expected_version: int,
    actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> Cable:
    await acquire_topology_lock(db)
    cable = await get_cable(db, cable_id, for_update=True)
    endpoints = await _ensure_visible(db, scope, cable_id)
    check_version_match(expected=expected_version, actual=cable.version)
    if cable.status == "removed":
        raise ConflictError("A removed cable is immutable history.")
    before = snapshot(cable)
    if "label" in changes and changes["label"].strip().lower() != cable.label.lower():
        label = changes["label"].strip()
        clash = select(Cable.id).where(Cable.is_live.is_(True), func.lower(Cable.label) == label.lower(), Cable.id != cable.id)
        if (await db.execute(clash)).first():
            raise ConflictError(f"A live cable labelled '{label}' already exists.")
        changes = {**changes, "label": label}
    if "cable_type" in changes:
        ports = [await db.get(EquipmentPort, endpoint.equipment_port_id) for endpoint in endpoints.values()]
        _check_media(changes["cable_type"], [p for p in ports if p is not None])
    for name in ("label", "cable_type", "connector_a", "connector_b", "length_m", "route_metadata", "notes"):
        if name in changes:
            setattr(cable, name, changes[name])
    if "label" in changes and cable.port_connection_created and cable.port_connection_id is not None:
        connection = await db.get(PortConnection, cable.port_connection_id)
        if connection is not None:
            connection.cable_id = cable.label[:64]
    cable.version += 1
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="cable.update", entity_type="cable", entity_id=cable.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=snapshot(cable),
    )
    await db.refresh(cable)
    return cable


async def install_cable(
    db: AsyncSession, *, scope: AccessScope, cable_id: uuid.UUID, installed_at: datetime | None, expected_version: int,
    actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> Cable:
    await acquire_topology_lock(db)
    cable = await get_cable(db, cable_id, for_update=True)
    await _ensure_visible(db, scope, cable_id)
    check_version_match(expected=expected_version, actual=cable.version)
    if cable.status != "planned":
        raise ConflictError(f"Only a planned cable can be installed (this one is {cable.status}).")
    before = snapshot(cable)
    cable.status, cable.installed_at = "installed", installed_at or datetime.now(UTC)
    cable.version += 1
    if cable.port_connection_created and cable.port_connection_id is not None:
        connection = await db.get(PortConnection, cable.port_connection_id)
        if connection is not None and connection.status == "planned":
            connection.status = "active"
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="cable.install", entity_type="cable", entity_id=cable.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=snapshot(cable),
    )
    await write_outbox_event(
        db, event_type="CableInstalled", aggregate_type="cable", aggregate_id=cable.id, payload={"label": cable.label},
        correlation_id=correlation_id, causation_id=request_id,
    )
    await db.refresh(cable)
    return cable


async def _release_connection(db: AsyncSession, cable: Cable) -> str:
    """Undo the logical connection this cable created, and only that one.

    The row is deleted only while it still joins exactly the cable's two endpoint ports. If
    something else retargeted it in the meantime it now belongs to someone else's topology, so
    it is kept and only unlinked. Returns "none", "released" or "kept_diverged" for the audit row.
    """
    if cable.port_connection_id is None or not cable.port_connection_created:
        return "none"
    connection = await db.get(PortConnection, cable.port_connection_id)
    cable.port_connection_id = None
    await db.flush()
    if connection is None:
        return "none"
    endpoint_ports = set(
        (await db.execute(select(CableEndpoint.equipment_port_id).where(CableEndpoint.cable_id == cable.id))).scalars().all()
    )
    still_the_cables_link = (
        connection.target_power_node_id is None and connection.target_port_id is not None
        and {connection.source_port_id, connection.target_port_id} == endpoint_ports and len(endpoint_ports) == 2
    )
    if not still_the_cables_link:
        return "kept_diverged"
    await db.delete(connection)
    return "released"


async def remove_cable(
    db: AsyncSession, *, scope: AccessScope, cable_id: uuid.UUID, removed_at: datetime | None, reason: str | None,
    expected_version: int, actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> Cable:
    """Retire a cable (keeps it as history). Undoes only a logical connection this cable created."""
    await acquire_topology_lock(db)
    cable = await get_cable(db, cable_id, for_update=True)
    await _ensure_visible(db, scope, cable_id)
    check_version_match(expected=expected_version, actual=cable.version)
    if cable.status == "removed":
        raise ConflictError("The cable is already removed.")
    when = removed_at or datetime.now(UTC)
    if cable.installed_at is not None and when < cable.installed_at:
        raise _invalid("removed_at cannot precede installed_at.")
    before = snapshot(cable)
    released = await _release_connection(db, cable)
    cable.status, cable.removed_at = "removed", when
    cable.version += 1
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="cable.remove", entity_type="cable", entity_id=cable.id,
        request_id=request_id, correlation_id=correlation_id, before=before,
        after=snapshot(cable) | {"port_connection": released}, reason=reason,
    )
    await write_outbox_event(
        db, event_type="CableRemoved", aggregate_type="cable", aggregate_id=cable.id, payload={"label": cable.label},
        correlation_id=correlation_id, causation_id=request_id,
    )
    await db.refresh(cable)
    return cable


async def delete_cable(
    db: AsyncSession, *, scope: AccessScope, cable_id: uuid.UUID, expected_version: int, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> None:
    """Hard delete, allowed only for a cable that was never installed. Installed cables are
    retired with `remove_cable` so the physical history survives."""
    await acquire_topology_lock(db)
    cable = await get_cable(db, cable_id, for_update=True)
    await _ensure_visible(db, scope, cable_id)
    check_version_match(expected=expected_version, actual=cable.version)
    if cable.status != "planned":
        raise ConflictError("Only a planned cable can be deleted; retire an installed cable instead.")
    before = snapshot(cable)
    released = await _release_connection(db, cable)
    await db.delete(cable)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="cable.delete", entity_type="cable", entity_id=cable_id,
        request_id=request_id, correlation_id=correlation_id, before=before, after={"port_connection": released},
    )
