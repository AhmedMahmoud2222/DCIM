# ruff: noqa: E501
"""Read-only verification of the demo state, scoped to the selected demo sites.

History completeness is checked against what *should* exist, not against whatever survived: for every expected series
(each existing mapping on the routed integrations, both metrics of each rack sensor, both metrics of each device) every
slot of the requested window must be stored under its loader dedup key. `as_of` (the clock the seed was run with) fixes
the window; without it the window ends at the live clock, less one slot that the refresher may not have written yet.
Freshness is a separate check against the live clock and covers both sensor metrics."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import UNRESTRICTED
from app.application.thermal.environment import FRESH_POLL_MULTIPLE, MEASURED_FRESH
from app.application.thermal.heatmap import HEATMAP_METRICS, build_heat_map
from app.domain.cooling.models import EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.placement.models import EquipmentPlacement
from app.domain.spatial.models import FloorPlan
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from app.tools.demo_loader.constants import (
    DEDUP_PREFIX,
    DEFAULT_DEVICE_HISTORY_HOURS,
    DEFAULT_HISTORY_HOURS,
    DEVICE_METRICS,
    MAPPING_LABEL,
    SENSOR_MARKER_KEY,
    SENSOR_METRICS,
    SENSOR_TAG_PREFIX,
    SOURCE_PREFIX,
)
from app.tools.demo_loader.floorplans import validate_geometry
from app.tools.demo_loader.provision import mapping_source, sensor_tag
from app.tools.demo_loader.selection import Targets, check_mapping_ownership
from app.tools.demo_loader.signals import slot_grid

LIVE_GRACE_SLOTS = 1


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


@dataclass
class Expected:
    mapping_id: uuid.UUID
    interval: int
    hours: float
    label: str


async def _expected_series(
    db: AsyncSession, targets: Targets, report: VerifyReport, hours: float, device_hours: float, parts: frozenset[str],
) -> list[Expected]:
    """Every series the seed must have produced for the selected sites, and a failed check for each one that has no mapping."""
    routes = {route.integration.id: route for route in targets.routes.values()}
    interval_of = {iid: route.integration.poll_interval_seconds for iid, route in routes.items()}
    expected: list[Expected] = []
    ownership = await check_mapping_ownership(db, targets)
    existing = (
        await db.execute(
            select(IntegrationMetricMapping.id, IntegrationMetricMapping.integration_id, IntegrationMetricMapping.source_identifier)
            .where(IntegrationMetricMapping.integration_id.in_(list(routes)), ~IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX),
                   IntegrationMetricMapping.id.in_(ownership.allowed))
        )
    ).all()
    if "existing" in parts:
        expected += [Expected(m, interval_of[i], hours, f"existing {src}") for m, i, src in existing]

    sensors = {
        a.asset_tag: s.id
        for s, a in (
            await db.execute(
                select(EnvironmentalSensor, ManagedAsset).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id)
                .where(ManagedAsset.asset_tag.startswith(SENSOR_TAG_PREFIX), ManagedAsset.external_ids.has_key(SENSOR_MARKER_KEY),
                       EnvironmentalSensor.site_id.in_([s.id for s in targets.sites]))
            )
        ).all()
    }
    wanted: dict[tuple[uuid.UUID, str], tuple[str, float, str]] = {}  # (integration, source) -> (label, hours, tag)
    for rack in targets.racks if "sensors" in parts else []:
        tag = sensor_tag(rack.asset_tag)
        for metric in SENSOR_METRICS:
            wanted[(targets.routes[rack.site_id].integration.id, mapping_source(tag, metric))] = (f"sensor {tag} {metric}", hours, tag)
    for device in targets.devices if "devices" in parts else []:
        for metric in DEVICE_METRICS:
            wanted[(targets.routes[device.site_id].integration.id, mapping_source(device.asset_tag, metric))] = (
                f"device {device.asset_tag} {metric}", device_hours, device.asset_tag,
            )
    have = {
        (i, src): m
        for m, i, src in (
            await db.execute(
                select(IntegrationMetricMapping.id, IntegrationMetricMapping.integration_id, IntegrationMetricMapping.source_identifier)
                .where(IntegrationMetricMapping.integration_id.in_(list(routes)), IntegrationMetricMapping.label == MAPPING_LABEL)
            )
        ).all()
    }
    missing_mappings = [label for key, (label, _, _) in wanted.items() if key not in have]
    report.add("every expected mapping exists", not missing_mappings, f"{len(wanted) - len(missing_mappings)}/{len(wanted)} present" + (f"; missing e.g. {missing_mappings[:3]}" if missing_mappings else ""))
    if "sensors" in parts:
        sensor_tags_wanted = {sensor_tag(r.asset_tag) for r in targets.racks}
        report.add("one loader sensor per rack", set(sensors) == sensor_tags_wanted, f"{len(set(sensors) & sensor_tags_wanted)}/{len(sensor_tags_wanted)} racks have their sensor; {len(set(sensors) - sensor_tags_wanted)} unexpected")
    expected += [Expected(have[key], interval_of[key[0]], h, label) for key, (label, h, _) in wanted.items() if key in have]
    return expected


async def verify(
    db: AsyncSession, targets: Targets, *, expected_racks: int | None = None, expected_devices: int | None = None,
    hours: float = DEFAULT_HISTORY_HOURS, device_hours: float = DEFAULT_DEVICE_HISTORY_HOURS, as_of: datetime | None = None,
    now: datetime | None = None, require_sensors: bool = True, parts: frozenset[str] = frozenset({"existing", "sensors", "devices", "floorplans"}),
) -> VerifyReport:
    now = now or datetime.now(UTC)
    report = VerifyReport()
    if expected_racks is not None:
        report.add("rack count unchanged", len(targets.racks) == expected_racks, f"{len(targets.racks)} racks in the selected sites (expected {expected_racks})")
    if expected_devices is not None:
        report.add("equipment count unchanged", len(targets.devices) == expected_devices, f"{len(targets.devices)} equipment in the selected sites (expected {expected_devices})")

    # ---- history: every expected series, every expected slot (selected sites only)
    expected = await _expected_series(db, targets, report, hours, device_hours, parts)
    report.data["expected_series"] = len(expected)
    window_end = as_of or now
    grace = 0 if as_of is not None else LIVE_GRACE_SLOTS
    earliest = window_end - timedelta(hours=max(hours, device_hours) + 1)
    stored: dict[uuid.UUID, set[str]] = {}
    ids = [e.mapping_id for e in expected]
    if ids:
        for mapping_id, key in (
            await db.execute(
                select(TelemetryReading.mapping_id, TelemetryReading.dedup_key)
                .where(TelemetryReading.mapping_id.in_(ids), TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.occurred_at >= earliest)
            )
        ).all():
            stored.setdefault(mapping_id, set()).add(key)
    incomplete: list[str] = []
    missing_total = 0
    for item in expected:
        slots = slot_grid(window_end, item.interval, item.hours)
        slots = slots[: len(slots) - grace] if grace else slots
        have = stored.get(item.mapping_id, set())
        missing = [at for at in slots if f"{DEDUP_PREFIX}{item.mapping_id}:{int(at.timestamp())}" not in have]
        if missing:
            incomplete.append(f"{item.label}: {len(missing)}/{len(slots)} slots missing (first {missing[0].isoformat()})")
            missing_total += len(missing)
    report.add("every expected series has every slot of its window", not incomplete, f"{len(expected) - len(incomplete)}/{len(expected)} complete" + (f"; {missing_total} slots missing, e.g. {incomplete[:3]}" if incomplete else ""))

    scope = TelemetryReading.mapping_id.in_(ids) if ids else TelemetryReading.id.is_(None)
    unflagged = (await db.execute(select(func.count()).select_from(TelemetryReading).where(scope, TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.attributes["synthetic"].astext.is_distinct_from("true")))).scalar_one()
    report.add("every simulated reading is flagged synthetic", unflagged == 0, f"{unflagged} unflagged")
    not_pinned = (await db.execute(select(func.count()).select_from(TelemetryReading).where(scope, TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.contract_evidence.is_distinct_from("pinned")))).scalar_one()
    report.add("every simulated reading is pinned to its mapping revision", not_pinned == 0, f"{not_pinned} not pinned")

    # ---- sensors: lifecycle, placement, ownership, freshness of BOTH metrics (live clock)
    sensor_rows = (
        await db.execute(
            select(EnvironmentalSensor, ManagedAsset).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id)
            .where(ManagedAsset.asset_tag.startswith(SENSOR_TAG_PREFIX), EnvironmentalSensor.site_id.in_([s.id for s in targets.sites]))
        )
    ).all()
    if require_sensors:
        report.add("sensors are combined inlet sensors", all(s.sensor_kind == "combined" and s.measurement_role == "rack_inlet" for s, _ in sensor_rows) and bool(sensor_rows), "kind=combined, role=rack_inlet")
        report.add("sensors are commissioned", all(a.lifecycle_status == "active" for _, a in sensor_rows) and bool(sensor_rows), f"{sum(a.lifecycle_status == 'active' for _, a in sensor_rows)}/{len(sensor_rows)} active")
        rack_by_id = {r.id: r for r in targets.racks}
        sensor_ids = [s.id for s, _ in sensor_rows]
        placements = {
            p.equipment_id: p
            for p in (await db.execute(select(EquipmentPlacement).where(EquipmentPlacement.equipment_id.in_(sensor_ids), EquipmentPlacement.effective_to.is_(None)))).scalars()
        }
        misplaced = [a.asset_tag for s, a in sensor_rows if (p := placements.get(s.id)) is None or p.rack_id not in rack_by_id
                     or rack_by_id[p.rack_id].site_id != s.site_id or rack_by_id[p.rack_id].room_id != p.room_id]
        covered = {p.rack_id for p in placements.values()}
        report.add("each sensor is placed in a rack of its own site and room", not misplaced and covered == set(rack_by_id), f"{len(misplaced)} misplaced; {len(covered & set(rack_by_id))}/{len(rack_by_id)} racks covered")
        latest = {
            (aid, metric): at
            for aid, metric, at in (
                await db.execute(
                    select(TelemetryReading.managed_asset_id, TelemetryReading.metric, func.max(TelemetryReading.occurred_at))
                    .where(TelemetryReading.managed_asset_id.in_(sensor_ids), TelemetryReading.metric.in_(SENSOR_METRICS)).group_by(TelemetryReading.managed_asset_id, TelemetryReading.metric)
                )
            ).all()
        }
        interval = {r.id: targets.routes[r.site_id].integration.poll_interval_seconds for r in targets.racks}
        stale = []
        for s, a in sensor_rows:
            rack_id = placements[s.id].rack_id if s.id in placements else None
            limit = timedelta(seconds=FRESH_POLL_MULTIPLE * interval.get(rack_id, 300)) if rack_id in interval else timedelta(seconds=FRESH_POLL_MULTIPLE * 300)
            for metric in SENSOR_METRICS:
                at = latest.get((s.id, metric))
                if at is None or now - at > limit:
                    stale.append(f"{a.asset_tag}:{metric}")
        report.add("both metrics of every sensor are fresh (<= 3 poll intervals, live clock)", not stale and bool(sensor_rows), f"{len(sensor_rows) * len(SENSOR_METRICS) - len(stale)}/{len(sensor_rows) * len(SENSOR_METRICS)} fresh" + ("; run `refresh`" if stale else ""))

    # ---- floor plans and heat maps
    plan_rows: list[dict] = []
    if "floorplans" in parts:
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
