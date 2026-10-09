"""Issue #105: the real collector telemetry pipeline feeds the thermal views, with the registry converting airflow,
pressure and Fahrenheit sources to canonical units while keeping the raw source value and unit."""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.domain.telemetry.models import TelemetryReading
from tests.api._phase8_helpers import create_integration, register_collector, sign_request
from tests.api._thermal_helpers import C, make_sensor, make_world, seed_plan


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp", poll_interval_seconds=60)
    assigned = await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert assigned.status_code == 201, assigned.text
    return {"admin": admin, "site": site["site"], "room": site["room"], "collector": collector, "integration": integration, "manage": headers}


async def map_metric(client, world, sensor_id, source, metric, unit, scale=1):
    response = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": world["integration"]["id"], "managed_asset_id": sensor_id, "source_identifier": source, "canonical_metric": metric, "unit": unit, "scale": scale},
        headers=world["manage"],
    )
    assert response.status_code == 201, response.text


async def ingest(client, world, records):
    raw = json.dumps({"records": records}).encode()
    headers = sign_request(secret=world["collector"]["secret"], collector_id=uuid.UUID(world["collector"]["id"]), raw_body=raw)
    headers["Content-Type"] = "application/json"
    response = await client.post(f"/api/v1/collectors/{world['collector']['id']}/telemetry", content=raw, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def record(world, source, value, key=None):
    return {
        "dedup_key": key or uuid.uuid4().hex, "integration_id": world["integration"]["id"], "external_identifier": source, "source_identifier": source,
        "occurred_at": datetime.now(UTC).isoformat(), "value": value,
    }


async def test_airflow_pressure_and_fahrenheit_are_converted_at_ingest_and_raw_provenance_is_kept(client, world, db_session):
    flow = await make_sensor(client, world["admin"], world["site"], world["room"], 2000, 1500, sensor_kind="airflow", flow_direction_deg=45, name="Flow")
    dp = await make_sensor(client, world["admin"], world["site"], world["room"], 2500, 1500, sensor_kind="differential_pressure", name="DP")
    temp = await make_sensor(client, world["admin"], world["site"], world["room"], 3000, 1500, name="Temp")
    await map_metric(client, world, flow["id"], "flow-cfm", "airflow_m3_s", "CFM")
    await map_metric(client, world, flow["id"], "vel-fpm", "airflow_velocity_m_s", "ft/min")
    await map_metric(client, world, dp["id"], "dp-inwc", "differential_pressure_pa", "inH2O")
    await map_metric(client, world, temp["id"], "temp-f", "temperature_c", "degF")
    results = await ingest(client, world, [record(world, "flow-cfm", 1000.0), record(world, "vel-fpm", 200.0), record(world, "dp-inwc", 0.1), record(world, "temp-f", 68.0)])
    assert [r["status"] for r in results] == ["accepted"] * 4
    rows = {r.metric: r for r in (await db_session.execute(select(TelemetryReading))).scalars()}
    assert (rows["airflow_m3_s"].unit, rows["airflow_m3_s"].value, rows["airflow_m3_s"].raw_unit, rows["airflow_m3_s"].raw_value) == ("m3/s", Decimal("0.47194744"), "CFM", Decimal("1000"))
    assert (rows["airflow_velocity_m_s"].unit, rows["airflow_velocity_m_s"].value) == ("m/s", Decimal("1.01600000"))
    assert (rows["differential_pressure_pa"].unit, rows["differential_pressure_pa"].value) == ("Pa", Decimal("24.90889100"))
    assert (rows["temperature_c"].unit, rows["temperature_c"].value, rows["temperature_c"].raw_unit) == ("degC", Decimal("20.00000000"), "degF")

    air = (await client.get(f"{C}/rooms/{world['room']}/airflow", headers=world["admin"])).json()
    element = next(e for e in air["elements"] if e["name"] == "Flow")
    assert element["volume_flow"]["value"] == pytest.approx(0.47194744) and element["volume_flow"]["presentation_unit"] == "m3/h"
    assert element["volume_flow"]["presentation_value"] == pytest.approx(1699.0, abs=0.5)  # 1000 CFM in m3/h
    assert element["velocity"]["value"] == pytest.approx(1.016) and element["direction_deg"] == 45 and element["drawable"] is True
    env = (await client.get(f"{C}/rooms/{world['room']}/environment", params={"metric": "differential_pressure_pa"}, headers=world["admin"])).json()
    assert env["points"][0]["value"] == pytest.approx(24.908891) and env["points"][0]["unit"] == "Pa"


async def test_a_heat_map_built_from_pipeline_data_reports_the_integration_poll_interval(client, world, db_session):
    values = [(1000, 1000, 68.0), (5000, 1000, 86.0), (1000, 3000, 75.2), (5000, 3000, 77.0)]  # degF => 20, 30, 24, 25 degC
    for i, (x, y, f) in enumerate(values):
        sensor = await make_sensor(client, world["admin"], world["site"], world["room"], x, y, name=f"T{i}")
        await map_metric(client, world, sensor["id"], f"t{i}", "temperature_c", "degF")
        await ingest(client, world, [record(world, f"t{i}", f)])
    body = (await client.get(f"{C}/rooms/{world['room']}/heat-map", headers=world["admin"])).json()
    assert body["state"] == "healthy"
    assert sorted(round(s["value"], 2) for s in body["sensors"]) == [20.0, 24.0, 25.0, 30.0]
    assert {s["expected_poll_interval_seconds"] for s in body["sensors"]} == {60}
    assert body["grid"]["min"] >= 20.0 and body["grid"]["max"] <= 30.0


async def test_a_sensor_cannot_be_mapped_to_a_metric_it_cannot_measure(client, world):
    humidity = await make_sensor(client, world["admin"], world["site"], world["room"], 100, 100, sensor_kind="humidity")
    response = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": world["integration"]["id"], "managed_asset_id": humidity["id"], "source_identifier": "x", "canonical_metric": "airflow_m3_s", "unit": "m3/s", "scale": 1},
        headers=world["manage"],
    )
    assert response.status_code == 422 and response.json()["title"] == "Metric not supported by sensor"


async def test_wrong_dimension_unit_is_rejected_for_a_new_metric(client, world):
    flow = await make_sensor(client, world["admin"], world["site"], world["room"], 100, 100, sensor_kind="airflow")
    response = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": world["integration"]["id"], "managed_asset_id": flow["id"], "source_identifier": "x", "canonical_metric": "airflow_m3_s", "unit": "Pa", "scale": 1},
        headers=world["manage"],
    )
    assert response.status_code == 422


async def test_alarm_rules_accept_the_new_metrics_and_units(client, world):
    response = await client.post(
        "/api/v1/alarms/rules",
        json={"integration_id": world["integration"]["id"], "metric": "differential_pressure_pa", "rule_type": "threshold_low", "threshold": 5, "unit": "Pa", "name": "Low DP"},
        headers=world["admin"],
    )
    assert response.status_code in (200, 201), response.text
