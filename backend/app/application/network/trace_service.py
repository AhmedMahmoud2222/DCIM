"""Port-to-port trace: device -> port -> cable -> remote port -> remote device.

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
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort, PortConnection

MAX_PREVIOUS_CABLES = 5
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


async def trace_port(db: AsyncSession, *, scope: AccessScope, port_id: uuid.UUID) -> dict:
    start = await db.get(EquipmentPort, port_id)
    if start is None:
        raise NotFoundError(f"EquipmentPort {port_id} not found.")
    if not await _is_visible(db, scope, start.equipment_id):
        raise NotFoundError(f"EquipmentPort {port_id} not found.")
    start_view = await _port_view(db, port_id)

    live = (
        await db.execute(
            select(Cable, CableEndpoint)
            .join(CableEndpoint, CableEndpoint.cable_id == Cable.id)
            .where(Cable.is_live.is_(True), CableEndpoint.equipment_port_id == port_id)
        )
    ).first()
    link: dict = {"kind": "none"}
    remote_port_id: uuid.UUID | None = None
    if live is not None:
        cable, near = live
        far = (
            await db.execute(select(CableEndpoint).where(CableEndpoint.cable_id == cable.id, CableEndpoint.id != near.id))
        ).scalar_one()
        remote_port_id = far.equipment_port_id
        link = {"kind": "cable", "cable": _cable_view(cable), "near_end": near.end_label, "far_end": far.end_label,
                "port_connection_id": str(cable.port_connection_id) if cable.port_connection_id else None}
    else:
        connection = (
            await db.execute(
                select(PortConnection).where(
                    PortConnection.target_port_id.is_not(None),
                    or_(PortConnection.source_port_id == port_id, PortConnection.target_port_id == port_id),
                )
            )
        ).scalars().first()
        if connection is not None:
            remote_port_id = connection.target_port_id if connection.source_port_id == port_id else connection.source_port_id
            link = {"kind": "port_connection", "port_connection_id": str(connection.id), "status": connection.status,
                    "cable_label": connection.cable_id, "note": "logical connection without a recorded physical cable"}

    hop: dict | None = None
    if remote_port_id is not None:
        remote_equipment = (await db.get(EquipmentPort, remote_port_id)).equipment_id  # type: ignore[union-attr]
        if await _is_visible(db, scope, remote_equipment):
            hop = {"restricted": False, "remote": await _port_view(db, remote_port_id)}
        else:
            hop = {"restricted": True, "remote": None}

    ports_for_evidence = [port_id] + ([remote_port_id] if remote_port_id and hop and not hop["restricted"] else [])
    evidence = await _evidence(db, scope, ports_for_evidence)
    agreement = "no_evidence"
    if evidence and remote_port_id is not None and hop and not hop["restricted"]:
        pair = {str(port_id), str(remote_port_id)}
        links = [{e["local_port_id"], e["remote_port_id"]} for e in evidence if e["local_port_id"] and e["remote_port_id"]]
        agreement = "agrees" if any(link == pair for link in links) else ("disagrees" if links else "no_evidence")
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
    terminated = "no_link" if remote_port_id is None else ("restricted" if hop and hop["restricted"] else "end_of_path")
    return {
        "start": start_view,
        "path": [{"link": link, "hop": hop}] if remote_port_id is not None else [],
        "terminated": terminated,
        "evidence": {"authoritative": False, "neighbors": evidence, "agreement": agreement},
        "previous_cables": [_cable_view(c) for c in previous],
        "format": "trace.v1",
    }
