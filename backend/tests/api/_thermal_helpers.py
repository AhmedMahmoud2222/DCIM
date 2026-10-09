"""Fixtures for the Issue #105 cooling / environment tests. Floor plans are seeded directly (an active plan, a
calibration and a room boundary) so the thermal tests do not depend on the DXF/VSDX parser sandbox."""

import uuid
from datetime import UTC, datetime, timedelta

from app.domain.integration.models import Collector, Integration
from app.domain.spatial.models import FloorPlan, FloorPlanCalibration, SpatialLayer, SpatialObject
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading, telemetry_series_key
from tests.api._phase2_helpers import create_rack_model_revision
from tests.api.test_user_groups import _make_site

C = "/api/v1/cooling"
UNITS = {
    "temperature_c": "degC", "humidity_percent": "%", "power_kw": "kW", "supply_air_temperature_c": "degC",
    "return_air_temperature_c": "degC", "airflow_m3_s": "m3/s", "airflow_velocity_m_s": "m/s", "differential_pressure_pa": "Pa",
    "cooling_output_kw": "kW",
}


async def make_world(client, auth_headers):
    admin = await auth_headers("Administrator")
    site = await _make_site(client, admin)
    return admin, site


async def seed_plan(db_session, room_id: str, *, width: int = 6000, height: int = 4000, calibrated: bool = True, boundary: bool = True) -> FloorPlan:
    room = uuid.UUID(room_id)
    plan = FloorPlan(room_id=room, revision_number=1, status="active", room_width_mm=width, room_height_mm=height)
    db_session.add(plan)
    await db_session.flush()
    if calibrated:
        calibration = FloorPlanCalibration(
            floor_plan_id=plan.id, sequence=1, method="declared_units", source_units="mm", mm_per_unit=1, origin_x=0, origin_y=0,
            y_axis="down", rotation_quadrants=0, confidence="high", reference={}, warnings=[],
        )
        db_session.add(calibration)
        await db_session.flush()
        plan.current_calibration_id = calibration.id
    if boundary:
        layer = SpatialLayer(floor_plan_id=plan.id, name="Room outline", layer_type="room_outline", z_order=0)
        db_session.add(layer)
        await db_session.flush()
        db_session.add(
            SpatialObject(spatial_layer_id=layer.id, object_type="room_outline", geometry_type="rect", x_mm=0, y_mm=0, width_mm=width, height_mm=height,
                          rotation_deg=0, label="Room", source="authoritative")
        )
    await db_session.commit()
    return plan


async def make_unit(client, admin, site_id: str, **extra) -> dict:
    body = {
        "unit_kind": "crah", "asset_tag": f"CU-{uuid.uuid4().hex[:8]}", "site_id": site_id, "name": f"Unit-{uuid.uuid4().hex[:4]}",
        "operating_status": "online", **extra,
    }
    response = await client.post(f"{C}/units", json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


async def activate(client, admin, asset_id: str) -> None:
    """planned -> installed -> active through the real lifecycle endpoint."""
    for status in ("installed", "active"):
        response = await client.post(f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": status}, headers=admin)
        assert response.status_code == 200, response.text


async def make_sensor(client, admin, site_id: str, room_id: str | None = None, x: int | None = None, y: int | None = None, *, activate_it: bool = True, **extra) -> dict:
    body = {
        "asset_tag": f"SN-{uuid.uuid4().hex[:8]}", "site_id": site_id, "name": f"Sensor-{uuid.uuid4().hex[:4]}", "sensor_kind": "temperature",
        "measurement_role": "ambient", **extra,
    }
    response = await client.post(f"{C}/sensors", json=body, headers=admin)
    assert response.status_code == 201, response.text
    sensor = response.json()
    if activate_it:
        await activate(client, admin, sensor["id"])
    if room_id is not None:
        placement = {"room_id": room_id, "placement_type": "floor_standing"}
        if x is not None:
            placement.update(x_mm=x, y_mm=y)
        resp = await client.put(f"{C}/sensors/{sensor['id']}/placement", json=placement, headers=admin)
        assert resp.status_code == 200, resp.text
    return sensor


async def make_zone(client, admin, room_id: str, **extra) -> dict:
    body = {"room_id": room_id, "name": f"Zone-{uuid.uuid4().hex[:4]}", "zone_kind": "served_zone", **extra}
    response = await client.post(f"{C}/zones", json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


async def make_group(client, admin, site_id: str, name: str | None = None) -> dict:
    response = await client.post(f"{C}/groups", json={"site_id": site_id, "name": name or f"G-{uuid.uuid4().hex[:5]}"}, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


async def relate(client, admin, unit_id: str, zone_id: str, kind: str = "serves", semantics: str = "configured") -> dict:
    response = await client.post(f"{C}/relations", json={"cooling_unit_id": unit_id, "thermal_zone_id": zone_id, "relation_kind": kind, "semantics": semantics}, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


class TelemetrySeeder:
    """Seeds readings for sensors through one collector/integration per poll interval."""

    def __init__(self, db_session):
        self.db = db_session
        self._integrations: dict[int, Integration] = {}
        self._collector: Collector | None = None
        self._mappings: dict[tuple[uuid.UUID, str], IntegrationMetricMapping] = {}

    async def _integration(self, poll: int) -> Integration:
        if self._collector is None:
            self._collector = Collector(id=uuid.uuid4(), name=f"c-{uuid.uuid4().hex}", collector_type="central", status="active", secret_ciphertext="t", secret_rotated_at=datetime.now(UTC))
            self.db.add(self._collector)
        if poll not in self._integrations:
            integration = Integration(id=uuid.uuid4(), name=f"i-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.1", config={}, poll_interval_seconds=poll)
            self.db.add(integration)
            await self.db.flush()
            self._integrations[poll] = integration
        return self._integrations[poll]

    async def reading(
        self, asset_id: str, metric: str, value: float, *, age_seconds: float = 5, poll: int = 60, unit: str | None = None,
        at: datetime | None = None,
    ) -> TelemetryReading:
        integration = await self._integration(poll)
        unit = unit or UNITS[metric]
        asset = uuid.UUID(asset_id)
        key = (asset, metric)
        if key not in self._mappings:
            mapping = IntegrationMetricMapping(
                id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=asset, source_identifier=f"{asset}:{metric}", canonical_metric=metric,
                unit=unit, scale=1,
            )
            self.db.add(mapping)
            await self.db.flush()
            self._mappings[key] = mapping
        mapping = self._mappings[key]
        occurred = at or (datetime.now(UTC) - timedelta(seconds=age_seconds))
        assert self._collector is not None
        row = TelemetryReading(
            id=uuid.uuid4(), collector_id=self._collector.id, integration_id=integration.id, mapping_id=mapping.id, managed_asset_id=asset,
            external_identifier="x", series_key=telemetry_series_key(integration.id, asset, "x", metric, unit), dedup_key=uuid.uuid4().hex,
            metric=metric, unit=unit, value=value, occurred_at=occurred, received_at=min(occurred, datetime.now(UTC)), attributes={},
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.commit()
        return row


async def rack_model(client, auth_headers) -> str:
    return await create_rack_model_revision(client, auth_headers)
