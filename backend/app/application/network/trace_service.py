"""End-to-end port trace: start port -> link -> arrival port -> (pass-through -> next link ->)* -> end.

A *link* is a live physical cable (planned or installed; removed cables are history and are
never followed) or, when a port has no cable, a logical `PortConnection`. A *pass-through*
(`port_pass_through`, e.g. patch-panel front <-> rear) is the authoritative way a signal
continues through a device: at every arrival port the trace looks for a pass-through and, if
there is one, continues from its partner port over that port's own link. Nothing is guessed:
LLDP/CDP evidence is never traversed, and the trace only reads.

Traversal is deterministic and bounded: at most `MAX_HOPS` links, a visited-port set stops
loops, and inconsistent data ends the trace with a stable `terminated` value instead of an
error. Terminations: `end_of_path` (arrived at a port with no pass-through), `no_link` (the
start port, or the far side of the last pass-through, has no link), `restricted` (the next
equipment is outside the caller's scope), `cycle_detected`, `hop_limit`, `broken_topology`.

Authoritative links (cables, then logical port connections) and discovery evidence are
reported in separate sections and never merged: `evidence` is what LLDP/CDP *saw*, and
`agreement` says whether that matches the authoritative link, so an operator can spot a
mispatched or undocumented cable without discovery ever rewriting the record.

Visibility follows the caller's site/rack scope. The start port must be visible (404
otherwise). If the far equipment is not visible, the hop is returned as `restricted` with
no identifiers, so the trace cannot be used to enumerate equipment outside the scope.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, equipment_visible_clause
from app.core.errors import NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.network.cable_models import Cable, CableEndpoint
from app.domain.network.discovery_models import DiscoveredNeighbor
from app.domain.network.pass_through_models import PortPassThrough, PortPassThroughMember
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort, PortConnection

MAX_PREVIOUS_CABLES = 5
MAX_HOPS = 32
MAX_EVIDENCE = 20


async def _port_view(db: AsyncSession, port_id: uuid.UUID) -> dict:
    row = (
        await db.execute(
            select(EquipmentPort, Equipment.hostname, ManagedAsset.asset_tag)
            .join(Equipment, Equipment.id == EquipmentPort.equipment_id)
            .join(ManagedAsset, ManagedAsset.id == Equipment.id)
            .where(EquipmentPort.id == port_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(f"EquipmentPort {port_id} not found.")
    port, hostname, asset_tag = row
    return {
        "port_id": str(port.id), "port_name": port.display_name, "stable_key": port.stable_key, "media_type": port.media_type,
        "equipment_id": str(port.equipment_id), "equipment_hostname": hostname, "equipment_asset_tag": asset_tag,
    }


async def _is_visible(db: AsyncSession, scope: AccessScope, equipment_id: uuid.UUID) -> bool:
    if scope.unrestricted:
        return True
    return (
        await db.execute(select(Equipment.id).where(Equipment.id == equipment_id, equipment_visible_clause(scope, Equipment.id)))
    ).first() is not None


def _cable_view(cable: Cable) -> dict:
    return {
        "id": str(cable.id), "label": cable.label, "cable_type": cable.cable_type, "status": cable.status,
        "length_m": float(cable.length_m) if cable.length_m is not None else None, "source": cable.source,
        "installed_at": cable.installed_at.isoformat() if cable.installed_at else None,
        "removed_at": cable.removed_at.isoformat() if cable.removed_at else None,
    }


async def _evidence(db: AsyncSession, scope: AccessScope, port_ids: list[uuid.UUID]) -> list[dict]:
    """Neighbors that confirm or merely propose a link touching these ports. Proposals are
    read from the stored match explanation; nothing here is authoritative."""
    proposals = [
        DiscoveredNeighbor.match_evidence["proposal"]["local_port_id"].astext.in_([str(p) for p in port_ids]),
        DiscoveredNeighbor.match_evidence["proposal"]["remote_port_id"].astext.in_([str(p) for p in port_ids]),
    ]
    rows = (
        await db.execute(
            select(DiscoveredNeighbor)
            .where(
                or_(DiscoveredNeighbor.local_port_id.in_(port_ids), DiscoveredNeighbor.remote_port_id.in_(port_ids), *proposals),
                DiscoveredNeighbor.reconciliation_state != "rejected",
            )
            .order_by(DiscoveredNeighbor.last_seen_at.desc())
            .limit(MAX_EVIDENCE)
        )
    ).scalars().all()
    out = []
    for n in rows:
        proposal = (n.match_evidence or {}).get("proposal") or {}
        local = n.local_port_id or (uuid.UUID(proposal["local_port_id"]) if proposal.get("local_port_id") else None)
        remote = n.remote_port_id or (uuid.UUID(proposal["remote_port_id"]) if proposal.get("remote_port_id") else None)
        # an adjacency touching equipment the caller cannot see is not shown at all
        hidden = False
        for other in (local, remote):
            if other is not None and other not in port_ids:
                other_port = await db.get(EquipmentPort, other)
                hidden = hidden or other_port is None or not await _is_visible(db, scope, other_port.equipment_id)
        if hidden:
            continue
        out.append({
            "neighbor_id": str(n.id), "protocol": n.protocol, "reconciliation_state": n.reconciliation_state,
            "status": n.status, "authoritative": False, "first_seen_at": n.first_seen_at.isoformat(),
            "last_seen_at": n.last_seen_at.isoformat(), "local_port_id": str(local) if local else None,
            "remote_port_id": str(remote) if remote else None, "remote_system_name": n.remote_system_name,
            "remote_chassis_ident": n.remote_chassis_ident, "remote_port_ident": n.remote_port_ident,
        })
    return out


class _Broken(Exception):
    """Stored topology is inconsistent; the trace ends with `broken_topology`."""


async def _next_link(db: AsyncSession, port_id: uuid.UUID) -> tuple[dict, uuid.UUID] | None:
    """The authoritative link leaving `port_id`: its live cable, else a logical connection."""
    live = (
        await db.execute(
            select(Cable, CableEndpoint)
            .join(CableEndpoint, CableEndpoint.cable_id == Cable.id)
            .where(Cable.is_live.is_(True), CableEndpoint.equipment_port_id == port_id)
            .order_by(Cable.id)
        )
    ).all()
    if len(live) > 1:
        raise _Broken("a port has more than one live cable")
    if live:
        cable, near = live[0]
        ends = (await db.execute(select(CableEndpoint).where(CableEndpoint.cable_id == cable.id))).scalars().all()
        others = [end for end in ends if end.id != near.id]
        if len(ends) != 2 or len(others) != 1:
            raise _Broken("a cable does not have exactly two endpoints")
        far = others[0]
        return (
            {"kind": "cable", "cable": _cable_view(cable), "near_end": near.end_label, "far_end": far.end_label,
             "port_connection_id": str(cable.port_connection_id) if cable.port_connection_id else None},
            far.equipment_port_id,
        )
    connections = (
        await db.execute(
            select(PortConnection)
            .where(
                PortConnection.target_port_id.is_not(None),
                or_(PortConnection.source_port_id == port_id, PortConnection.target_port_id == port_id),
            )
            .order_by(PortConnection.id)
            .limit(2)
        )
    ).scalars().all()
    if len(connections) > 1:
        raise _Broken("a port has more than one logical connection")
    if not connections:
        return None
    connection = connections[0]
    remote = connection.target_port_id if connection.source_port_id == port_id else connection.source_port_id
    assert remote is not None
    return (
        {"kind": "port_connection", "port_connection_id": str(connection.id), "status": connection.status,
         "cable_label": connection.cable_id, "note": "logical connection without a recorded physical cable"},
        remote,
    )


async def _pass_through(db: AsyncSession, port_id: uuid.UUID) -> tuple[PortPassThrough, uuid.UUID] | None:
    """The pass-through `port_id` belongs to and the port the signal continues on."""
    member = (
        await db.execute(select(PortPassThroughMember).where(PortPassThroughMember.equipment_port_id == port_id))
    ).scalar_one_or_none()
    if member is None:
        return None
    pair = (
        await db.execute(select(PortPassThroughMember).where(PortPassThroughMember.pass_through_id == member.pass_through_id))
    ).scalars().all()
    partners = [m.equipment_port_id for m in pair if m.equipment_port_id != port_id]
    parent = await db.get(PortPassThrough, member.pass_through_id)
    if parent is None or len(pair) != 2 or len(partners) != 1:
        raise _Broken("a pass-through does not join exactly two ports")
    return parent, partners[0]


async def trace_port(db: AsyncSession, *, scope: AccessScope, port_id: uuid.UUID) -> dict:
    start = await db.get(EquipmentPort, port_id)
    if start is None:
        raise NotFoundError(f"EquipmentPort {port_id} not found.")
    if not await _is_visible(db, scope, start.equipment_id):
        raise NotFoundError(f"EquipmentPort {port_id} not found.")
    start_view = await _port_view(db, port_id)

    path: list[dict] = []
    visited: set[uuid.UUID] = {port_id}
    current = port_id
    terminated = "no_link"
    reason: str | None = None
    first_remote: uuid.UUID | None = None
    try:
        while True:
            step = await _next_link(db, current)
            if step is None:
                terminated = "no_link"
                break
            if len(path) >= MAX_HOPS:
                terminated = "hop_limit"
                break
            link, remote_id = step
            remote = await db.get(EquipmentPort, remote_id)
            if remote is None:
                raise _Broken("a link ends at a port that no longer exists")
            if not await _is_visible(db, scope, remote.equipment_id):
                path.append({"link": link, "hop": {"restricted": True, "remote": None}, "pass_through": None})
                terminated = "restricted"
                break
            element: dict = {"link": link, "hop": {"restricted": False, "remote": await _port_view(db, remote_id)},
                             "pass_through": None}
            path.append(element)
            if first_remote is None:
                first_remote = remote_id
            if remote_id in visited:
                element["hop"]["cycle"] = True
                terminated = "cycle_detected"
                break
            visited.add(remote_id)
            continuation = await _pass_through(db, remote_id)
            if continuation is None:
                terminated = "end_of_path"
                break
            parent, partner_id = continuation
            partner = await db.get(EquipmentPort, partner_id)
            if partner is None or partner.equipment_id != remote.equipment_id:
                raise _Broken("a pass-through leaves its device")
            element["pass_through"] = {
                "id": str(parent.id), "label": parent.label, "from_port_id": str(remote_id),
                "to": await _port_view(db, partner_id),
            }
            if partner_id in visited:
                terminated = "cycle_detected"
                break
            visited.add(partner_id)
            current = partner_id
    except _Broken as error:
        terminated, reason = "broken_topology", str(error)

    remote_port_id = first_remote
    first_hop = path[0]["hop"] if path else None
    ports_for_evidence = [port_id] + ([remote_port_id] if remote_port_id and first_hop and not first_hop["restricted"] else [])
    evidence = await _evidence(db, scope, ports_for_evidence)
    agreement = "no_evidence"
    if evidence and remote_port_id is not None and first_hop and not first_hop["restricted"]:
        pair = {str(port_id), str(remote_port_id)}
        links = [{e["local_port_id"], e["remote_port_id"]} for e in evidence if e["local_port_id"] and e["remote_port_id"]]
        agreement = "agrees" if any(candidate == pair for candidate in links) else ("disagrees" if links else "no_evidence")
    elif evidence and remote_port_id is None:
        agreement = "undocumented_adjacency"

    previous = (
        await db.execute(
            select(Cable)
            .join(CableEndpoint, CableEndpoint.cable_id == Cable.id)
            .where(Cable.status == "removed", CableEndpoint.equipment_port_id == port_id)
            .order_by(Cable.removed_at.desc())
            .limit(MAX_PREVIOUS_CABLES)
        )
    ).scalars().all()
    return {
        "start": start_view,
        "path": path,
        "hop_count": len(path),
        "max_hops": MAX_HOPS,
        "terminated": terminated,
        "terminated_reason": reason,
        "evidence": {"authoritative": False, "neighbors": evidence, "agreement": agreement},
        "previous_cables": [_cable_view(c) for c in previous],
        "format": "trace.v2",
    }
