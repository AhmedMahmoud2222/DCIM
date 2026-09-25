"""Failure-impact ("blast radius") graph traversal (Phase 10C).

Reuses `app/application/power_graph.py`'s bounded, iterative BFS discipline for the
power side unchanged — `get_downstream_node_ids` already walks the exact distribution
graph (`PowerNode`/`PowerConnection`) a power-node failure needs, retired-node exclusion
and all. The network side has no separate "topology" table family to traverse — the
Phase 10B cabling graph (`EquipmentPort`/`PortConnection`) *is* the network topology — so
`_network_bfs` below implements one narrowly-scoped, analogous bounded BFS over it,
reusing `power_graph.py`'s own `MAX_TRAVERSAL_DEPTH`/`MAX_TRAVERSAL_NODES` bounds and
`GraphTraversalBounded` exception rather than inventing a second limit/exception pair for
what is architecturally the same kind of guard.

Both simulations are read-only: nothing here ever writes to `PowerConnection`,
`PortConnection`, or any equipment row. "Simulating a failure" means asking what the
graph already implies would happen if one node/port stopped working right now, never
modeling a hypothetical edit to the graph itself."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_graph import (
    MAX_TRAVERSAL_DEPTH,
    MAX_TRAVERSAL_NODES,
    GraphTraversalBounded,
    get_downstream_node_ids,
)
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort, EquipmentPowerInlet, PortConnection
from app.domain.power.models import PowerConnection, PowerNode

IMPACT_TARGET_TYPES = ("power_node", "network_port")


class ImpactTargetNotFound(ValueError):
    pass


@dataclass(frozen=True)
class ImpactedEquipment:
    equipment_id: uuid.UUID
    asset_tag: str
    hostname: str | None
    service: str | None
    hop: int
    # "power_loss" | "degraded_redundancy" (power); "network_isolated" | "network_degraded" (network)
    impact_type: str
    message: str


@dataclass(frozen=True)
class ImpactSimulationResult:
    target_type: str
    target_id: uuid.UUID
    directly_impacted: list[ImpactedEquipment] = field(default_factory=list)
    indirectly_impacted: list[ImpactedEquipment] = field(default_factory=list)
    # Human-readable redundancy-loss narratives (populated for "degraded_redundancy" and
    # "network_degraded" items — the ones that lose a path but keep another), e.g. "Server
    # X loses Power Supply A; running single-corded on Supply B", per the spec's own example.
    lost_redundancy_paths: list[str] = field(default_factory=list)
    affected_services: list[str] = field(default_factory=list)


async def _load_equipment_rows(
    db: AsyncSession, equipment_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[Equipment, ManagedAsset]]:
    if not equipment_ids:
        return {}
    rows = (
        await db.execute(
            select(Equipment, ManagedAsset)
            .join(ManagedAsset, ManagedAsset.id == Equipment.id)
            .where(Equipment.id.in_(equipment_ids))
        )
    ).all()
    return {equipment.id: (equipment, asset) for equipment, asset in rows}


def _affected_services(items: list[ImpactedEquipment]) -> list[str]:
    return sorted({item.service for item in items if item.service})


async def simulate_power_node_failure(db: AsyncSession, node_id: uuid.UUID) -> ImpactSimulationResult:
    """Every `EquipmentPowerInlet` fed (directly or transitively) by `node_id`, classified
    per equipment as a full `power_loss` (every modeled inlet on that equipment is
    downstream of the failed node) or a `degraded_redundancy` (at least one other inlet
    survives — the equipment keeps running, single-corded, on it)."""
    node = await db.get(PowerNode, node_id)
    if node is None:
        raise ImpactTargetNotFound(f"PowerNode {node_id} not found.")

    downstream_ids = await get_downstream_node_ids(db, node_id)  # bounded; may raise GraphTraversalBounded
    candidate_ids = downstream_ids | {node_id}

    direct_rows = (
        await db.execute(
            select(PowerConnection.target_node_id).where(
                PowerConnection.source_node_id == node_id, PowerConnection.effective_to.is_(None)
            )
        )
    ).scalars().all()
    # A node one hop below the failed node is "direct" — and so is the failed node
    # itself: simulating a failure targeted directly at an equipment_power_input (an
    # operator asking "what if this server's own inlet/PSU died", not just an upstream
    # PDU/breaker) must not get demoted to "indirect" merely because an
    # equipment_power_input is always a leaf with no outgoing PowerConnection of its own
    # to appear in direct_rows.
    direct_children = (set(direct_rows) & candidate_ids) | {node_id}

    impacted_inlets = (
        (await db.execute(select(EquipmentPowerInlet).where(EquipmentPowerInlet.power_node_id.in_(candidate_ids))))
        .scalars()
        .all()
    )
    if not impacted_inlets:
        return ImpactSimulationResult(target_type="power_node", target_id=node_id)

    affected_equipment_ids = {inlet.equipment_id for inlet in impacted_inlets}
    all_inlets = (
        (await db.execute(select(EquipmentPowerInlet).where(EquipmentPowerInlet.equipment_id.in_(affected_equipment_ids))))
        .scalars()
        .all()
    )
    all_inlets_by_equipment: dict[uuid.UUID, list[EquipmentPowerInlet]] = defaultdict(list)
    for inlet in all_inlets:
        all_inlets_by_equipment[inlet.equipment_id].append(inlet)
    impacted_by_equipment: dict[uuid.UUID, list[EquipmentPowerInlet]] = defaultdict(list)
    for inlet in impacted_inlets:
        impacted_by_equipment[inlet.equipment_id].append(inlet)

    equipment_rows = await _load_equipment_rows(db, affected_equipment_ids)

    directly: list[ImpactedEquipment] = []
    indirectly: list[ImpactedEquipment] = []
    lost_redundancy_paths: list[str] = []
    for equipment_id, impacted in impacted_by_equipment.items():
        equipment, asset = equipment_rows[equipment_id]
        surviving = [i for i in all_inlets_by_equipment[equipment_id] if i.power_node_id not in candidate_ids]
        hop = 1 if any(i.power_node_id in direct_children for i in impacted) else 2
        label = equipment.hostname or asset.asset_tag
        impacted_labels = ", ".join(i.label for i in impacted)
        if surviving:
            message = f"{label} loses {impacted_labels}; running single-corded on {surviving[0].label}"
            impact_type = "degraded_redundancy"
            lost_redundancy_paths.append(message)
        else:
            message = f"{label} loses all modeled power feeds ({impacted_labels}) — full outage"
            impact_type = "power_loss"
        item = ImpactedEquipment(
            equipment_id=equipment_id, asset_tag=asset.asset_tag, hostname=equipment.hostname, service=equipment.service,
            hop=hop, impact_type=impact_type, message=message,
        )
        (directly if hop == 1 else indirectly).append(item)

    return ImpactSimulationResult(
        target_type="power_node", target_id=node_id, directly_impacted=directly, indirectly_impacted=indirectly,
        lost_redundancy_paths=lost_redundancy_paths, affected_services=_affected_services([*directly, *indirectly]),
    )


async def _network_bfs(db: AsyncSession, root_port_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Bounded, iterative BFS (never recursive Python — the same discipline
    `power_graph._traverse` documents) over active `PortConnection` edges, treated as
    undirected: a physical link's failure disconnects both ends regardless of which one
    is recorded as `source_port_id`. Returns `{port_id: hop}` for every port other than
    `root_port_id` reachable through it — hop 1 for the port on the other end of
    `root_port_id`'s own cable, hop 2+ for anything reached through further chained
    connections (e.g. a patch panel, then an access switch's own uplink)."""
    visited: dict[uuid.UUID, int] = {}
    frontier: set[uuid.UUID] = {root_port_id}
    seen: set[uuid.UUID] = {root_port_id}
    depth = 0

    while frontier:
        depth += 1
        if depth > MAX_TRAVERSAL_DEPTH:
            raise GraphTraversalBounded(root_id=root_port_id, limit_kind="depth")

        rows = (
            await db.execute(
                select(PortConnection.source_port_id, PortConnection.target_port_id).where(
                    PortConnection.status == "active",
                    (PortConnection.source_port_id.in_(frontier)) | (PortConnection.target_port_id.in_(frontier)),
                )
            )
        ).all()
        next_frontier: set[uuid.UUID] = set()
        for source_id, target_id in rows:
            for a, b in ((source_id, target_id), (target_id, source_id)):
                if a in frontier and b is not None and b not in seen:
                    next_frontier.add(b)
        if not next_frontier:
            break
        seen |= next_frontier
        if len(seen) > MAX_TRAVERSAL_NODES:
            raise GraphTraversalBounded(root_id=root_port_id, limit_kind="nodes")
        for port_id in next_frontier:
            visited[port_id] = depth
        frontier = next_frontier

    return visited


async def simulate_network_port_failure(db: AsyncSession, port_id: uuid.UUID) -> ImpactSimulationResult:
    """Every piece of equipment reachable through `port_id`'s own cabling chain,
    classified per equipment as `network_isolated` (every reachable port on that
    equipment loses its only active connection) or `network_degraded` (at least one other
    active connection survives — e.g. a second uplink)."""
    port = await db.get(EquipmentPort, port_id)
    if port is None:
        raise ImpactTargetNotFound(f"EquipmentPort {port_id} not found.")

    reached = await _network_bfs(db, port_id)  # bounded; may raise GraphTraversalBounded
    if not reached:
        return ImpactSimulationResult(target_type="network_port", target_id=port_id)

    affected_ports = (await db.execute(select(EquipmentPort).where(EquipmentPort.id.in_(reached.keys())))).scalars().all()
    affected_equipment_ids = {p.equipment_id for p in affected_ports}
    affected_ports_by_equipment: dict[uuid.UUID, list[EquipmentPort]] = defaultdict(list)
    for p in affected_ports:
        affected_ports_by_equipment[p.equipment_id].append(p)

    all_ports = (
        (await db.execute(select(EquipmentPort).where(EquipmentPort.equipment_id.in_(affected_equipment_ids))))
        .scalars()
        .all()
    )
    all_port_ids = {p.id for p in all_ports}
    # A port can be connected as either PortConnection.source_port_id (the only column
    # `equipment_instantiation_service.list_port_connections` indexes — the right choice
    # for its own "this port's own outgoing cabling edge" use, wrong here) or
    # target_port_id (a patch-panel/switch port on the *receiving* end of someone else's
    # cable, exactly `server_port_b` in the dual-homed test below) — a redundancy check
    # must count both, so this queries both columns directly instead of reusing that helper.
    active_connected_rows = (
        await db.execute(
            select(PortConnection.source_port_id, PortConnection.target_port_id).where(
                PortConnection.status == "active",
                (PortConnection.source_port_id.in_(all_port_ids)) | (PortConnection.target_port_id.in_(all_port_ids)),
            )
        )
    ).all()
    actively_connected_port_ids: set[uuid.UUID] = set()
    for source_id, target_id in active_connected_rows:
        actively_connected_port_ids.add(source_id)
        if target_id is not None:
            actively_connected_port_ids.add(target_id)
    equipment_rows = await _load_equipment_rows(db, affected_equipment_ids)

    directly: list[ImpactedEquipment] = []
    indirectly: list[ImpactedEquipment] = []
    lost_redundancy_paths: list[str] = []
    for equipment_id, affected in affected_ports_by_equipment.items():
        equipment, asset = equipment_rows[equipment_id]
        affected_ids = {p.id for p in affected}
        surviving = [
            p for p in all_ports if p.id not in affected_ids and p.id in actively_connected_port_ids
        ]
        hop = min(reached[p.id] for p in affected)
        label = equipment.hostname or asset.asset_tag
        affected_names = ", ".join(p.display_name for p in affected)
        if surviving:
            message = f"{label} loses {affected_names}; still reachable via {surviving[0].display_name}"
            impact_type = "network_degraded"
            lost_redundancy_paths.append(message)
        else:
            message = f"{label} loses {affected_names} — network connectivity lost"
            impact_type = "network_isolated"
        item = ImpactedEquipment(
            equipment_id=equipment_id, asset_tag=asset.asset_tag, hostname=equipment.hostname, service=equipment.service,
            hop=hop, impact_type=impact_type, message=message,
        )
        (directly if hop == 1 else indirectly).append(item)

    return ImpactSimulationResult(
        target_type="network_port", target_id=port_id, directly_impacted=directly, indirectly_impacted=indirectly,
        lost_redundancy_paths=lost_redundancy_paths, affected_services=_affected_services([*directly, *indirectly]),
    )


async def simulate_failure(db: AsyncSession, *, target_type: str, target_id: uuid.UUID) -> ImpactSimulationResult:
    if target_type == "power_node":
        return await simulate_power_node_failure(db, target_id)
    if target_type == "network_port":
        return await simulate_network_port_failure(db, target_id)
    raise ValueError(f"Unsupported impact target_type: {target_type!r}")
