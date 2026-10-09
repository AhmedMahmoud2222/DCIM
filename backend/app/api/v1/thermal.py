# ruff: noqa: E501
"""Derived read-only thermal views for the calibrated spatial twin (Issue #105): layout, current environmental
values, temperature / humidity heat maps, airflow, cooling capacity and environmental exceptions.

A #105 heat map is an operational interpolation / visualisation, not validated CFD.

Authorization (literal codes, checked in this order and all required):
  layout        cooling:read + spatial:read
  environment   cooling:read + spatial:read + telemetry:read
  heat-map      cooling:read + spatial:read + telemetry:read
  airflow       cooling:read + spatial:read + telemetry:read
  capacity      cooling:read (+ power:read to see thermal load; without it load is reported as `not_permitted`)
  exceptions    cooling:read + telemetry:read (+ alarm:read to include persistent alarms)
Source integration identity is added to a response only when the caller also holds integration:read.
Interpolation, quality counts and provenance ids are computed after the caller's scope has been applied to the sensors,
so none of them can reflect a sensor the caller may not see. Site-restricted callers hold none of these permissions
(they are not scope-aware) and are refused before any data is read."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.rbac import AuthContext, get_auth_context
from app.application.thermal import airflow, capacity, exceptions, heatmap
from app.application.thermal.environment import DEFAULT_MAP_ROLES, load_room_sensor_snapshot
from app.application.thermal.heatmap import load_room_extent, parse_as_of, serialize_point
from app.core.errors import ApiError, ForbiddenError, NotFoundError
from app.domain.cooling.models import ContainmentElement, CoolingUnitZone, ThermalZone
from app.domain.telemetry.registry import METRIC_REGISTRY

router = APIRouter(prefix="/cooling/rooms", tags=["cooling"])

ENVIRONMENT_METRICS = (
    "temperature_c", "humidity_percent", "supply_air_temperature_c", "return_air_temperature_c", "airflow_m3_s",
    "airflow_velocity_m_s", "differential_pressure_pa",
)
SENSOR_ROLES = ("ambient", "rack_inlet", "rack_exhaust", "supply_air", "return_air", "other")


def _need(ctx: AuthContext, *codes: str) -> None:
    for code in codes:
        # literal checks per code keep the static permission allow-list test able to see every code used
        if code == "cooling:read" and not ctx.has_permission("cooling:read"):
            raise ForbiddenError("Missing required permission: cooling:read")
        if code == "spatial:read" and not ctx.has_permission("spatial:read"):
            raise ForbiddenError("Missing required permission: spatial:read")
        if code == "telemetry:read" and not ctx.has_permission("telemetry:read"):
            raise ForbiddenError("Missing required permission: telemetry:read")


async def _require_room(db: AsyncSession, ctx: AuthContext, room_id: uuid.UUID) -> uuid.UUID:
    site_id = await capacity.room_site_id(db, room_id)
    if site_id is None or not ctx.scope.allows_site(site_id):
        raise NotFoundError(f"Room {room_id} not found.")
    return site_id


def _roles(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_MAP_ROLES
    roles = tuple(dict.fromkeys(r.strip() for r in raw.split(",") if r.strip()))
    bad = [r for r in roles if r not in SENSOR_ROLES]
    if bad or not roles:
        raise ApiError(status_code=422, title="Validation Error", detail=f"sensor_roles must be a subset of {list(SENSOR_ROLES)}.")
    return roles


def _include_source(ctx: AuthContext) -> bool:
    return ctx.has_permission("integration:read")


@router.get("/{room_id}/layout")
async def get_layout(room_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)) -> dict:
    """Cooling units, sensors (positions only, no values), zones, containment and unit-zone relationships for a room."""
    _need(ctx, "cooling:read", "spatial:read")
    await _require_room(db, ctx, room_id)
    extent = await load_room_extent(db, room_id)
    zones = (await db.execute(select(ThermalZone).where(ThermalZone.room_id == room_id, ThermalZone.retired.is_(False)).order_by(ThermalZone.name, ThermalZone.id))).scalars().all()
    elements: dict[uuid.UUID, list[dict]] = {}
    if zones:
        for e in (await db.execute(select(ContainmentElement).where(ContainmentElement.thermal_zone_id.in_([z.id for z in zones])).order_by(ContainmentElement.id))).scalars():
            elements.setdefault(e.thermal_zone_id, []).append(
                {"id": str(e.id), "element_kind": e.element_kind, "x1_mm": e.x1_mm, "y1_mm": e.y1_mm, "x2_mm": e.x2_mm, "y2_mm": e.y2_mm, "label": e.label}
            )
    units = await airflow.load_room_cooling_units(db, room_id, ctx.scope)
    relations = []
    if units or zones:
        rel_rows = (
            await db.execute(
                select(CoolingUnitZone).where(
                    CoolingUnitZone.thermal_zone_id.in_([z.id for z in zones]) if zones else CoolingUnitZone.cooling_unit_id.in_([u.id for u, _, _ in units])
                ).order_by(CoolingUnitZone.id)
            )
        ).scalars().all()
        visible_units = {u.id for u, _, _ in units}
        relations = [
            {"id": str(r.id), "cooling_unit_id": str(r.cooling_unit_id), "thermal_zone_id": str(r.thermal_zone_id), "relation_kind": r.relation_kind, "semantics": r.semantics}
            for r in rel_rows
            if r.cooling_unit_id in visible_units or ctx.scope.unrestricted
        ]
    sensors = {}
    for metric in ("temperature_c", "humidity_percent", "airflow_m3_s", "differential_pressure_pa"):
        snap = await load_room_sensor_snapshot(db, room_id=room_id, metric=metric, scope=ctx.scope, roles=SENSOR_ROLES)
        for p in snap.points:
            sensors.setdefault(p.sensor_id, {
                "id": str(p.sensor_id), "asset_tag": p.asset_tag, "name": p.name, "sensor_kind": p.sensor_kind, "measurement_role": p.measurement_role,
                "placement_type": p.placement_type, "x_mm": p.x_mm, "y_mm": p.y_mm, "position_source": p.position_source, "position_exact": p.position_exact,
                "lifecycle_status": p.lifecycle_status,
            })
    return {
        "room_id": str(room_id), "room_name": extent.room_name, "generated_at": datetime.now(UTC).isoformat(),
        "floor_plan_id": str(extent.floor_plan_id) if extent.floor_plan_id else None, "calibration_id": str(extent.calibration_id) if extent.calibration_id else None,
        "layout_reasons": extent.reasons,
        "zones": [
            {
                "id": str(z.id), "name": z.name, "zone_kind": z.zone_kind, "containment": z.containment, "geometry_type": z.geometry_type, "x_mm": z.x_mm,
                "y_mm": z.y_mm, "width_mm": z.width_mm, "height_mm": z.height_mm, "points": z.points, "version": z.version,
                "authority": "operator_configured", "elements": elements.get(z.id, []),
            }
            for z in zones
        ],
        "cooling_units": [
            {
                "id": str(u.id), "asset_tag": a.asset_tag, "name": u.name, "unit_kind": u.unit_kind, "operating_status": u.operating_status,
                "lifecycle_status": a.lifecycle_status, "x_mm": p.x_mm, "y_mm": p.y_mm, "rotation_deg": p.rotation_deg,
                "supply_direction_deg": u.supply_direction_deg,
                "rated_cooling_capacity_kw": float(u.rated_cooling_capacity_kw) if u.rated_cooling_capacity_kw is not None else None,
            }
            for u, a, p in units
        ],
        "sensors": sorted(sensors.values(), key=lambda s: s["id"]),
        "relations": relations,
    }


@router.get("/{room_id}/environment")
async def get_environment(
    room_id: uuid.UUID,
    metric: str = Query("temperature_c"),
    as_of: datetime | None = Query(default=None, description="Snapshot instant (UTC); defaults to now and never exceeds it."),
    sensor_roles: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> dict:
    """Current value of every expected sensor for one metric, with freshness state. The single current-value contract
    behind the heat maps and the environment overlay."""
    _need(ctx, "cooling:read", "spatial:read", "telemetry:read")
    if metric not in ENVIRONMENT_METRICS:
        raise ApiError(status_code=422, title="Validation Error", detail=f"metric must be one of {list(ENVIRONMENT_METRICS)}.")
    await _require_room(db, ctx, room_id)
    now = datetime.now(UTC)
    snapshot = await load_room_sensor_snapshot(
        db, room_id=room_id, metric=metric, scope=ctx.scope, now=now, as_of=parse_as_of(as_of, now),
        roles=_roles(sensor_roles) if sensor_roles else SENSOR_ROLES,
    )
    include_source = _include_source(ctx)
    definition = METRIC_REGISTRY[metric]
    return {
        "room_id": str(room_id), "metric": metric, "unit": definition.canonical_unit, "presentation_unit": definition.presentation_unit,
        "generated_at": now.isoformat(), "as_of": snapshot.as_of.isoformat(), "value_provenance": "measured",
        "counts": {
            "total": len(snapshot.points), "measured_fresh": snapshot.count("measured_fresh"), "measured_stale": snapshot.count("measured_stale"),
            "missing": snapshot.count("missing"), "invalid": snapshot.count("invalid"),
        },
        "points": [serialize_point(p, include_source=include_source) for p in snapshot.points], "truncated": snapshot.truncated,
    }


@router.get("/{room_id}/heat-map")
async def get_heat_map(
    room_id: uuid.UUID,
    metric: Literal["temperature_c", "humidity_percent"] = Query("temperature_c"),
    as_of: datetime | None = Query(default=None),
    cell_mm: int | None = Query(default=None, ge=100, le=5000),
    radius_mm: int | None = Query(default=None, ge=500, le=20000),
    max_skew_seconds: int | None = Query(default=None, ge=60, le=3600),
    sensor_roles: str | None = Query(default=None, description="comma-separated measurement roles; default ambient,rack_inlet,other"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> dict:
    """Operational interpolation of sensor readings for the calibrated room (IDW, documented). Not validated CFD."""
    _need(ctx, "cooling:read", "spatial:read", "telemetry:read")
    await _require_room(db, ctx, room_id)
    now = datetime.now(UTC)
    return await heatmap.build_heat_map(
        db, room_id=room_id, metric=metric, scope=ctx.scope, include_source=_include_source(ctx), now=now, as_of=parse_as_of(as_of, now),
        cell_mm=cell_mm, radius_mm=radius_mm, max_skew_seconds=max_skew_seconds, roles=_roles(sensor_roles),
    )


@router.get("/{room_id}/airflow")
async def get_airflow(room_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)) -> dict:
    _need(ctx, "cooling:read", "spatial:read", "telemetry:read")
    await _require_room(db, ctx, room_id)
    return await airflow.build_airflow(db, room_id=room_id, scope=ctx.scope, include_source=_include_source(ctx))


@router.get("/{room_id}/capacity")
async def get_capacity(room_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)) -> dict:
    _need(ctx, "cooling:read")
    await _require_room(db, ctx, room_id)
    return await capacity.build_room_capacity(db, room_id=room_id, scope=ctx.scope, can_read_power=ctx.has_permission("power:read"))


@router.get("/{room_id}/exceptions")
async def get_exceptions(room_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)) -> dict:
    _need(ctx, "cooling:read", "telemetry:read")
    await _require_room(db, ctx, room_id)
    result = await exceptions.build_exceptions(db, room_id=room_id, scope=ctx.scope, can_read_power=ctx.has_permission("power:read"))
    if not ctx.has_permission("alarm:read"):
        result["items"] = [i for i in result["items"] if i["source"] != "alarm"]
        result["counts"] = {}
        for item in result["items"]:
            result["counts"][item["severity"]] = result["counts"].get(item["severity"], 0) + 1
        result["alarms_omitted"] = "alarm:read is required to include persistent alarms"
    return result
