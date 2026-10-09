# ruff: noqa: E501
"""Airflow visualisation data (Issue #105). Only elements backed by real inputs are emitted, each with provenance.

  cooling_supply   a CRAC/CRAH with a configured supply direction. Direction and magnitude are *configured*
                   (design airflow capacity); no live fan telemetry is implied.
  sensor_airflow   an airflow sensor with a configured axis orientation. Magnitude is *measured* (with freshness
                   state); direction is the *configured* orientation of the sensor, not a measured direction.

Not emitted: any inferred path, streamline, return-air path or vector derived from geometry. `modelled` provenance
is reserved for the CFD work package (#106) and nothing in #105 produces it. Elements with a missing direction or
missing magnitude are listed with `drawable: false` and a reason rather than being given a default.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, equipment_visible_clause
from app.application.thermal.environment import (
    MEASURED_FRESH,
    MEASURED_STALE,
    SensorPoint,
    assert_room_visible,
    load_room_sensor_snapshot,
)
from app.application.thermal.heatmap import serialize_point
from app.domain.cooling.models import CoolingUnit, CoolingUnitZone, ThermalZone
from app.domain.identity.models import ManagedAsset
from app.domain.placement.models import EquipmentPlacement

AIRFLOW_ROLES = ("ambient", "rack_inlet", "rack_exhaust", "supply_air", "return_air", "other")
NO_MODELLED_NOTE = "No modelled airflow is generated in Issue #105. Streamlines and predicted flow belong to the CFD work package (#106)."


async def load_room_cooling_units(
    db: AsyncSession, room_id: uuid.UUID, scope: AccessScope
) -> list[tuple[CoolingUnit, ManagedAsset, EquipmentPlacement]]:
    rows = (
        await db.execute(
            select(CoolingUnit, ManagedAsset, EquipmentPlacement)
            .join(ManagedAsset, ManagedAsset.id == CoolingUnit.id)
            .join(EquipmentPlacement, EquipmentPlacement.equipment_id == CoolingUnit.id)
            .where(
                EquipmentPlacement.room_id == room_id, EquipmentPlacement.effective_to.is_(None),
                equipment_visible_clause(scope, EquipmentPlacement.equipment_id),
            )
            .order_by(CoolingUnit.id)
        )
    ).all()
    return [(u, a, p) for u, a, p in rows]


def _sensor_element(volume: SensorPoint | None, velocity: SensorPoint | None, include_source: bool) -> dict[str, Any]:
    base = volume or velocity
    assert base is not None
    reasons: list[str] = []
    if base.flow_direction_deg is None:
        reasons.append("no_configured_direction")
    if not base.located:
        reasons.append("no_location")
    measured = [p for p in (volume, velocity) if p is not None and p.state in (MEASURED_FRESH, MEASURED_STALE)]
    if not measured:
        reasons.append("no_measured_magnitude")
    fresh = any(p.state == MEASURED_FRESH for p in measured)
    return {
        "id": str(base.sensor_id), "kind": "sensor_airflow", "name": base.name, "x_mm": base.x_mm, "y_mm": base.y_mm,
        "position_exact": base.position_exact, "direction_deg": base.flow_direction_deg, "direction_provenance": "configured",
        "magnitude_provenance": "measured" if measured else "none",
        "volume_flow": serialize_point(volume, include_source=include_source) if volume else None,
        "velocity": serialize_point(velocity, include_source=include_source) if velocity else None,
        "state": MEASURED_FRESH if fresh else (MEASURED_STALE if measured else "missing"),
        "drawable": not reasons, "not_drawable_reasons": reasons,
    }


async def build_airflow(
    db: AsyncSession, *, room_id: uuid.UUID, scope: AccessScope, include_source: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    await assert_room_visible(db, room_id, scope)
    volume_snap = await load_room_sensor_snapshot(db, room_id=room_id, metric="airflow_m3_s", scope=scope, now=now, roles=AIRFLOW_ROLES)
    velocity_snap = await load_room_sensor_snapshot(db, room_id=room_id, metric="airflow_velocity_m_s", scope=scope, now=now, roles=AIRFLOW_ROLES)
    volume = {p.sensor_id: p for p in volume_snap.points}
    velocity = {p.sensor_id: p for p in velocity_snap.points}
    elements: list[dict[str, Any]] = [
        _sensor_element(volume.get(sid), velocity.get(sid), include_source) for sid in sorted(set(volume) | set(velocity), key=str)
    ]

    units = await load_room_cooling_units(db, room_id, scope)
    zones_by_unit: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if units:
        for unit_id, zone_id, zone_name, relation, semantics in (
            await db.execute(
                select(CoolingUnitZone.cooling_unit_id, ThermalZone.id, ThermalZone.name, CoolingUnitZone.relation_kind, CoolingUnitZone.semantics)
                .join(ThermalZone, ThermalZone.id == CoolingUnitZone.thermal_zone_id)
                .where(CoolingUnitZone.cooling_unit_id.in_([u.id for u, _, _ in units]), ThermalZone.room_id == room_id)
            )
        ).all():
            zones_by_unit.setdefault(unit_id, []).append(
                {"zone_id": str(zone_id), "zone_name": zone_name, "relation": relation, "semantics": semantics}
            )
    for unit, asset, placement in units:
        if unit.unit_kind == "chiller":
            continue
        reasons = []
        if unit.supply_direction_deg is None:
            reasons.append("no_configured_direction")
        if placement.x_mm is None or placement.y_mm is None:
            reasons.append("no_location")
        if unit.airflow_capacity_m3_s is None:
            reasons.append("no_design_airflow")
        elements.append(
            {
                "id": str(unit.id), "kind": "cooling_supply", "name": unit.name, "unit_kind": unit.unit_kind,
                "x_mm": placement.x_mm, "y_mm": placement.y_mm, "position_exact": placement.x_mm is not None,
                "direction_deg": unit.supply_direction_deg, "direction_provenance": "configured",
                "magnitude_m3_s": float(unit.airflow_capacity_m3_s) if unit.airflow_capacity_m3_s is not None else None,
                "magnitude_provenance": "configured_design" if unit.airflow_capacity_m3_s is not None else "none",
                "operating_status": unit.operating_status, "lifecycle_status": asset.lifecycle_status,
                "state": "configured", "drawable": not reasons, "not_drawable_reasons": reasons,
                "zones": zones_by_unit.get(unit.id, []),
            }
        )
    return {
        "room_id": str(room_id), "generated_at": now.isoformat(), "elements": elements,
        # counts describe what is drawn; an element that lacks a direction or a magnitude is counted separately
        "provenance_summary": {
            "measured_magnitude": sum(1 for e in elements if e["drawable"] and e.get("magnitude_provenance") == "measured"),
            "configured_design": sum(1 for e in elements if e["drawable"] and e.get("magnitude_provenance") == "configured_design"),
            "modelled": 0,
            "not_drawable": sum(1 for e in elements if not e["drawable"]),
        },
        "note": NO_MODELLED_NOTE,
        "disclaimer": "Arrows show configured direction and either measured or design magnitude at a point. They are not a CFD result "
        "and do not show flow paths between points.",
    }
