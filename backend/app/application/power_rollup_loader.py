"""Builds `power_rollup.compute_rollup` inputs for one site from the existing tables (Issue #102).

Telemetry comes from `TelemetryReading` rows for the canonical metric (`power_kw`, kW) that the #99
registry already normalised at ingest. Nothing is converted here and no reading is written or altered."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_graph import MAX_TRAVERSAL_NODES
from app.application.power_protection import device_rated_kw
from app.application.power_rollup import EdgeIn, EquipmentIn, NodeIn, RollupResult, compute_rollup
from app.core.errors import ApiError
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Building, Floor, Room
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.power.models import (
    UPS,
    Generator,
    PowerCapacity,
    PowerConnection,
    PowerNode,
    PowerPanel,
    ProtectionDevice,
)
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import METRIC_REGISTRY

POWER_METRIC = "power_kw"
POWER_UNIT = METRIC_REGISTRY[POWER_METRIC].canonical_unit


def _f(v) -> float | None:
    return float(v) if v is not None else None


async def load_rollup_inputs(
    db: AsyncSession, site_id: uuid.UUID
) -> tuple[list[NodeIn], list[EdgeIn], list[EquipmentIn]]:
    room_site = {
        r: s
        for r, s in (
            await db.execute(
                select(Room.id, Building.site_id)
                .join(Floor, Floor.id == Room.floor_id)
                .join(Building, Building.id == Floor.building_id)
            )
        ).all()
    }
    nodes = (await db.execute(select(PowerNode).where(PowerNode.retired_at.is_(None)))).scalars().all()
    if len(nodes) > MAX_TRAVERSAL_NODES:
        raise ApiError(status_code=422, title="Graph Too Large", detail="Power graph exceeds the rollup bound.")
    cap_rows = (await db.execute(select(PowerCapacity).where(PowerCapacity.effective_to.is_(None)))).scalars()
    caps = {c.power_node_id: c for c in cap_rows}
    devices = {d.power_node_id: d for d in (await db.execute(select(ProtectionDevice))).scalars()}
    gen_site = dict((await db.execute(select(Generator.id, Generator.site_id))).all())
    asset_room: dict[uuid.UUID, uuid.UUID] = {}
    for model in (UPS, PowerPanel):
        asset_room.update(dict((await db.execute(select(model.id, model.room_id))).all()))

    placements = (
        await db.execute(
            select(EquipmentPlacement.equipment_id, EquipmentPlacement.rack_id, EquipmentPlacement.room_id).where(
                EquipmentPlacement.effective_to.is_(None)
            )
        )
    ).all()
    rack_room = dict(
        (await db.execute(select(RackPlacement.rack_id, RackPlacement.room_id).where(RackPlacement.effective_to.is_(None)))).all()
    )
    placed: dict[uuid.UUID, tuple[uuid.UUID | None, uuid.UUID | None]] = {}
    for eq_id, rack_id, plc_room in placements:
        placed[eq_id] = (rack_id, rack_room.get(rack_id, plc_room) if rack_id else plc_room)

    # The same definition `equipment_power_summary` uses: an equipment item's inlets are the
    # `equipment_power_input` nodes it owns, however they were created.
    inlets = [(n.owning_asset_id, n.id) for n in nodes if n.node_type == "equipment_power_input" and n.owning_asset_id]
    inlet_equipment = {n: e for e, n in inlets}

    def node_site_room(n: PowerNode) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        if n.node_type == "protection_device":
            d = devices.get(n.id)
            return (d.site_id if d else None), None
        asset = n.managed_asset_id or n.owning_asset_id
        if asset in gen_site:
            return gen_site[asset], None
        if asset in asset_room:
            housing_room = asset_room[asset]
            return room_site.get(housing_room), housing_room
        if n.node_type == "equipment_power_input":
            eq_room = placed.get(inlet_equipment.get(n.id, uuid.UUID(int=0)), (None, None))[1]
            return (room_site.get(eq_room) if eq_room else None), None
        if asset in placed:
            asset_placed_room = placed[asset][1]
            return (room_site.get(asset_placed_room) if asset_placed_room else None), None
        return None, None

    resolved = {n.id: node_site_room(n) for n in nodes}
    edges_all = (
        await db.execute(
            select(
                PowerConnection.source_node_id, PowerConnection.target_node_id, PowerConnection.feed_label,
                PowerConnection.status,
            ).where(PowerConnection.effective_to.is_(None))
        )
    ).all()
    in_scope = {nid for nid, (s, _) in resolved.items() if s == site_id}
    # Nodes with no resolvable site join the scope only through an edge to a node already in it.
    unknown = {nid for nid, (s, _) in resolved.items() if s is None}
    changed = True
    while changed:
        changed = False
        for src, dst, _, _ in edges_all:
            for a, b in ((src, dst), (dst, src)):
                if a in in_scope and b in unknown and b not in in_scope:
                    in_scope.add(b)
                    changed = True

    node_in: list[NodeIn] = []
    for n in nodes:
        if n.id not in in_scope:
            continue
        cap = caps.get(n.id)
        dev = devices.get(n.id)
        rated = _f(cap.rated_capacity_kw) if cap else None
        capacity = None
        if cap is not None:
            capacity = _f(cap.configured_capacity_kw) if cap.configured_capacity_kw is not None else rated
        if dev is not None:
            dev_kw = device_rated_kw(dev.rating_a, dev.voltage_v, dev.phase_config)
            rated = dev_kw if rated is None else rated
            capacity = dev_kw if capacity is None else capacity
        site, room = resolved[n.id]
        node_in.append(
            NodeIn(
                n.id, n.node_type, n.label, site, room, capacity, rated, dev.state if dev else None,
                dev.status if dev else None, _f(cap.warning_threshold_pct) if cap else None,
                _f(cap.critical_threshold_pct) if cap else None, cap.redundancy_factor if cap else None,
            )
        )
    edge_in = [EdgeIn(s, t, fl, st) for s, t, fl, st in edges_all if s in in_scope and t in in_scope]

    by_equipment: dict[uuid.UUID, list[tuple[uuid.UUID, float | None]]] = {}
    cap_by_node = {x.id: x.capacity_kw for x in node_in}
    for eq_id, node_id in sorted(inlets, key=lambda r: (str(r[0]), str(r[1]))):
        if node_id in in_scope:
            by_equipment.setdefault(eq_id, []).append((node_id, cap_by_node.get(node_id)))

    latest: dict[uuid.UUID, tuple[float, datetime]] = {}
    if by_equipment:
        sub = (
            select(
                TelemetryReading.managed_asset_id,
                func.max(TelemetryReading.occurred_at).label("ts"),
            )
            .where(
                TelemetryReading.metric == POWER_METRIC, TelemetryReading.unit == POWER_UNIT,
                TelemetryReading.managed_asset_id.in_(list(by_equipment)),
            )
            .group_by(TelemetryReading.managed_asset_id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(TelemetryReading.managed_asset_id, TelemetryReading.value, TelemetryReading.occurred_at)
                .join(
                    sub,
                    (sub.c.managed_asset_id == TelemetryReading.managed_asset_id)
                    & (sub.c.ts == TelemetryReading.occurred_at),
                )
                .where(TelemetryReading.metric == POWER_METRIC)
                .order_by(TelemetryReading.managed_asset_id, TelemetryReading.value)
            )
        ).all()
        for aid, value, ts in rows:
            if aid is not None:
                latest[aid] = (float(value), ts)  # on a timestamp tie the highest value wins (sorted ascending)

    tag_rows = (
        await db.execute(select(ManagedAsset.id, ManagedAsset.asset_tag).where(ManagedAsset.id.in_(list(by_equipment))))
        if by_equipment
        else None
    )
    tags = dict(tag_rows.all()) if tag_rows is not None else {}
    eq_in = []
    for eq_id, lst in sorted(by_equipment.items(), key=lambda kv: str(kv[0])):
        rack_id, room_id = placed.get(eq_id, (None, None))
        reading = latest.get(eq_id)
        eq_in.append(
            EquipmentIn(
                eq_id, tags.get(eq_id) or str(eq_id), tuple(lst), rack_id, room_id, site_id,
                reading[0] if reading else None, reading[1] if reading else None,
            )
        )
    return node_in, edge_in, eq_in


async def rollup_for_site(db: AsyncSession, site_id: uuid.UUID, now: datetime, freshness_seconds: int = 900) -> RollupResult:
    nodes, edges, equipment = await load_rollup_inputs(db, site_id)
    return compute_rollup(nodes, edges, equipment, now=now, site_id=site_id, freshness_seconds=freshness_seconds)
