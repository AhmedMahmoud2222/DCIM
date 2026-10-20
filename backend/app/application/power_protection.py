"""Protection devices (Issue #102, area A): scope resolution, link validation and rating math.

A protection device is a `PowerNode` of type `protection_device`; its upstream and downstream links are
ordinary `PowerConnection` rows, so cycle checks, traversal and impact simulation see it with no second
graph. This module owns the rules the generic connection endpoint does not know about:

* every node resolves to a site where the data allows it (`resolve_node_site`); a device and every node it
  links to must agree, so a direct ID cannot weave one site's topology into another's;
* a device never links to itself (the connection table already forbids self loops) or to a retired node;
* a feeder whose rated voltage or phase configuration cannot carry the device's rating is rejected;
* a link whose rated current exceeds the device rating is rejected as an unsafe combination.
"""

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.domain.location.models import Building, Floor, Room
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.power.models import (
    PDU,
    UPS,
    Generator,
    PowerNode,
    PowerPanel,
    ProtectionDevice,
)

SQRT3 = math.sqrt(3)


def device_rated_kw(rating_a: float, voltage_v: float, phase_config: str) -> float:
    """Apparent power at unity power factor, in kW. Conservative and deterministic."""
    factor = SQRT3 if phase_config == "three" else 1.0
    return round(float(rating_a) * float(voltage_v) * factor / 1000.0, 3)


class ProtectionScopeError(ApiError):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=422, title="Invalid Protection Topology", detail=detail)


async def _room_site(db: AsyncSession, room_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Building.site_id)
            .select_from(Room)
            .join(Floor, Floor.id == Room.floor_id)
            .join(Building, Building.id == Floor.building_id)
            .where(Room.id == room_id)
        )
    ).scalar_one_or_none()


async def resolve_node_site(db: AsyncSession, node: PowerNode) -> uuid.UUID | None:
    """Best-known site of a node, or None when the data does not locate it (utility intake, PDUs and
    their outlets carry no room). None means "unknown", never "any site"."""
    t = node.node_type
    if t == "protection_device":
        return (
            await db.execute(select(ProtectionDevice.site_id).where(ProtectionDevice.power_node_id == node.id))
        ).scalar_one_or_none()
    asset_id = node.managed_asset_id or node.owning_asset_id
    if asset_id is not None:
        gen = (await db.execute(select(Generator.site_id).where(Generator.id == asset_id))).scalar_one_or_none()
        if gen is not None:
            return gen
        for model in (UPS, PowerPanel):
            room_id = (await db.execute(select(model.room_id).where(model.id == asset_id))).scalar_one_or_none()
            if room_id is not None:
                return await _room_site(db, room_id)
    if t == "equipment_power_input" and node.owning_asset_id is not None:
        return await _placed_site(db, node.owning_asset_id)
    if asset_id is not None and t in ("pdu", "pdu_outlet"):
        return await _placed_site(db, asset_id)
    return None


async def _placed_site(db: AsyncSession, asset_id: uuid.UUID) -> uuid.UUID | None:
    placement = (
        await db.execute(
            select(EquipmentPlacement.room_id, EquipmentPlacement.rack_id).where(
                EquipmentPlacement.equipment_id == asset_id, EquipmentPlacement.effective_to.is_(None)
            )
        )
    ).first()
    if placement is None:
        return None
    room_id, rack_id = placement
    if rack_id is not None:
        rack_room = (
            await db.execute(
                select(RackPlacement.room_id).where(RackPlacement.rack_id == rack_id, RackPlacement.effective_to.is_(None))
            )
        ).scalar_one_or_none()
        room_id = rack_room or room_id
    return await _room_site(db, room_id) if room_id is not None else None


async def housing_site(db: AsyncSession, housing_asset_id: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
    """(exists, site) for the asset that houses a device. PDUs exist but carry no site."""
    gen = (await db.execute(select(Generator.site_id).where(Generator.id == housing_asset_id))).scalar_one_or_none()
    if gen is not None:
        return True, gen
    for model in (UPS, PowerPanel):
        room_id = (await db.execute(select(model.room_id).where(model.id == housing_asset_id))).scalar_one_or_none()
        if room_id is not None:
            return True, await _room_site(db, room_id)
    pdu = (await db.execute(select(PDU.id).where(PDU.id == housing_asset_id))).scalar_one_or_none()
    if pdu is None:
        return False, None
    return True, await _placed_site(db, housing_asset_id)


async def assert_link_allowed(
    db: AsyncSession, *, source: PowerNode, target: PowerNode, rated_current_a: float | None, voltage: float | None,
    phase: str | None,
) -> None:
    """Called by the connection endpoint when either end is a protection device. Raises
    `ProtectionScopeError` (422) for cross-site links and unsafe rating/phase combinations."""
    devices = [n for n in (source, target) if n.node_type == "protection_device"]
    if not devices:
        return
    sites: dict[uuid.UUID, str] = {}
    for node in (source, target):
        site = await resolve_node_site(db, node)
        if site is not None:
            sites[site] = node.node_type
    if len(sites) > 1:
        raise ProtectionScopeError("A protection device cannot be linked to a node in a different site.")

    for dev_node in devices:
        dev = await db.get(ProtectionDevice, dev_node.id)
        if dev is None:
            raise ProtectionScopeError("Protection device record is missing.")
        if rated_current_a is not None and rated_current_a > float(dev.rating_a):
            raise ProtectionScopeError(
                f"Connection rated current {rated_current_a} A exceeds the device rating {float(dev.rating_a)} A."
            )
        if voltage is not None and abs(voltage - float(dev.voltage_v)) > 0.5 * float(dev.voltage_v):
            raise ProtectionScopeError("Connection voltage is incompatible with the device voltage rating.")
        if phase is not None and phase != dev.phase_config:
            raise ProtectionScopeError(
                f"Connection phase {phase!r} does not match the device phase configuration {dev.phase_config!r}."
            )


@dataclass(frozen=True)
class DeviceView:
    power_node_id: uuid.UUID
    rated_kw: float


async def inlet_path_status(
    db: AsyncSession, inlet_node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[bool, list[str]]]:
    """For each inlet node: (has a live path to a source, labels of interrupting devices found upstream).

    Read-only and bounded. A device is interrupting when it is open or tripped, or out of service. An
    `unknown` state is not interrupting. Retired nodes and non-active connections cut a path. A path is
    live when the walk reaches a source-type node (utility, generator, UPS, panel, PDU) that is not
    itself interrupting."""
    from app.application.power_graph import MAX_TRAVERSAL_NODES
    from app.application.power_rollup import ROOT_TYPES
    from app.domain.power.models import PowerConnection

    result: dict[uuid.UUID, tuple[bool, list[str]]] = {}
    for inlet_id in inlet_node_ids:
        seen = {inlet_id}
        frontier = {inlet_id}
        live = False
        blockers: set[str] = set()
        while frontier and len(seen) <= MAX_TRAVERSAL_NODES:
            edges = (
                await db.execute(
                    select(PowerConnection.source_node_id, PowerConnection.target_node_id).where(
                        PowerConnection.effective_to.is_(None), PowerConnection.status == "active",
                        PowerConnection.target_node_id.in_(frontier),
                    )
                )
            ).all()
            parent_ids = {s for s, _ in edges} - seen
            nodes = {
                n.id: n
                for n in (
                    await db.execute(select(PowerNode).where(PowerNode.id.in_(frontier | parent_ids)))
                ).scalars()
            }
            devices = {
                d.power_node_id: d
                for d in (
                    await db.execute(select(ProtectionDevice).where(ProtectionDevice.power_node_id.in_(frontier | parent_ids)))
                ).scalars()
            }
            has_parent = {t for _, t in edges}
            next_frontier: set[uuid.UUID] = set()
            for nid in frontier:
                node = nodes.get(nid)
                dev = devices.get(nid)
                if node is None or node.retired_at is not None:
                    continue
                if dev is not None and (dev.state in ("open", "tripped") or dev.status == "out_of_service"):
                    blockers.add(node.label)
                    continue
                if nid not in has_parent:
                    if node.node_type in ROOT_TYPES:
                        live = True
                    continue
            for s, t in edges:
                if t in frontier and s not in seen:
                    next_frontier.add(s)
            seen |= next_frontier
            frontier = next_frontier
            if live:
                break
        result[inlet_id] = (live, sorted(blockers))
    return result
