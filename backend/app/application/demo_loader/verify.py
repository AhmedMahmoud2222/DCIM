# ruff: noqa: E501
"""Read-only verification of the demo state. Every check names what it looked at, so the output is evidence."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import UNRESTRICTED
from app.application.demo_loader.constants import DEDUP_PREFIX, DEFAULT_HISTORY_HOURS, SENSOR_METRICS, SENSOR_TAG_PREFIX
from app.application.demo_loader.floorplans import validate_geometry
from app.application.demo_loader.selection import Targets
from app.application.thermal.environment import MEASURED_FRESH
from app.application.thermal.heatmap import HEATMAP_METRICS, build_heat_map
from app.domain.cooling.models import EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement
from app.domain.spatial.models import FloorPlan
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks], "data": self.data}


async def verify(
    db: AsyncSession, targets: Targets, *, expected_racks: int | None = None, expected_devices: int | None = None,
    hours: float = DEFAULT_HISTORY_HOURS, now: datetime | None = None, require_sensors: bool = True,
) -> VerifyReport:
    now = now or datetime.now(UTC)
    report = VerifyReport()
    if expected_racks is not None:
        report.add("rack count unchanged", len(targets.racks) == expected_racks, f"{len(targets.racks)} racks (expected {expected_racks})")
    if expected_devices is not None:
        report.add("equipment count unchanged", len(targets.devices) == expected_devices, f"{len(targets.devices)} equipment (expected {expected_devices})")
    total_equipment = (await db.execute(select(func.count()).select_from(Equipment))).scalar_one()
    total_racks = (await db.execute(select(func.count()).select_from(Rack))).scalar_one()
    report.data["totals"] = {"racks": total_racks, "equipment": total_equipment}

    # ---- history: every simulated series has many points, no slot twice, all flagged synthetic
    start = now - timedelta(hours=hours)
    counts = (
        await db.execute(
            select(TelemetryReading.mapping_id, func.count(), func.count(func.distinct(TelemetryReading.occurred_at)))
            .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.occurred_at >= start)
            .group_by(TelemetryReading.mapping_id)
        )
    ).all()
    report.data["simulated_series"] = len(counts)
    if counts:
        fewest = min(c[1] for c in counts)
        report.add("history has many points per series", fewest >= 12, f"{len(counts)} series, fewest points in {hours:g} h = {fewest}")
        report.add("no slot stored twice", all(c[1] == c[2] for c in counts), "distinct slots == rows for every series")
    else:
        report.add("history has many points per series", False, "no simulated readings found; run `apply`")
    unflagged = (
        await db.execute(
            select(func.count()).select_from(TelemetryReading).where(
                TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.attributes["synthetic"].astext.is_distinct_from("true")
            )
        )
    ).scalar_one()
    report.add("every simulated reading is flagged synthetic", unflagged == 0, f"{unflagged} unflagged")
    pinned = (
        await db.execute(
            select(func.count()).select_from(TelemetryReading).where(
                TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.contract_evidence.is_distinct_from("pinned")
            )
        )
    ).scalar_one()
    report.add("every simulated reading is pinned to its mapping revision", pinned == 0, f"{pinned} not pinned")

    # ---- devices: a mapping per device and metric, with data
    device_ids = [d.id for d in targets.devices]
    if device_ids:
        mapped = (
            await db.execute(
                select(func.count(func.distinct(IntegrationMetricMapping.managed_asset_id))).where(
                    IntegrationMetricMapping.managed_asset_id.in_(device_ids), IntegrationMetricMapping.label == "Demo simulator"
                )
            )
        ).scalar_one()
        report.data["devices_with_mappings"] = mapped
        with_data = (
            await db.execute(
                select(func.count(func.distinct(TelemetryReading.managed_asset_id))).where(
                    TelemetryReading.managed_asset_id.in_(device_ids), TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)
                )
            )
        ).scalar_one()
        report.add("every device has simulated telemetry", mapped == len(device_ids) == with_data, f"{with_data}/{len(device_ids)} devices with readings, {mapped} with mappings")

    # ---- sensors
    sensors = (
        await db.execute(
            select(EnvironmentalSensor, ManagedAsset).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id)
            .where(ManagedAsset.asset_tag.startswith(SENSOR_TAG_PREFIX), EnvironmentalSensor.site_id.in_([s.id for s in targets.sites]))
        )
    ).all()
    if require_sensors:
        report.add("one sensor per rack", len(sensors) == len(targets.racks), f"{len(sensors)} sensors for {len(targets.racks)} racks")
        report.add("sensors are combined inlet sensors", all(s.sensor_kind == "combined" and s.measurement_role == "rack_inlet" for s, _ in sensors), "kind=combined, role=rack_inlet")
        report.add("sensors are commissioned", all(a.lifecycle_status == "active" for _, a in sensors), f"{sum(a.lifecycle_status == 'active' for _, a in sensors)} active")
        rack_by_id = {r.id: r for r in targets.racks}
        placements = {
            p.equipment_id: p
            for p in (
                await db.execute(select(EquipmentPlacement).where(EquipmentPlacement.equipment_id.in_([s.id for s, _ in sensors]), EquipmentPlacement.effective_to.is_(None)))
            ).scalars()
        }
        bad = [a.asset_tag for s, a in sensors if (p := placements.get(s.id)) is None or p.rack_id not in rack_by_id
               or rack_by_id[p.rack_id].site_id != s.site_id or rack_by_id[p.rack_id].room_id != p.room_id]
        racks_with_sensor = {p.rack_id for p in placements.values()}
        report.add("each sensor is placed in a rack of its own site and room", not bad and len(racks_with_sensor) == len(sensors), f"{len(bad)} misplaced; {len(racks_with_sensor)} racks covered")
        sensor_ids = [s.id for s, _ in sensors]
        mapping_counts = dict(
            (await db.execute(
                select(IntegrationMetricMapping.managed_asset_id, func.count()).where(IntegrationMetricMapping.managed_asset_id.in_(sensor_ids)).group_by(IntegrationMetricMapping.managed_asset_id)
            )).all()
        )
        report.add("each sensor maps temperature and humidity", all(mapping_counts.get(i) == len(SENSOR_METRICS) for i in sensor_ids) and bool(sensor_ids), f"{sum(mapping_counts.get(i) == 2 for i in sensor_ids)}/{len(sensor_ids)} with two mappings")
        latest = dict(
            (await db.execute(
                select(TelemetryReading.managed_asset_id, func.max(TelemetryReading.occurred_at)).where(TelemetryReading.managed_asset_id.in_(sensor_ids)).group_by(TelemetryReading.managed_asset_id)
            )).all()
        )
        stale = [i for i in sensor_ids if latest.get(i) is None or now - latest[i] > timedelta(seconds=900)]
        report.add("sensor readings are fresh (<= 3 poll intervals)", not stale, f"{len(sensor_ids) - len(stale)}/{len(sensor_ids)} fresh" + ("; run `refresh`" if stale else ""))

    # ---- floor plans and heat maps
    rooms: dict[uuid.UUID, list] = {}
    for rack in targets.racks:
        rooms.setdefault(rack.room_id, []).append(rack)
    plan_rows = []
    for room_id, racks in rooms.items():
        plan = (await db.execute(select(FloorPlan).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))).scalar_one_or_none()
        if plan is None:
            report.add(f"room {room_id} has an active plan", False, "none")
            continue
        calibrated = plan.current_calibration_id is not None
        geometry = validate_geometry(plan.room_width_mm or 0, plan.room_height_mm or 0, racks)
        entry: dict = {"room_id": str(room_id), "plan_id": str(plan.id), "calibrated": calibrated, "geometry_problems": len(geometry)}
        report.add(f"plan {plan.id} is calibrated", calibrated, "calibration present" if calibrated else "no calibration; run `apply --steps floorplans`")
        report.add(f"plan {plan.id} rack geometry valid", not geometry, f"{len(racks)} racks, {len(geometry)} problems" + (f": {geometry[:3]}" if geometry else ""))
        if calibrated and require_sensors:
            for metric in HEATMAP_METRICS:
                heat = await build_heat_map(db, room_id=room_id, metric=metric, scope=UNRESTRICTED, now=now)
                grid = heat["grid"]
                cells = 0 if grid is None else sum(1 for v in grid["values"] if v is not None)
                fresh = sum(1 for s in heat["sensors"] if s["state"] == MEASURED_FRESH)
                entry[metric] = {"state": heat["state"], "cells": cells, "fresh_sensors": fresh, "contributing": heat["quality"]["contributing_count"], "reasons": heat["state_reasons"]}
                report.add(f"{metric} heat map for room {room_id} has a non-empty grid", grid is not None and cells > 0, f"state={heat['state']} cells={cells} fresh_sensors={fresh} reasons={heat['state_reasons']}")
        plan_rows.append(entry)
    report.data["plans"] = plan_rows
    return report
