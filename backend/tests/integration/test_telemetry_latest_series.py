"""PostgreSQL API regression tests for latest-per-canonical-series semantics."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading, telemetry_series_key


@pytest.mark.asyncio
async def test_latest_is_one_deterministic_row_per_filtered_series(client, db_session, auth_headers):
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    collector = Collector(id=uuid.uuid4(), name=f"latest-{uuid.uuid4().hex}", collector_type="central", status="active", secret_ciphertext="x", secret_rotated_at=now)
    first = Integration(id=uuid.uuid4(), name=f"latest-a-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.1", config={}, poll_interval_seconds=300)
    second = Integration(id=uuid.uuid4(), name=f"latest-b-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.2", config={}, poll_interval_seconds=600)
    asset = ManagedAsset(id=uuid.uuid4(), asset_type="equipment", asset_tag=f"asset-{uuid.uuid4().hex}", lifecycle_status="active")
    mappings = [IntegrationMetricMapping(id=uuid.uuid4(), integration_id=first.id, source_identifier="temp", canonical_metric="temperature_c", unit="celsius", scale=1), IntegrationMetricMapping(id=uuid.uuid4(), integration_id=first.id, source_identifier="humidity", canonical_metric="humidity_percent", unit="percent", scale=1), IntegrationMetricMapping(id=uuid.uuid4(), integration_id=second.id, source_identifier="temp", canonical_metric="temperature_c", unit="celsius", scale=1)]
    db_session.add_all([collector, first, second, asset, *mappings])
    await db_session.flush()

    def reading(integration, mapping, external, metric, unit, value, occurred, received, asset_id=None, reading_id=None):
        return TelemetryReading(id=reading_id or uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id, managed_asset_id=asset_id, external_identifier=external, series_key=telemetry_series_key(integration.id, asset_id, external, metric, unit), dedup_key=uuid.uuid4().hex, metric=metric, unit=unit, value=value, occurred_at=occurred, received_at=received, attributes={})

    # Noisy series plus two other series: limit applies after de-duplication.
    rows = [reading(first, mappings[0], "a", "temperature_c", "celsius", i, now - timedelta(minutes=10-i), now - timedelta(minutes=10-i), asset.id) for i in range(5)]
    tie_old = uuid.UUID("00000000-0000-0000-0000-000000000001")
    tie_new = uuid.UUID("00000000-0000-0000-0000-000000000002")
    rows += [reading(first, mappings[1], "a", "humidity_percent", "percent", 40, now, now, asset.id, tie_old), reading(first, mappings[1], "a", "humidity_percent", "percent", 41, now, now, asset.id, tie_new), reading(second, mappings[2], "b", "temperature_c", "celsius", 21, now - timedelta(minutes=1), now - timedelta(minutes=1))]
    db_session.add_all(rows)
    await db_session.commit()

    headers = await auth_headers()
    response = await client.get("/api/v1/telemetry/latest?limit=10", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload) == 3
    assert {(row["integration_id"], row["metric"]) for row in payload} == {(str(first.id), "temperature_c"), (str(first.id), "humidity_percent"), (str(second.id), "temperature_c")}
    assert next(row for row in payload if row["metric"] == "temperature_c" and row["integration_id"] == str(first.id))["value"] == 4
    assert next(row for row in payload if row["metric"] == "humidity_percent")["id"] == str(tie_new)
    assert all(row["expected_poll_interval_seconds"] in (300, 600) for row in payload)

    scoped = await client.get(f"/api/v1/telemetry/latest?managed_asset_id={asset.id}&metric=temperature_c&limit=1", headers=headers)
    assert scoped.status_code == 200 and len(scoped.json()) == 1
    assert scoped.json()[0]["integration_id"] == str(first.id)
