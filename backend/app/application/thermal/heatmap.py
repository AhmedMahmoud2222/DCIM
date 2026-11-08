# ruff: noqa: E501
"""Temperature / humidity heat maps for the calibrated spatial twin (Issue #105).

A heat map is an *operational interpolation of sensor readings*, not validated CFD and not a measured field.
Measured sensor points and interpolated cells are separate arrays with separate provenance. Interpolation runs only
over sensors the caller is authorised to see, only over fresh, located, in-room readings of one metric, and only when
the readings are close enough in time. When those conditions fail the map is returned as `degraded` or
`unavailable` with the reason, and no field is drawn.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope
from app.application.spatial_import.boundary import load_boundary
from app.application.thermal import interpolation as idw
from app.application.thermal.environment import (
    DEFAULT_MAP_ROLES,
    FRESH_POLL_MULTIPLE,
    INVALID,
    MEASURED_FRESH,
    MEASURED_STALE,
    MISSING,
    SensorPoint,
    SensorSnapshot,
    assert_room_visible,
    load_room_sensor_snapshot,
)
from app.core.errors import NotFoundError
from app.domain.alarm.models import AlarmRule
from app.domain.location.models import Room
from app.domain.spatial.models import FloorPlan
from app.domain.telemetry.registry import METRIC_REGISTRY

HEATMAP_METRICS = ("temperature_c", "humidity_percent")
DEFAULT_MAX_SKEW_SECONDS = 900
MIN_MAX_SKEW_SECONDS = 60
MAX_MAX_SKEW_SECONDS = 3600
SENSOR_BOUNDARY_TOLERANCE_MM = 5.0
DISCLAIMER = (
    "Operational interpolation of sensor readings for visualisation. It is not validated CFD, not a measured field, "
    "and interpolated cells must not be treated as measurements."
)

_ASSUMPTIONS = {
    "common": [
        "Inverse-distance weighting (power 2) over fresh, located sensors inside the room; cells with fewer than two sensors "
        "within the influence radius are left empty.",
        "The field is two-dimensional. Sensor height, stratification and three-dimensional air movement are not modelled.",
        "Walls, containment and racks do not block influence. The map says nothing about whether air or heat crosses them.",
        "Stale, missing, invalid and unlocated sensors are excluded from the field, not down-weighted.",
        "Interpolated values are derived on request, never stored, and never written back to telemetry.",
    ],
    "humidity_percent": [
        "Relative humidity depends on temperature. Humidity is interpolated directly from humidity sensors; it is not "
        "recomputed from a temperature field, so cells between sensors at different temperatures are approximate.",
    ],
}


@dataclass
class RoomExtent:
    room_id: uuid.UUID
    room_name: str
    floor_plan_id: uuid.UUID | None = None
    floor_plan_revision: int | None = None
    calibration_id: uuid.UUID | None = None
    polygon: list[idw.Point] | None = None
    reasons: list[str] = field(default_factory=list)


async def load_room_extent(db: AsyncSession, room_id: uuid.UUID, scope: AccessScope | None = None) -> RoomExtent:
    if scope is not None:
        await assert_room_visible(db, room_id, scope)
    room = await db.get(Room, room_id)
    if room is None:
        raise NotFoundError(f"Room {room_id} not found.")
    extent = RoomExtent(room_id=room.id, room_name=room.name)
    plan = (await db.execute(select(FloorPlan).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))).scalar_one_or_none()
    if plan is None:
        extent.reasons.append("no_active_floor_plan")
        return extent
    extent.floor_plan_id = plan.id
    extent.floor_plan_revision = plan.revision_number
    extent.calibration_id = plan.current_calibration_id
    if plan.current_calibration_id is None:
        extent.reasons.append("no_calibration")
    boundary = await load_boundary(db, plan.id)
    if boundary is not None:
        extent.polygon = boundary
    elif plan.room_width_mm and plan.room_height_mm:
        w, h = float(plan.room_width_mm), float(plan.room_height_mm)
        extent.polygon = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    else:
        extent.reasons.append("no_room_extent")
    return extent


def coverage_class(percent: float) -> str:
    if percent <= 0:
        return "none"
    if percent >= 80:
        return "good"
    return "fair" if percent >= 50 else "poor"


def serialize_point(p: SensorPoint, *, include_source: bool, excluded_reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sensor_id": str(p.sensor_id), "asset_tag": p.asset_tag, "name": p.name, "sensor_kind": p.sensor_kind,
        "measurement_role": p.measurement_role, "metric": p.metric, "placement_type": p.placement_type,
        "rack_id": str(p.rack_id) if p.rack_id else None,
        "x_mm": p.x_mm, "y_mm": p.y_mm, "position_source": p.position_source, "position_exact": p.position_exact,
        "state": p.state,
        "value_provenance": "measured" if p.state in (MEASURED_FRESH, MEASURED_STALE) else "none",
        "value": p.value, "unit": p.unit, "presentation_value": p.presentation_value, "presentation_unit": p.presentation_unit,
        "occurred_at": p.occurred_at.isoformat() if p.occurred_at else None, "age_seconds": p.age_seconds,
        "expected_poll_interval_seconds": p.poll_interval_seconds, "invalid_reason": p.invalid_reason,
        "used_in_field": excluded_reason is None and p.usable_for_field,
        "excluded_reason": excluded_reason,
        "source": {"integration_id": str(p.integration_id), "integration_name": p.integration_name}
        if include_source and p.integration_id is not None else None,
    }
    if p.active_alarm_count is not None:  # only present when the caller may read alarms
        out["active_alarm_count"] = p.active_alarm_count
    return out


async def _thresholds(db: AsyncSession, snapshot: SensorSnapshot) -> list[dict[str, Any]]:
    integration_ids = {p.integration_id for p in snapshot.points if p.integration_id is not None}
    if not integration_ids:
        return []
    rules = (
        await db.execute(
            select(AlarmRule).where(
                AlarmRule.metric == snapshot.metric, AlarmRule.enabled.is_(True), AlarmRule.integration_id.in_(integration_ids),
                AlarmRule.rule_type.in_(("threshold_high", "threshold_low")),
            )
        )
    ).scalars().all()
    seen: dict[tuple[str, float, str | None], dict[str, Any]] = {}
    for rule in sorted(rules, key=lambda r: (r.rule_type, float(r.threshold or 0), str(r.id))):
        key = (rule.rule_type, float(rule.threshold or 0), rule.unit)
        seen.setdefault(key, {"rule_type": rule.rule_type, "threshold": float(rule.threshold or 0), "unit": rule.unit, "name": rule.name})
    return list(seen.values())


async def build_heat_map(
    db: AsyncSession,
    *,
    room_id: uuid.UUID,
    metric: str,
    scope: AccessScope,
    include_source: bool = False,
    include_alarms: bool = False,
    now: datetime | None = None,
    as_of: datetime | None = None,
    cell_mm: int | None = None,
    radius_mm: int | None = None,
    max_skew_seconds: int | None = None,
    roles: tuple[str, ...] = DEFAULT_MAP_ROLES,
) -> dict[str, Any]:
    if metric not in HEATMAP_METRICS:
        raise ValueError(f"No heat map is defined for {metric!r}.")
    now = now or datetime.now(UTC)
    skew_limit = max(MIN_MAX_SKEW_SECONDS, min(MAX_MAX_SKEW_SECONDS, max_skew_seconds or DEFAULT_MAX_SKEW_SECONDS))
    extent = await load_room_extent(db, room_id, scope)
    snapshot = await load_room_sensor_snapshot(db, room_id=room_id, metric=metric, scope=scope, now=now, as_of=as_of, roles=roles, include_alarms=include_alarms)
    definition = METRIC_REGISTRY[metric]

    reasons: list[str] = list(extent.reasons)
    excluded: dict[uuid.UUID, str] = {}
    candidates: list[SensorPoint] = []
    for point in snapshot.points:
        if not point.located:
            excluded[point.sensor_id] = "no_location"
        elif extent.polygon is not None and idw.distance_to_polygon_mm(point.x_mm or 0.0, point.y_mm or 0.0, extent.polygon) > SENSOR_BOUNDARY_TOLERANCE_MM:
            excluded[point.sensor_id] = "outside_room_boundary"
        elif point.state != MEASURED_FRESH:
            excluded[point.sensor_id] = point.state
        else:
            candidates.append(point)

    fresh_times = [p.occurred_at for p in candidates if p.occurred_at is not None]
    skew = int((max(fresh_times) - min(fresh_times)).total_seconds()) if len(fresh_times) > 1 else 0
    field_grid: idw.GridField | None = None
    if len(candidates) > idw.MAX_SENSORS_PER_MAP:
        reasons.append("sensor_limit_exceeded")
    elif len(candidates) < idw.MIN_SENSORS_FOR_FIELD:
        reasons.append("insufficient_fresh_sensors")
    elif skew > skew_limit:
        reasons.append("age_skew_exceeded")
    elif extent.polygon is not None and not (set(reasons) & {"no_active_floor_plan", "no_calibration", "no_room_extent"}):
        samples = [idw.FieldSample(str(p.sensor_id), p.x_mm or 0.0, p.y_mm or 0.0, p.value or 0.0) for p in candidates]
        field_grid = idw.idw_field(samples, extent.polygon, cell_mm=cell_mm, radius_mm=radius_mm)

    stale = snapshot.count(MEASURED_STALE)
    missing = snapshot.count(MISSING)
    invalid = snapshot.count(INVALID)
    fresh = snapshot.count(MEASURED_FRESH)
    unlocated = sum(1 for v in excluded.values() if v == "no_location")
    if field_grid is not None:
        state = "healthy" if not (stale or missing or invalid or unlocated or excluded) else "partial"
        if state == "partial":
            reasons.append("some_expected_sensors_not_contributing")
    elif "age_skew_exceeded" in reasons:
        state = "degraded"
    else:
        state = "unavailable"
    if snapshot.truncated:
        reasons.append("sensor_list_truncated")
        if state == "healthy":
            state = "partial"

    times = [p.occurred_at for p in snapshot.points if p.occurred_at is not None]
    coverage = field_grid.coverage_percent if field_grid is not None else 0.0
    sensors = [serialize_point(p, include_source=include_source, excluded_reason=excluded.get(p.sensor_id)) for p in snapshot.points]
    for item in sensors:
        item["used_in_field"] = item["used_in_field"] and field_grid is not None
    return {
        "room_id": str(room_id), "room_name": extent.room_name, "metric": metric, "unit": definition.canonical_unit,
        "presentation_unit": definition.presentation_unit,
        "kind": "interpolated_operational_estimate", "disclaimer": DISCLAIMER,
        "generated_at": now.isoformat(), "as_of": snapshot.as_of.isoformat(),
        "floor_plan_id": str(extent.floor_plan_id) if extent.floor_plan_id else None,
        "floor_plan_revision": extent.floor_plan_revision,
        "calibration_id": str(extent.calibration_id) if extent.calibration_id else None,
        "state": state, "state_reasons": reasons,
        "quality": {
            "sensor_count": len(snapshot.points), "fresh_count": fresh, "stale_count": stale, "missing_count": missing,
            "invalid_count": invalid, "unlocated_count": unlocated, "contributing_count": len(candidates) if field_grid else 0,
            "coverage_percent": coverage, "coverage_class": coverage_class(coverage),
            "interpolation_coverage_percent": coverage,
            "source_time_range": {"oldest": min(times).isoformat(), "newest": max(times).isoformat()} if times else None,
            "max_age_skew_seconds": skew, "max_age_skew_allowed_seconds": skew_limit,
            "freshness_cutoff": f"{FRESH_POLL_MULTIPLE} x integration poll interval",
        },
        "method": {
            "name": "idw", "power": idw.IDW_POWER, "radius_mm": idw.clamp_radius(radius_mm),
            "cell_mm": field_grid.cell_mm if field_grid else None, "min_sensors": idw.MIN_SENSORS_FOR_FIELD,
            "min_neighbours_per_cell": idw.MIN_NEIGHBOURS_PER_CELL, "stale_policy": "excluded", "missing_policy": "excluded",
            "barrier_aware": False, "dimensions": 2,
            "limits": {
                "max_sensors": idw.MAX_SENSORS_PER_MAP, "max_grid_dimension": idw.MAX_GRID_DIM,
                "max_radius_mm": idw.MAX_RADIUS_MM, "min_radius_mm": idw.MIN_RADIUS_MM, "min_cell_mm": idw.MIN_CELL_MM,
            },
            "assumptions": _ASSUMPTIONS["common"] + _ASSUMPTIONS.get(metric, []),
        },
        "grid": None if field_grid is None else {
            "value_provenance": "interpolated", "origin_x_mm": field_grid.origin_x_mm, "origin_y_mm": field_grid.origin_y_mm,
            "cell_mm": field_grid.cell_mm, "columns": field_grid.columns, "rows": field_grid.rows, "unit": definition.canonical_unit,
            "min": field_grid.min_value, "max": field_grid.max_value, "values": field_grid.values, "support": field_grid.support,
        },
        "sensors": sensors,
        "source_set": {
            "contributing_sensor_ids": sorted(str(p.sensor_id) for p in candidates) if field_grid else [],
            "as_of": snapshot.as_of.isoformat(),
        },
        "thresholds": await _thresholds(db, snapshot) if include_alarms else [],
        "thresholds_withheld": not include_alarms,
        "truncated": snapshot.truncated,
    }


def parse_as_of(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return min(value, now) if value > now + timedelta(seconds=1) else value
