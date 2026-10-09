# ruff: noqa: E501
"""Cooling capacity, thermal load, headroom and redundancy (Issue #105). This is an accounting view, not CFD.

Definitions (kW of heat removal, canonical unit):
  installed_rated   sum of the nameplate rating of every air-side unit that serves the zone
  available         sum of effective capacity of units that are currently available
                    (effective = configured/derated figure if set, else rated; unknown stays unknown)
  thermal_load      IT heat released into the zone
  headroom          available - thermal_load; withheld (null) unless both sides are fully known

A unit is *available* only when its lifecycle is installed/active AND its operating status is online/standby.
Maintenance, offline, fault, unknown and decommissioned units contribute nothing.

Air-side units are CRAC and CRAH. A chiller produces chilled water for CRAH coils; adding its capacity to the room
would double count, so chillers are reported in a separate `plant` block and never added to room headroom.

Thermal load assumption (explicit, echoed in every result): the electrical IT load from the #102 power roll-up
(`measured where fresh, last known where stale, nameplate estimate where never measured`) is treated as heat released
into the room at 1.0 W per W (`ELECTRICAL_TO_THERMAL_FACTOR`). That is energy conservation for IT equipment that
dissipates essentially all of its input power as heat. It excludes UPS/PDU losses, lighting, envelope and solar gain,
and does not use COP, PUE or any efficiency figure. A zone with geometry counts racks whose footprint centre lies in
it; floor-standing non-rack equipment is only counted by zones that cover the whole room.

Redundancy (deterministic): pool = all members of the unit's cooling group, or the zone's ungrouped serving units.
  not_configured        no air-side unit serves the zone
  unavailable           no unit in the pool is available
  single_unit           the pool has exactly one unit (no redundancy provisioned)
  degraded              2+ units provisioned but at least one is unavailable, OR all are available but the pool
                        load exceeds the capacity left after losing its largest available unit (N+1 not met)
  redundant             2+ units, all available, and load <= capacity - largest available unit  (N+1 verified)
  redundant_unverified  2+ units, all available, but a capacity or load figure is unknown so N+1 cannot be verified
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope
from app.application.power_rollup import DEFAULT_CRITICAL_PCT, DEFAULT_WARNING_PCT
from app.application.power_rollup_loader import rollup_for_site
from app.application.thermal.environment import assert_room_visible, scope_sees_site_assets
from app.application.thermal.interpolation import point_in_polygon
from app.domain.catalog.models import RackModelRevision
from app.domain.cooling.models import (
    AVAILABLE_LIFECYCLE_STATUSES,
    AVAILABLE_OPERATING_STATUSES,
    CoolingGroup,
    CoolingUnit,
    CoolingUnitZone,
    ThermalZone,
)
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Building, Floor, Room
from app.domain.physical.models import Rack
from app.domain.placement.models import RackPlacement

ELECTRICAL_TO_THERMAL_FACTOR = 1.0
AIR_SIDE_KINDS = ("crac", "crah")
LOAD_ASSUMPTION = (
    "Thermal load = electrical IT load x 1.0 (all IT input power becomes heat). Source: #102 power roll-up "
    "(measured where fresh, last known where stale, nameplate estimate where never measured). Excludes UPS/PDU "
    "losses, lighting and envelope gains. No COP, PUE or efficiency factor is applied."
)
_POOL_SEVERITY = {"unavailable": 0, "degraded": 1, "single_unit": 2, "redundant_unverified": 3, "redundant": 4}


@dataclass
class UnitIn:
    id: uuid.UUID
    name: str
    kind: str
    lifecycle_status: str
    operating_status: str
    rated_kw: float | None
    configured_kw: float | None
    group_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    semantics: str = "configured"

    @property
    def available(self) -> bool:
        return self.lifecycle_status in AVAILABLE_LIFECYCLE_STATUSES and self.operating_status in AVAILABLE_OPERATING_STATUSES

    @property
    def effective_kw(self) -> float | None:
        return self.configured_kw if self.configured_kw is not None else self.rated_kw

    @property
    def unavailable_reason(self) -> str | None:
        if self.lifecycle_status not in AVAILABLE_LIFECYCLE_STATUSES:
            return f"lifecycle_{self.lifecycle_status}"
        if self.operating_status not in AVAILABLE_OPERATING_STATUSES:
            return f"operating_{self.operating_status}"
        return None


def evaluate_pool(units: list[UnitIn], load_kw: float | None) -> dict[str, Any]:
    """Pure redundancy evaluation of one pool of air-side units against one thermal load (None = not fully known)."""
    provisioned = len(units)
    available = [u for u in units if u.available]
    result: dict[str, Any] = {
        "provisioned_units": provisioned, "available_units": len(available), "n_plus_1_verified": None, "reason": None,
        "unavailable_unit_ids": sorted(str(u.id) for u in units if not u.available),
    }
    if provisioned == 0:
        return {**result, "state": "not_configured"}
    if not available:
        # unknown operating status is "insufficient data", distinct from units that are known to be down
        reason = "availability_unknown" if any(u.operating_status == "unknown" for u in units) else "no_available_unit"
        return {**result, "state": "unavailable", "reason": reason}
    if provisioned == 1:
        return {**result, "state": "single_unit"}
    caps = [u.effective_kw for u in available]
    if len(available) < 2:
        verified: bool | None = False
    elif any(c is None for c in caps) or load_kw is None:
        verified = None
    else:
        known = [c for c in caps if c is not None]
        verified = load_kw <= sum(known) - max(known) + 1e-9
    result["n_plus_1_verified"] = verified
    if len(available) < provisioned:
        return {**result, "state": "degraded", "reason": "unit_unavailable"}
    if verified is False:
        return {**result, "state": "degraded", "reason": "insufficient_n_plus_1_capacity"}
    return {**result, "state": "redundant" if verified else "redundant_unverified"}


def worst_pool_state(states: list[str]) -> str:
    ranked = [s for s in states if s in _POOL_SEVERITY]
    return min(ranked, key=lambda s: _POOL_SEVERITY[s]) if ranked else "not_configured"


def sum_known(values: list[float | None]) -> tuple[float, bool]:
    """(sum of the known values, True when every value was known)."""
    return sum(v for v in values if v is not None), all(v is not None for v in values)


@dataclass
class ZoneLoad:
    state: str  # known | incomplete | unknown | withheld_by_scope | not_permitted
    electrical_kw: float | None = None
    thermal_kw: float | None = None
    quality: str | None = None
    rack_count: int = 0
    rack_ids: list[str] = field(default_factory=list)
    unassigned_rack_count: int = 0
    missing_load_rack_count: int = 0
    basis: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state, "electrical_kw": self.electrical_kw, "thermal_kw": self.thermal_kw, "quality": self.quality,
            "rack_count": self.rack_count, "unassigned_rack_count": self.unassigned_rack_count,
            "missing_load_rack_count": self.missing_load_rack_count, "basis": self.basis,
            "assumption": LOAD_ASSUMPTION, "electrical_to_thermal_factor": ELECTRICAL_TO_THERMAL_FACTOR,
        }


def zone_contains(zone: ThermalZone, x: float, y: float) -> bool:
    if zone.geometry_type == "rect":
        # the DB check geometry_shape_consistent guarantees all four are set for a rect; an unset one means the zone cannot contain anything
        if zone.x_mm is None or zone.y_mm is None or zone.width_mm is None or zone.height_mm is None:
            return False
        return zone.x_mm <= x <= zone.x_mm + zone.width_mm and zone.y_mm <= y <= zone.y_mm + zone.height_mm
    if zone.geometry_type == "polygon" and zone.points:
        return point_in_polygon(x, y, [(float(p[0]), float(p[1])) for p in zone.points])
    return True  # no geometry => whole room


def _quality_of(qualities: list[str]) -> str | None:
    unique = set(qualities)
    if not unique:
        return None
    return next(iter(unique)) if len(unique) == 1 else "mixed"


async def _zone_load(
    db: AsyncSession, zone: ThermalZone, rollup: Any, rack_rows: list[tuple[uuid.UUID, float | None, float | None]],
    *, can_read_power: bool, scope: AccessScope, site_id: uuid.UUID,
) -> ZoneLoad:
    if not can_read_power:
        return ZoneLoad(state="not_permitted", basis="power:read is required to see thermal load")
    if not scope_sees_site_assets(scope, site_id):
        return ZoneLoad(state="withheld_by_scope", basis="load depends on racks outside the caller's scope")
    if rollup is None:
        return ZoneLoad(state="unknown", basis="no power roll-up available")
    if zone.geometry_type is None:
        room = rollup.rooms.get(zone.room_id)
        if room is None:
            return ZoneLoad(state="unknown", basis="whole room; no power modelled for this room")
        return ZoneLoad(
            state="known", electrical_kw=round(room.load_kw, 3), thermal_kw=round(room.load_kw * ELECTRICAL_TO_THERMAL_FACTOR, 3),
            quality=room.quality, rack_count=len([1 for r in rack_rows]), basis="whole room (all racks and floor equipment)",
        )
    inside: list[uuid.UUID] = []
    unassigned = 0
    for rack_id, cx, cy in rack_rows:
        if cx is None or cy is None:
            unassigned += 1
        elif zone_contains(zone, cx, cy):
            inside.append(rack_id)
    loads: list[float] = []
    qualities: list[str] = []
    missing = 0
    for rack_id in sorted(inside, key=str):
        scoped = rollup.racks.get(rack_id)
        if scoped is None or scoped.quality == "missing":
            missing += 1
        else:
            loads.append(scoped.load_kw)
            qualities.append(scoped.quality)
    electrical = round(sum(loads), 3)
    state = "known" if not (unassigned or missing) else "incomplete"
    return ZoneLoad(
        state=state, electrical_kw=electrical if loads or state == "known" else None,
        thermal_kw=round(electrical * ELECTRICAL_TO_THERMAL_FACTOR, 3) if loads or state == "known" else None,
        quality=_quality_of(qualities), rack_count=len(inside), rack_ids=[str(r) for r in sorted(inside, key=str)],
        unassigned_rack_count=unassigned, missing_load_rack_count=missing,
        basis="racks whose footprint centre lies inside the zone (floor equipment not counted)",
    )


async def room_site_id(db: AsyncSession, room_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Building.site_id).join(Floor, Floor.building_id == Building.id).join(Room, Room.floor_id == Floor.id).where(Room.id == room_id)
        )
    ).scalar_one_or_none()


async def _rack_centres(db: AsyncSession, room_id: uuid.UUID) -> list[tuple[uuid.UUID, float | None, float | None]]:
    rows = (
        await db.execute(
            select(RackPlacement.rack_id, RackPlacement.x_mm, RackPlacement.y_mm, RackPlacement.rotation_deg, RackModelRevision.width_mm, RackModelRevision.depth_mm)
            .join(Rack, Rack.id == RackPlacement.rack_id)
            .join(RackModelRevision, RackModelRevision.id == Rack.model_revision_id)
            .where(RackPlacement.room_id == room_id, RackPlacement.effective_to.is_(None))
        )
    ).all()
    out: list[tuple[uuid.UUID, float | None, float | None]] = []
    for rack_id, x, y, rot, width, depth in rows:
        if x is None or y is None:
            out.append((rack_id, None, None))
            continue
        w, d = (depth, width) if (rot or 0) % 180 == 90 else (width, depth)
        out.append((rack_id, x + w / 2.0, y + d / 2.0))
    return out


def _unit_out(u: UnitIn, relation: str | None = None) -> dict[str, Any]:
    return {
        "id": str(u.id), "name": u.name, "kind": u.kind, "lifecycle_status": u.lifecycle_status, "operating_status": u.operating_status,
        "available": u.available, "unavailable_reason": u.unavailable_reason, "rated_kw": u.rated_kw, "configured_kw": u.configured_kw,
        "effective_kw": u.effective_kw, "capacity_known": u.effective_kw is not None,
        "cooling_group_id": str(u.group_id) if u.group_id else None, "relationship": relation, "relationship_semantics": u.semantics,
    }


async def build_room_capacity(
    db: AsyncSession, *, room_id: uuid.UUID, scope: AccessScope, can_read_power: bool, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    site_id = await assert_room_visible(db, room_id, scope)
    if not scope_sees_site_assets(scope, site_id):
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Room {room_id} has no cooling configuration visible to this caller.")
    zones = list(
        (
            await db.execute(
                select(ThermalZone)
                .where(ThermalZone.room_id == room_id, ThermalZone.zone_kind == "served_zone", ThermalZone.retired.is_(False))
                .order_by(ThermalZone.name, ThermalZone.id)
            )
        ).scalars()
    )
    result: dict[str, Any] = {
        "room_id": str(room_id), "site_id": str(site_id), "generated_at": now.isoformat(), "load_assumption": LOAD_ASSUMPTION,
        "thresholds": {"warning_utilization_pct": DEFAULT_WARNING_PCT, "critical_utilization_pct": DEFAULT_CRITICAL_PCT},
        "zones": [], "state": "ok", "reasons": [],
    }
    if not zones:
        result.update(state="not_configured", reasons=["no_served_zone"])
        return result

    rel_rows = (
        await db.execute(
            select(CoolingUnitZone.thermal_zone_id, CoolingUnitZone.relation_kind, CoolingUnitZone.semantics, CoolingUnit, ManagedAsset.lifecycle_status)
            .join(CoolingUnit, CoolingUnit.id == CoolingUnitZone.cooling_unit_id)
            .join(ManagedAsset, ManagedAsset.id == CoolingUnit.id)
            .where(CoolingUnitZone.thermal_zone_id.in_([z.id for z in zones]), CoolingUnitZone.relation_kind == "serves")
        )
    ).all()

    def to_in(unit: CoolingUnit, lifecycle: str, semantics: str = "configured") -> UnitIn:
        return UnitIn(
            unit.id, unit.name, unit.unit_kind, lifecycle, unit.operating_status,
            float(unit.rated_cooling_capacity_kw) if unit.rated_cooling_capacity_kw is not None else None,
            float(unit.configured_cooling_capacity_kw) if unit.configured_cooling_capacity_kw is not None else None,
            unit.cooling_group_id, unit.site_id, semantics,
        )

    serving: dict[uuid.UUID, list[UnitIn]] = defaultdict(list)
    for zone_id, _kind, semantics, unit, lifecycle in rel_rows:
        if scope_sees_site_assets(scope, unit.site_id):
            serving[zone_id].append(to_in(unit, lifecycle, semantics))

    group_ids = {u.group_id for units in serving.values() for u in units if u.group_id is not None}
    group_members: dict[uuid.UUID, list[UnitIn]] = defaultdict(list)
    group_zone_ids: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    if group_ids:
        for unit, lifecycle in (
            await db.execute(
                select(CoolingUnit, ManagedAsset.lifecycle_status)
                .join(ManagedAsset, ManagedAsset.id == CoolingUnit.id)
                .where(CoolingUnit.cooling_group_id.in_(group_ids), CoolingUnit.unit_kind.in_(AIR_SIDE_KINDS))
            )
        ).all():
            if scope_sees_site_assets(scope, unit.site_id) and unit.cooling_group_id is not None:  # selected by group_id IN (...), never NULL
                group_members[unit.cooling_group_id].append(to_in(unit, lifecycle))
        for unit_id, zone_id in (
            await db.execute(
                select(CoolingUnitZone.cooling_unit_id, CoolingUnitZone.thermal_zone_id)
                .where(CoolingUnitZone.relation_kind == "serves", CoolingUnitZone.cooling_unit_id.in_([m.id for ms in group_members.values() for m in ms]))
            )
        ).all():
            for gid, members in group_members.items():
                if any(m.id == unit_id for m in members):
                    group_zone_ids[gid].add(zone_id)

    rollup = await rollup_for_site(db, site_id, now) if can_read_power else None  # the caller sees the whole site (checked above)
    rack_rows = await _rack_centres(db, room_id)
    zone_loads: dict[uuid.UUID, ZoneLoad] = {
        z.id: await _zone_load(db, z, rollup, rack_rows, can_read_power=can_read_power, scope=scope, site_id=site_id) for z in zones
    }
    # Zones outside this room that a pooled group also serves (needed for the pool's total load).
    outside_ids = {zid for zs in group_zone_ids.values() for zid in zs} - set(zone_loads)
    outside_zones = (
        {z.id: z for z in (await db.execute(select(ThermalZone).where(ThermalZone.id.in_(outside_ids)))).scalars()} if outside_ids else {}
    )
    for zid, z in outside_zones.items():
        zone_loads[zid] = await _zone_load(db, z, rollup, await _rack_centres(db, z.room_id), can_read_power=can_read_power, scope=scope, site_id=site_id)

    plant_rel = (
        await db.execute(
            select(CoolingUnitZone.thermal_zone_id, CoolingUnit, ManagedAsset.lifecycle_status, CoolingUnitZone.semantics)
            .join(CoolingUnit, CoolingUnit.id == CoolingUnitZone.cooling_unit_id)
            .join(ManagedAsset, ManagedAsset.id == CoolingUnit.id)
            .where(CoolingUnitZone.thermal_zone_id.in_([z.id for z in zones]), CoolingUnitZone.relation_kind == "serves", CoolingUnit.unit_kind == "chiller")
        )
    ).all()
    chillers: dict[uuid.UUID, list[UnitIn]] = defaultdict(list)
    for zone_id, unit, lifecycle, semantics in plant_rel:
        if scope_sees_site_assets(scope, unit.site_id):
            chillers[zone_id].append(to_in(unit, lifecycle, semantics))

    worst = "ok"
    for zone in zones:
        air = [u for u in serving.get(zone.id, []) if u.kind in AIR_SIDE_KINDS]
        load = zone_loads[zone.id]
        pools: dict[uuid.UUID | None, list[UnitIn]] = defaultdict(list)
        for air_unit in air:
            pools[air_unit.group_id].append(air_unit)
        pool_results: list[dict[str, Any]] = []
        for pool_gid, pool_members in sorted(pools.items(), key=lambda kv: str(kv[0])):
            if pool_gid is None:
                zone_pool_load = load.thermal_kw if load.state == "known" else None
                pool_results.append({"scope": "zone_units", "cooling_group_id": None, **evaluate_pool(pool_members, zone_pool_load)})
            else:
                everyone = {m.id: m for m in group_members.get(pool_gid, [])}
                for m in pool_members:
                    everyone.setdefault(m.id, m)
                loads = [zone_loads[zid] for zid in sorted(group_zone_ids.get(pool_gid, set()) | {zone.id}, key=str) if zid in zone_loads]
                pool_load = sum(item.thermal_kw or 0.0 for item in loads) if loads and all(i.state == "known" for i in loads) else None
                pool_results.append({"scope": "cooling_group", "cooling_group_id": str(pool_gid), "pool_load_kw": pool_load, **evaluate_pool(list(everyone.values()), pool_load)})
        redundancy_state = worst_pool_state([p["state"] for p in pool_results]) if pool_results else "not_configured"

        rated_kw, rated_complete = sum_known([u.rated_kw for u in air])
        avail_units = [u for u in air if u.available]
        available_kw, available_complete = sum_known([u.effective_kw for u in avail_units])
        # An operating status of "unknown" is missing information, not a unit that is known to be down: while any unit's
        # availability is unknown, the available total is only a lower bound and must not be used for headroom.
        availability_known = all(u.operating_status != "unknown" for u in air)
        available_complete = available_complete and availability_known
        no_known_capacity = bool(avail_units) and all(u.effective_kw is None for u in avail_units)
        thermal = load.thermal_kw if load.state == "known" else None
        headroom = round(available_kw - thermal, 3) if thermal is not None and available_complete and air else None
        utilization = round(100.0 * thermal / available_kw, 1) if thermal is not None and available_complete and available_kw > 0 else None
        if not air:
            level = "not_configured"
        elif not avail_units and availability_known:
            level = "critical"  # every unit is known to be unavailable: genuinely zero capacity
        elif headroom is None:
            level = "unknown"
        elif headroom < 0 or (utilization is not None and utilization >= DEFAULT_CRITICAL_PCT):
            level = "critical"
        elif utilization is not None and utilization >= DEFAULT_WARNING_PCT:
            level = "warning"
        else:
            level = "ok"
        insufficient_data = any(p.get("reason") == "availability_unknown" for p in pool_results)
        if redundancy_state in ("unavailable", "degraded") and level in ("ok", "unknown") and not insufficient_data:
            level = "warning" if redundancy_state == "degraded" else "critical"
        chiller_units = chillers.get(zone.id, [])
        plant_available, plant_complete = sum_known([u.effective_kw for u in chiller_units if u.available])
        plant_rated, plant_rated_complete = sum_known([u.rated_kw for u in chiller_units])
        result["zones"].append(
            {
                "zone_id": str(zone.id), "name": zone.name, "geometry": "whole_room" if zone.geometry_type is None else zone.geometry_type,
                "units": [_unit_out(u, "serves") for u in sorted(air, key=lambda u: str(u.id))],
                "installed_rated_kw": rated_kw if any(u.rated_kw is not None for u in air) else None, "installed_rated_complete": rated_complete and bool(air),
                "available_kw": available_kw if air and not no_known_capacity and (avail_units or availability_known) else None,
                "available_complete": available_complete and bool(air),
                "available_units": len(avail_units), "unit_count": len(air),
                "thermal_load": load.as_dict(), "headroom_kw": headroom, "utilization_pct": utilization, "level": level,
                "redundancy": {"state": redundancy_state, "pools": pool_results},
                "plant": {
                    "note": "Chillers feed CRAH coils; their capacity is never added to room headroom.",
                    "units": [_unit_out(u, "serves") for u in sorted(chiller_units, key=lambda u: str(u.id))],
                    "installed_rated_kw": plant_rated if any(u.rated_kw is not None for u in chiller_units) else None, "installed_rated_complete": plant_rated_complete and bool(chiller_units),
                    "available_kw": plant_available if chiller_units and any(u.effective_kw is not None for u in chiller_units if u.available) else None,
                    "available_complete": plant_complete and bool(chiller_units),
                },
            }
        )
        rank = {"ok": 0, "not_configured": 1, "unknown": 1, "warning": 2, "critical": 3}
        if rank[level] > rank[worst]:
            worst = level
    result["state"] = worst
    return result


async def groups_summary(db: AsyncSession, site_id: uuid.UUID, scope: AccessScope) -> list[dict[str, Any]]:
    if not scope_sees_site_assets(scope, site_id):
        return []
    groups = (
        await db.execute(select(CoolingGroup).where(CoolingGroup.site_id == site_id, CoolingGroup.retired.is_(False)).order_by(CoolingGroup.name))
    ).scalars().all()
    members: dict[uuid.UUID, list[str]] = defaultdict(list)
    for member in (await db.execute(select(CoolingUnit).where(CoolingUnit.site_id == site_id, CoolingUnit.cooling_group_id.is_not(None)))).scalars():
        if member.cooling_group_id is not None:
            members[member.cooling_group_id].append(str(member.id))
    return [{"id": str(g.id), "name": g.name, "member_ids": sorted(members.get(g.id, [])), "version": g.version} for g in groups]
