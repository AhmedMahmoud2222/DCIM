# ruff: noqa: E501
"""Read-only operational overlays for the 2D/3D spatial twin. Every item is keyed by the same ManagedAsset
ids (racks and equipment) and, where relevant, power-node / cable ids that the rest of the application uses;
nothing here creates a parallel topology. Each overlay says where its data came from and how fresh it is.

  power        equipment_power_summary + inlet_path_status (protection-device / feed-impact aware)
  network      authoritative cables only; unconfirmed LLDP/CDP neighbours are counted separately and never
               presented as topology
  environment  latest temperature/humidity telemetry with freshness and data quality; no estimation is done
               here (cooling/CFD modelling is a later work package), so a gap is reported as a gap"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_capacity import equipment_power_summary
from app.application.power_protection import inlet_path_status
from app.domain.alarm.models import Alarm
from app.domain.integration.models import Integration
from app.domain.network.cable_models import Cable, CableEndpoint
from app.domain.network.discovery_models import DiscoveredNeighbor
from app.domain.physical.ports import EquipmentPort
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.telemetry.models import TelemetryReading

MAX_OVERLAY_ASSETS = 500
ENVIRONMENT_METRICS = ("temperature_c", "humidity_percent")
FRESH_POLL_MULTIPLE = 3
_SEVERITY = {"critical": 3, "warning": 2, "normal": 1, "unavailable": 0}


@dataclass(frozen=True)
class RoomAssets:
    rack_ids: list[uuid.UUID]
    equipment_by_rack: dict[uuid.UUID, list[uuid.UUID]]
    floor_equipment_ids: list[uuid.UUID]

    @property
    def all_equipment_ids(self) -> list[uuid.UUID]:
        ids = [e for members in self.equipment_by_rack.values() for e in members]
        return sorted(set(ids) | set(self.floor_equipment_ids), key=str)


async def load_room_assets(db: AsyncSession, room_id: uuid.UUID) -> RoomAssets:
    rack_ids = list(
        (await db.execute(select(RackPlacement.rack_id).where(RackPlacement.room_id == room_id, RackPlacement.effective_to.is_(None)))).scalars()
    )
    by_rack: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    floor: list[uuid.UUID] = []
    rows = (
        await db.execute(
            select(EquipmentPlacement.equipment_id, EquipmentPlacement.rack_id, EquipmentPlacement.placement_type).where(
                EquipmentPlacement.room_id == room_id, EquipmentPlacement.effective_to.is_(None)
            )
        )
    ).all()
    for equipment_id, rack_id, placement_type in rows:
        if placement_type == "rack_mounted" and rack_id is not None:
            by_rack[rack_id].append(equipment_id)
        else:
            floor.append(equipment_id)
    return RoomAssets(rack_ids=rack_ids, equipment_by_rack=dict(by_rack), floor_equipment_ids=floor)


def _worst(states: list[str]) -> str:
    if not states:
        return "unavailable"
    return max(states, key=lambda s: _SEVERITY[s])


async def power_overlay(db: AsyncSession, assets: RoomAssets) -> dict[str, Any]:
    equipment_ids = assets.all_equipment_ids[:MAX_OVERLAY_ASSETS]
    items: dict[uuid.UUID, dict[str, Any]] = {}
    for eq_id in equipment_ids:
        summary = await equipment_power_summary(db, eq_id)
        feed_ids = [f["power_node_id"] for f in summary.feed_nodes]
        interrupted: list[str] = []
        dead_feeds = 0
        if feed_ids:
            # inlet_path_status reports (a source is reachable, protection devices found open/tripped upstream).
            # A feed counts as down when no source is reachable OR a device on its path interrupts it; the same
            # reading the failure-impact simulation uses ("blockers" decide a power loss).
            for _, (live, blockers) in (await inlet_path_status(db, feed_ids)).items():
                if not live or blockers:
                    dead_feeds += 1
                interrupted.extend(blockers)
        if summary.redundancy_classification == "no_power_modeled":
            state, reason = "unavailable", "No power feed is modelled for this equipment."
        elif dead_feeds == len(feed_ids) and feed_ids:
            state, reason = "critical", "No live power path" + (f"; interrupted by {', '.join(sorted(set(interrupted)))}" if interrupted else ".")
        elif dead_feeds:
            state, reason = "warning", "One feed has no live power path (redundancy lost)" + (
                f"; interrupted by {', '.join(sorted(set(interrupted)))}" if interrupted else "."
            )
        elif summary.redundancy_classification == "degraded":
            state, reason = "warning", "Redundant feeds share an upstream dependency."
        elif summary.redundancy_classification == "single_feed":
            state, reason = "warning", "Only a single power feed is modelled (no A/B redundancy)."
        else:
            state, reason = "normal", "Redundant feeds are healthy."
        items[eq_id] = {
            "asset_id": str(eq_id), "asset_kind": "equipment", "state": state, "reason": reason,
            "redundancy": summary.redundancy_classification, "data_quality": summary.data_quality,
            "feed_node_ids": [str(f) for f in feed_ids], "interrupting_devices": sorted(set(interrupted)),
            "effective_demand_kw": summary.effective_demand_kw,
        }
    racks: list[dict[str, Any]] = []
    for rack_id in assets.rack_ids:
        members = [items[e] for e in assets.equipment_by_rack.get(rack_id, []) if e in items]
        state = _worst([m["state"] for m in members if m["state"] != "unavailable"]) if any(m["state"] != "unavailable" for m in members) else "unavailable"
        racks.append(
            {
                "asset_id": str(rack_id), "asset_kind": "rack", "state": state, "member_count": len(members),
                "reason": "Worst state of the equipment mounted in this rack." if members else "No equipment is mounted in this rack.",
            }
        )
    return {"source": "power_topology", "items": [*racks, *items.values()], "truncated": len(assets.all_equipment_ids) > MAX_OVERLAY_ASSETS}


async def network_overlay(db: AsyncSession, assets: RoomAssets) -> dict[str, Any]:
    equipment_ids = assets.all_equipment_ids[:MAX_OVERLAY_ASSETS]
    per_equipment: dict[uuid.UUID, dict[str, Any]] = {
        e: {"installed": 0, "planned": 0, "cable_ids": [], "peers": set()} for e in equipment_ids
    }
    discovered: dict[uuid.UUID, int] = defaultdict(int)
    if equipment_ids:
        rows = (
            await db.execute(
                select(Cable.id, Cable.label, Cable.status, EquipmentPort.equipment_id, CableEndpoint.end_label)
                .join(CableEndpoint, CableEndpoint.cable_id == Cable.id)
                .join(EquipmentPort, EquipmentPort.id == CableEndpoint.equipment_port_id)
                .where(Cable.is_live.is_(True))
            )
        ).all()
        ends: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
        for cable_id, _label, status, equipment_id, _end in rows:
            ends[cable_id].append((equipment_id, status))
        for cable_id, endpoints in ends.items():
            for equipment_id, status in endpoints:
                if equipment_id in per_equipment:
                    bucket = per_equipment[equipment_id]
                    bucket["installed" if status == "installed" else "planned"] += 1
                    bucket["cable_ids"].append(str(cable_id))
                    bucket["peers"].update(str(other) for other, _ in endpoints if other != equipment_id)
        neighbor_rows = (
            await db.execute(
                select(EquipmentPort.equipment_id)
                .join(DiscoveredNeighbor, DiscoveredNeighbor.local_port_id == EquipmentPort.id)
                .where(
                    DiscoveredNeighbor.status == "active", DiscoveredNeighbor.reconciliation_state != "confirmed",
                    EquipmentPort.equipment_id.in_(equipment_ids),
                )
            )
        ).scalars()
        for equipment_id in neighbor_rows:
            discovered[equipment_id] += 1
    items: list[dict[str, Any]] = []
    for eq_id, bucket in per_equipment.items():
        if bucket["installed"]:
            state, reason = "normal", f"{bucket['installed']} installed cable(s) recorded."
        elif bucket["planned"]:
            state, reason = "warning", "Only planned (not yet installed) cables are recorded."
        else:
            state, reason = "unavailable", "No authoritative cable is recorded for this equipment."
        items.append(
            {
                "asset_id": str(eq_id), "asset_kind": "equipment", "state": state, "reason": reason, "source": "authoritative_cables",
                "installed_cables": bucket["installed"], "planned_cables": bucket["planned"], "cable_ids": sorted(set(bucket["cable_ids"])),
                "peer_asset_ids": sorted(bucket["peers"]),
                "discovered_unconfirmed": discovered.get(eq_id, 0),
            }
        )
    for rack_id in assets.rack_ids:
        members = [i for i in items if uuid.UUID(i["asset_id"]) in assets.equipment_by_rack.get(rack_id, [])]
        installed = sum(m["installed_cables"] for m in members)
        planned = sum(m["planned_cables"] for m in members)
        items.append(
            {
                "asset_id": str(rack_id), "asset_kind": "rack", "source": "authoritative_cables",
                "state": "normal" if installed else ("warning" if planned else "unavailable"),
                "reason": f"{installed} installed / {planned} planned cable end(s) on mounted equipment.",
                "installed_cables": installed, "planned_cables": planned, "cable_ids": [], "peer_asset_ids": [],
                "discovered_unconfirmed": sum(m["discovered_unconfirmed"] for m in members),
            }
        )
    return {"source": "authoritative_cables", "items": items, "truncated": len(assets.all_equipment_ids) > MAX_OVERLAY_ASSETS}


async def environment_overlay(db: AsyncSession, assets: RoomAssets) -> dict[str, Any]:
    asset_ids = [*assets.rack_ids, *assets.all_equipment_ids][: MAX_OVERLAY_ASSETS * 2]
    now = datetime.now(UTC)
    latest: dict[tuple[uuid.UUID, str], tuple[TelemetryReading, int]] = {}
    if asset_ids:
        rows = (
            await db.execute(
                select(TelemetryReading, Integration.poll_interval_seconds)
                .join(Integration, Integration.id == TelemetryReading.integration_id)
                .where(TelemetryReading.managed_asset_id.in_(asset_ids), TelemetryReading.metric.in_(ENVIRONMENT_METRICS))
                .order_by(TelemetryReading.occurred_at.desc())
            )
        ).all()
        for reading, interval in rows:
            assert reading.managed_asset_id is not None
            latest.setdefault((reading.managed_asset_id, reading.metric), (reading, interval))
    alarmed = set(
        (
            await db.execute(
                select(Alarm.managed_asset_id).where(Alarm.managed_asset_id.in_(asset_ids), Alarm.status.in_(("ACTIVE", "ACKNOWLEDGED")))
            )
        ).scalars()
    ) if asset_ids else set()
    items: list[dict[str, Any]] = []
    for (asset_id, metric), (reading, interval) in latest.items():
        age = (now - reading.occurred_at).total_seconds()
        fresh = age <= max(interval, 1) * FRESH_POLL_MULTIPLE
        items.append(
            {
                "asset_id": str(asset_id), "asset_kind": "rack" if asset_id in assets.rack_ids else "equipment", "metric": metric,
                "value": float(reading.value), "unit": reading.unit, "occurred_at": reading.occurred_at.isoformat(),
                "age_seconds": int(age), "expected_poll_interval_seconds": interval,
                "data_quality": "measured" if fresh else "stale",
                "state": "critical" if asset_id in alarmed else ("normal" if fresh else "warning"),
                "reason": "Active alarm on this asset." if asset_id in alarmed else ("Fresh measured value." if fresh else "Reading is older than three poll intervals."),
            }
        )
    reported = {uuid.UUID(i["asset_id"]) for i in items}
    for asset_id in asset_ids:
        if asset_id not in reported:
            items.append(
                {
                    "asset_id": str(asset_id), "asset_kind": "rack" if asset_id in assets.rack_ids else "equipment", "metric": None,
                    "value": None, "unit": None, "occurred_at": None, "age_seconds": None, "expected_poll_interval_seconds": None,
                    "data_quality": "missing", "state": "unavailable", "reason": "No temperature or humidity telemetry is mapped to this asset.",
                }
            )
    return {"source": "telemetry_latest", "estimated_values": False, "items": items, "truncated": len(asset_ids) >= MAX_OVERLAY_ASSETS * 2}
