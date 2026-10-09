# ruff: noqa: E501
"""Environmental exception view for a room (Issue #105).

Two sources, kept distinct and never merged into a second alarm store:
  alarm     a persistent alarm already raised by the existing alarm engine (rules on temperature / humidity / any
            registry metric) on a sensor in this room. The row stays owned by the alarm system; this view reads it.
  derived   a condition computed on request from current state: stale / missing / invalid sensor data, unavailable
            cooling units, capacity headroom breach, zone data-quality gaps. Nothing is persisted.
Derived exceptions never raise or clear alarms; if an operator wants a persistent alarm for a metric they create an
alarm rule through the existing API.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope
from app.application.thermal.airflow import load_room_cooling_units
from app.application.thermal.capacity import build_room_capacity, zone_contains
from app.application.thermal.environment import (
    INVALID,
    MEASURED_FRESH,
    MEASURED_STALE,
    MISSING,
    SensorPoint,
    assert_room_visible,
    load_room_sensor_snapshot,
)
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.cooling.models import ThermalZone

MAX_EXCEPTIONS = 500
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
ALL_ROLES = ("ambient", "rack_inlet", "rack_exhaust", "supply_air", "return_air", "other")

# metric -> roles whose sensors are expected to report it
_EXPECTED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("temperature_c", ("ambient", "rack_inlet", "rack_exhaust", "other")),
    ("supply_air_temperature_c", ("supply_air",)),
    ("return_air_temperature_c", ("return_air",)),
    ("humidity_percent", ALL_ROLES),
    ("differential_pressure_pa", ALL_ROLES),
)
_ALARM_TYPES = {
    ("temperature_c", "threshold_high"): "high_temperature",
    ("temperature_c", "threshold_low"): "low_temperature",
    ("humidity_percent", "threshold_high"): "high_humidity",
    ("humidity_percent", "threshold_low"): "low_humidity",
}


def _item(kind: str, severity: str, subject_type: str, subject_id: Any, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": kind, "severity": severity, "subject_type": subject_type, "subject_id": str(subject_id), "message": message,
        "source": extra.pop("source", "derived"), **extra,
    }


async def build_exceptions(
    db: AsyncSession, *, room_id: uuid.UUID, scope: AccessScope, can_read_power: bool, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    await assert_room_visible(db, room_id, scope)
    items: list[dict[str, Any]] = []

    by_sensor: dict[uuid.UUID, dict[str, SensorPoint]] = {}
    sensor_names: dict[uuid.UUID, str] = {}
    for metric, roles in _EXPECTED:
        snap = await load_room_sensor_snapshot(db, room_id=room_id, metric=metric, scope=scope, now=now, roles=roles)
        for p in snap.points:
            by_sensor.setdefault(p.sensor_id, {})[metric] = p
            sensor_names[p.sensor_id] = p.name
    # An airflow sensor is expected to report volume OR velocity; absent only when both are.
    for sensor_id_metric in (("airflow_m3_s", "airflow_velocity_m_s"),):
        vol = await load_room_sensor_snapshot(db, room_id=room_id, metric=sensor_id_metric[0], scope=scope, now=now, roles=ALL_ROLES)
        vel = await load_room_sensor_snapshot(db, room_id=room_id, metric=sensor_id_metric[1], scope=scope, now=now, roles=ALL_ROLES)
        vel_by = {p.sensor_id: p for p in vel.points}
        for p in vol.points:
            other = vel_by.get(p.sensor_id)
            best = p if other is None or p.state in (MEASURED_FRESH,) or other.state == MISSING else other
            by_sensor.setdefault(p.sensor_id, {})["airflow"] = best
            sensor_names[p.sensor_id] = p.name

    for sensor_id, metrics in sorted(by_sensor.items(), key=lambda kv: str(kv[0])):
        for metric, p in sorted(metrics.items()):
            if p.state == MEASURED_STALE:
                items.append(_item("stale_sensor", "warning", "sensor", sensor_id, f"{p.name}: last {metric} reading is {p.age_seconds}s old.",
                                   metric=metric, age_seconds=p.age_seconds, occurred_at=p.occurred_at.isoformat() if p.occurred_at else None))
            elif p.state == MISSING:
                items.append(_item("missing_sensor_data", "warning", "sensor", sensor_id, f"{p.name}: no {metric} reading received.", metric=metric))
            elif p.state == INVALID:
                items.append(_item("invalid_sensor_data", "warning", "sensor", sensor_id, f"{p.name}: {metric} reading rejected ({p.invalid_reason}).",
                                   metric=metric, reason=p.invalid_reason))
            if not p.located:
                items.append(_item("sensor_not_located", "info", "sensor", sensor_id, f"{p.name} has no resolvable location, so it is not on the map.", metric=metric))

    ids = list(by_sensor)
    if ids:
        for alarm, rule in (
            await db.execute(
                select(Alarm, AlarmRule).join(AlarmRule, AlarmRule.id == Alarm.rule_id)
                .where(Alarm.managed_asset_id.in_(ids), Alarm.status.in_(("ACTIVE", "ACKNOWLEDGED")))
            )
        ).all():
            kind = _ALARM_TYPES.get((rule.metric, rule.rule_type), "environment_alarm")
            items.append(
                _item(kind, "critical", "sensor", alarm.managed_asset_id, f"{rule.name}: alarm {alarm.status.lower()} since {alarm.opened_at.isoformat()}.",
                      source="alarm", alarm_id=str(alarm.id), rule_id=str(rule.id), metric=rule.metric, status=alarm.status,
                      value=float(alarm.last_value), opened_at=alarm.opened_at.isoformat())
            )

    for unit, asset, placement in await load_room_cooling_units(db, room_id, scope):
        available = asset.lifecycle_status in ("installed", "active") and unit.operating_status in ("online", "standby")
        if not available:
            items.append(_item("cooling_unit_unavailable", "critical", "cooling_unit", unit.id,
                               f"{unit.name} ({unit.unit_kind.upper()}) is {unit.operating_status} / {asset.lifecycle_status} and provides no capacity.",
                               operating_status=unit.operating_status, lifecycle_status=asset.lifecycle_status))
        if placement.x_mm is None:
            items.append(_item("cooling_unit_not_located", "info", "cooling_unit", unit.id, f"{unit.name} has no position on the floor plan."))

    capacity = await build_room_capacity(db, room_id=room_id, scope=scope, can_read_power=can_read_power, now=now)
    for zone in capacity["zones"]:
        over = zone["headroom_kw"] is not None and zone["utilization_pct"] is not None and (
            zone["headroom_kw"] < 0 or zone["utilization_pct"] >= capacity["thresholds"]["warning_utilization_pct"]
        )
        if over:
            items.append(_item("capacity_headroom_breach", "critical" if zone["headroom_kw"] < 0 or zone["utilization_pct"] >= capacity["thresholds"]["critical_utilization_pct"] else "warning", "thermal_zone", zone["zone_id"],
                               f"{zone['name']}: cooling utilisation {zone['utilization_pct']}% (headroom {zone['headroom_kw']} kW).",
                               headroom_kw=zone["headroom_kw"], utilization_pct=zone["utilization_pct"]))
        if zone["redundancy"]["state"] in ("degraded", "unavailable", "single_unit"):
            items.append(_item("cooling_redundancy_lost" if zone["redundancy"]["state"] != "single_unit" else "cooling_single_unit",
                               "critical" if zone["redundancy"]["state"] == "unavailable" else "warning", "thermal_zone", zone["zone_id"],
                               f"{zone['name']}: redundancy is {zone['redundancy']['state']}.", redundancy=zone["redundancy"]["state"]))
        if zone["level"] == "unknown":
            items.append(_item("capacity_inputs_incomplete", "info", "thermal_zone", zone["zone_id"],
                               f"{zone['name']}: headroom cannot be computed (capacity or load inputs are incomplete).",
                               load_state=zone["thermal_load"]["state"], available_complete=zone["available_complete"]))
        if zone["level"] == "not_configured":
            items.append(_item("no_cooling_assigned", "warning", "thermal_zone", zone["zone_id"], f"{zone['name']}: no CRAC/CRAH is assigned."))

    zones = (await db.execute(select(ThermalZone).where(ThermalZone.room_id == room_id, ThermalZone.retired.is_(False)))).scalars().all()
    temp = {sid: m["temperature_c"] for sid, m in by_sensor.items() if "temperature_c" in m}

    for zone in sorted(zones, key=lambda z: str(z.id)):
        if zone.zone_kind not in ("hot_aisle", "cold_aisle", "served_zone"):
            continue
        inside = [p for p in temp.values() if p.located and zone_contains(zone, p.x_mm or 0.0, p.y_mm or 0.0)]
        fresh = [p for p in inside if p.state == MEASURED_FRESH]
        if not inside:
            items.append(_item("zone_data_quality", "warning", "thermal_zone", zone.id, f"{zone.name}: no temperature sensor lies inside this zone.",
                               sensors_inside=0, fresh_inside=0))
        elif not fresh:
            items.append(_item("zone_data_quality", "warning", "thermal_zone", zone.id, f"{zone.name}: none of its {len(inside)} temperature sensor(s) has fresh data.",
                               sensors_inside=len(inside), fresh_inside=0))
    items.sort(key=lambda i: (_SEVERITY_RANK[i["severity"]], i["type"], i["subject_id"], i.get("metric", "")))
    truncated = len(items) > MAX_EXCEPTIONS
    items = items[:MAX_EXCEPTIONS]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "room_id": str(room_id), "generated_at": now.isoformat(), "items": items, "counts": counts, "truncated": truncated,
        "sources": {"alarm": "Persistent alarms from the existing alarm engine.", "derived": "Computed on request; not persisted."},
    }
