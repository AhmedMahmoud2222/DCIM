"""Deterministic matching of a discovered neighbor to inventory ports.

`evaluate_neighbor` is read-only: it never writes. It returns the state the evidence
supports and an explanation; `neighbor_service` stores that on the neighbor row. It never
sets `local_port_id`/`remote_port_id` (only an operator confirmation does) and never
touches `port_connection`, `cable` or equipment.

Matching rules (nothing is guessed; every uncertainty becomes `unmatched` or `ambiguous`):

1. Local side. The observing integration must already be reconciled to a managed asset
   (`DiscoveredDevice.status == "reconciled"`, set only by a human). The reported local
   port name must equal exactly one of that equipment's ports (`display_name` or
   `stable_key`, compared case-insensitively with whitespace collapsed).
2. Remote device, by evidence tier:
   * management address == `Equipment.ip_address`, or == the target host of an integration
     reconciled to an asset  (strong);
   * chassis ID that is a MAC == `Equipment.mac_address`  (strong);
   * system name / CDP device ID == `Equipment.hostname`, exact or short name  (weak).
   Two strong candidates for one tier, strong tiers that disagree, or a weak tier that
   contradicts a strong one are `ambiguous`. A weak tier alone never yields a proposal; it is
   offered as a suggestion for an operator.
3. Remote port. The remote port ID (or description) must equal exactly one port of the
   resolved remote equipment.
4. A fully resolved pair is `proposed` unless it contradicts authoritative evidence: a
   confirmed neighbor or a live physical/logical link on either port that points elsewhere
   makes it `conflict`; the authoritative side is never changed.
5. Another active neighbor on the same local port, from a different protocol, naming a
   different remote device makes both observations `ambiguous` (protocol disagreement).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.network.neighbor_evidence import normalize_mac, normalize_name
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import DiscoveredDevice, Integration
from app.domain.network.discovery_models import DiscoveredNeighbor
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort, PortConnection

MAX_CANDIDATES = 10


@dataclass
class MatchOutcome:
    state: str
    evidence: dict = field(default_factory=dict)


def fingerprint(neighbor: DiscoveredNeighbor) -> str:
    """Hash of the evidence fields matching depends on, to detect changes cheaply."""
    material = "\x1f".join(
        str(v or "")
        for v in (
            neighbor.integration_id, neighbor.local_port_name, neighbor.remote_chassis_ident, neighbor.remote_chassis_subtype,
            neighbor.remote_port_ident, neighbor.remote_port_description, neighbor.remote_system_name,
            neighbor.remote_management_address,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _port_names(port: EquipmentPort) -> set[str]:
    return {normalize_name(port.display_name), normalize_name(port.stable_key)}


def _ids(values) -> list[str]:
    return sorted(str(v) for v in values)[:MAX_CANDIDATES]


async def _live_equipment_ids(db: AsyncSession, *conditions) -> set[uuid.UUID]:
    stmt = (
        select(Equipment.id)
        .join(ManagedAsset, ManagedAsset.id == Equipment.id)
        .where(ManagedAsset.decommissioned_at.is_(None), *conditions)
        .limit(MAX_CANDIDATES + 1)
    )
    return set((await db.execute(stmt)).scalars())


async def _local_side(db: AsyncSession, neighbor: DiscoveredNeighbor) -> tuple[dict, uuid.UUID | None, uuid.UUID | None]:
    devices = list(
        (
            await db.execute(
                select(DiscoveredDevice.matched_managed_asset_id).where(
                    DiscoveredDevice.integration_id == neighbor.integration_id,
                    DiscoveredDevice.status == "reconciled",
                    DiscoveredDevice.matched_managed_asset_id.is_not(None),
                )
            )
        ).scalars()
    )
    if not devices:
        return {"state": "unmatched", "reason": "the observing device is not reconciled to an asset"}, None, None
    equipment = await _live_equipment_ids(db, Equipment.id.in_(set(devices)))
    if len(equipment) != 1:
        return (
            {"state": "ambiguous" if len(equipment) > 1 else "unmatched",
             "reason": "no unique live equipment for the observing device", "candidates": _ids(equipment)},
            None, None,
        )
    equipment_id = next(iter(equipment))
    if not neighbor.local_port_name:
        reason = "the local port name was not reported"
        return {"state": "unmatched", "equipment_id": str(equipment_id), "reason": reason}, equipment_id, None
    wanted = normalize_name(neighbor.local_port_name)
    ports = (await db.execute(select(EquipmentPort).where(EquipmentPort.equipment_id == equipment_id))).scalars().all()
    matches = [p.id for p in ports if wanted in _port_names(p)]
    if len(matches) == 1:
        return {"state": "matched", "equipment_id": str(equipment_id), "port_id": str(matches[0])}, equipment_id, matches[0]
    return (
        {"state": "ambiguous" if matches else "unmatched", "equipment_id": str(equipment_id),
         "reason": "several ports match the local port name" if matches else "no port matches the local port name",
         "candidates": _ids(matches)},
        equipment_id, None,
    )


async def _remote_device(db: AsyncSession, neighbor: DiscoveredNeighbor) -> dict:
    tiers: dict[str, list[uuid.UUID]] = {}
    address = neighbor.remote_management_address
    if address:
        by_ip = await _live_equipment_ids(db, func.host(Equipment.ip_address) == address)
        via_integration = set(
            (
                await db.execute(
                    select(DiscoveredDevice.matched_managed_asset_id)
                    .join(Integration, Integration.id == DiscoveredDevice.integration_id)
                    .where(
                        Integration.target_host == address, DiscoveredDevice.status == "reconciled",
                        DiscoveredDevice.matched_managed_asset_id.is_not(None),
                    )
                )
            ).scalars()
        )
        if via_integration:
            by_ip |= await _live_equipment_ids(db, Equipment.id.in_(via_integration))
        tiers["management_address"] = sorted(by_ip, key=str)
    mac = normalize_mac(neighbor.remote_chassis_ident)
    if mac:
        digits = mac.replace(":", "")
        stored = func.regexp_replace(func.lower(Equipment.mac_address), "[^0-9a-f]", "", "g")
        found = await _live_equipment_ids(db, Equipment.mac_address.is_not(None), stored == digits)
        tiers["chassis_mac"] = sorted(found, key=str)
    names = {
        normalize_name(v)
        for v in (neighbor.remote_system_name, neighbor.remote_chassis_ident)
        if v and not normalize_mac(v) and not re.fullmatch(r"[0-9a-fA-F:.\-]+", v)
    }
    if names:
        shorts = {n.split(".", 1)[0] for n in names}
        tiers["system_name"] = sorted(
            await _live_equipment_ids(
                db,
                Equipment.hostname.is_not(None),
                or_(
                    func.lower(Equipment.hostname).in_(names),
                    func.lower(func.split_part(Equipment.hostname, ".", 1)).in_(shorts),
                ),
            ),
            key=str,
        )

    strong = {k: v for k, v in tiers.items() if k in ("management_address", "chassis_mac") and v}
    weak = tiers.get("system_name", [])
    evidence = {"tiers": {k: _ids(v) for k, v in tiers.items()}}
    for tier, found in strong.items():
        if len(found) > 1:
            return evidence | {"state": "ambiguous", "reason": f"several inventory items share this {tier.replace('_', ' ')}"}
    singles = {v[0] for v in strong.values()}
    if len(singles) > 1:
        return evidence | {"state": "ambiguous", "reason": "management address and chassis MAC identify different equipment"}
    if singles:
        chosen = next(iter(singles))
        if weak and chosen not in weak:
            return evidence | {"state": "ambiguous", "reason": "the reported system name contradicts the address/MAC match"}
        return evidence | {
            "state": "matched", "equipment_id": str(chosen), "strength": "strong", "agreeing_tiers": sorted(strong),
        }
    if len(weak) == 1:
        return evidence | {
            "state": "unmatched", "suggested_equipment_id": str(weak[0]),
            "reason": "only the system name matches; name-only evidence is not sufficient",
        }
    if len(weak) > 1:
        return evidence | {"state": "ambiguous", "reason": "several equipment share this system name"}
    return evidence | {"state": "unmatched", "reason": "no inventory equipment matches the remote device"}


async def _remote_port(db: AsyncSession, neighbor: DiscoveredNeighbor, equipment_id: uuid.UUID) -> dict:
    wanted = {normalize_name(v) for v in (neighbor.remote_port_ident, neighbor.remote_port_description) if v}
    ports = (await db.execute(select(EquipmentPort).where(EquipmentPort.equipment_id == equipment_id))).scalars().all()
    matches = [p.id for p in ports if wanted & _port_names(p)]
    if len(matches) == 1:
        return {"state": "matched", "port_id": str(matches[0])}
    return {
        "state": "ambiguous" if matches else "unmatched",
        "reason": "several ports match the remote port" if matches else "no port of the remote equipment matches the remote port",
        "candidates": _ids(matches),
    }


async def authoritative_links(
    db: AsyncSession, port_ids: list[uuid.UUID], *, exclude_neighbor_id: uuid.UUID | None
) -> list[dict]:
    """Authoritative adjacencies touching any of `port_ids`: logical port connections,
    confirmed neighbors and (from `cable_links`) live physical cables."""
    links: list[dict] = []
    rows = await db.execute(
        select(PortConnection.id, PortConnection.source_port_id, PortConnection.target_port_id).where(
            PortConnection.target_port_id.is_not(None),
            or_(PortConnection.source_port_id.in_(port_ids), PortConnection.target_port_id.in_(port_ids)),
        )
    )
    links += [{"kind": "port_connection", "id": str(i), "ports": sorted([str(a), str(b)])} for i, a, b in rows]
    stmt = select(DiscoveredNeighbor.id, DiscoveredNeighbor.local_port_id, DiscoveredNeighbor.remote_port_id).where(
        DiscoveredNeighbor.reconciliation_state == "confirmed",
        or_(DiscoveredNeighbor.local_port_id.in_(port_ids), DiscoveredNeighbor.remote_port_id.in_(port_ids)),
    )
    if exclude_neighbor_id is not None:
        stmt = stmt.where(DiscoveredNeighbor.id != exclude_neighbor_id)
    links += [
        {"kind": "confirmed_neighbor", "id": str(i), "ports": sorted([str(a), str(b)])}
        for i, a, b in await db.execute(stmt)
        if a and b
    ]
    from app.application.network.cable_links import live_cable_links

    links += await live_cable_links(db, port_ids)
    return links


async def _protocol_disagreement(db: AsyncSession, neighbor: DiscoveredNeighbor) -> list[dict]:
    if not neighbor.local_port_name:
        return []
    rows = (
        await db.execute(
            select(DiscoveredNeighbor).where(
                DiscoveredNeighbor.integration_id == neighbor.integration_id,
                DiscoveredNeighbor.id != neighbor.id,
                DiscoveredNeighbor.protocol != neighbor.protocol,
                DiscoveredNeighbor.status == "active",
                DiscoveredNeighbor.reconciliation_state != "rejected",
                func.lower(DiscoveredNeighbor.local_port_name) == neighbor.local_port_name.casefold(),
            ).limit(MAX_CANDIDATES)
        )
    ).scalars().all()
    mine = {normalize_name(v) for v in (neighbor.remote_system_name, neighbor.remote_chassis_ident) if v}
    mac = normalize_mac(neighbor.remote_chassis_ident)
    out = []
    for other in rows:
        theirs = {normalize_name(v) for v in (other.remote_system_name, other.remote_chassis_ident) if v}
        same = bool(mine & theirs) or (mac is not None and mac == normalize_mac(other.remote_chassis_ident)) or (
            neighbor.remote_management_address is not None
            and neighbor.remote_management_address == other.remote_management_address
        )
        if not same:
            short_mine = {n.split(".", 1)[0] for n in mine}
            same = bool(short_mine & {n.split(".", 1)[0] for n in theirs})
        if not same:
            remote = other.remote_system_name or other.remote_chassis_ident
            out.append({"neighbor_id": str(other.id), "protocol": other.protocol, "remote": remote})
    return out


async def evaluate_neighbor(db: AsyncSession, neighbor: DiscoveredNeighbor) -> MatchOutcome:
    local, _local_equipment, local_port = await _local_side(db, neighbor)
    evidence: dict = {"local": local, "fingerprint": fingerprint(neighbor), "evaluated_at": datetime.now(UTC).isoformat()}
    disagreement = await _protocol_disagreement(db, neighbor)
    if disagreement:
        evidence["protocol_disagreement"] = disagreement

    remote = await _remote_device(db, neighbor)
    evidence["remote_device"] = remote
    remote_port: dict | None = None
    if remote["state"] == "matched":
        remote_port = await _remote_port(db, neighbor, uuid.UUID(remote["equipment_id"]))
        evidence["remote_port"] = remote_port

    states = [local["state"], remote["state"]] + ([remote_port["state"]] if remote_port else [])
    if "ambiguous" in states:
        return MatchOutcome("ambiguous", evidence)
    if disagreement:
        evidence["reasons"] = ["another protocol reports a different neighbor on the same local port"]
        return MatchOutcome("ambiguous", evidence)
    if local_port is None or remote_port is None or remote_port["state"] != "matched":
        return MatchOutcome("unmatched", evidence)

    remote_port_id = uuid.UUID(remote_port["port_id"])
    pair = sorted([str(local_port), str(remote_port_id)])
    evidence["proposal"] = {"local_port_id": str(local_port), "remote_port_id": str(remote_port_id)}
    if local_port == remote_port_id:
        evidence["reasons"] = ["local and remote ports resolve to the same port"]
        return MatchOutcome("ambiguous", evidence)
    links = await authoritative_links(db, [local_port, remote_port_id], exclude_neighbor_id=neighbor.id)
    conflicts = [link for link in links if link["ports"] != pair]
    corroborating = [link for link in links if link["ports"] == pair]
    if corroborating:
        evidence["corroborated_by"] = corroborating
    if conflicts:
        evidence["conflicts"] = conflicts
        return MatchOutcome("conflict", evidence)
    return MatchOutcome("proposed", evidence)
