"""PostgreSQL checks for the conservative 0014 historical asset backfill."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    TelemetryReading,
    telemetry_series_key,
)


def _backfill_revision():
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0014_safe_telemetry_asset_backfill.py"
    spec = importlib.util.spec_from_file_location("telemetry_asset_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _execute_upgrade(db_session) -> None:
    """Run the exact Alembic upgrade SQL against the PostgreSQL test connection."""
    revision = _backfill_revision()
    connection = await db_session.connection()

    def run_upgrade(sync_connection) -> None:
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            revision.upgrade()

    await connection.run_sync(run_upgrade)


@pytest.mark.asyncio
async def test_backfill_attributes_only_proven_series_and_exposes_asset_history(client, db_session, auth_headers):
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - timedelta(days=366)
    collector = Collector(
        id=uuid.uuid4(), name=f"backfill-{uuid.uuid4().hex}", collector_type="central", status="active",
        secret_ciphertext="test", secret_rotated_at=now,
    )
    integration = Integration(
        id=uuid.uuid4(), name=f"backfill-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.20",
        config={}, poll_interval_seconds=300,
    )
    asset_a = ManagedAsset(
        id=uuid.uuid4(), asset_type="equipment", asset_tag=f"backfill-a-{uuid.uuid4().hex}", lifecycle_status="active"
    )
    asset_b = ManagedAsset(
        id=uuid.uuid4(), asset_type="equipment", asset_tag=f"backfill-b-{uuid.uuid4().hex}", lifecycle_status="active"
    )
    deterministic = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=asset_a.id,
        source_identifier="1.3.6.1.4.1.1", canonical_metric="temperature_c", unit="celsius", scale=1,
    )
    # Two mappings with the same daily-aggregate evidence make that aggregate
    # ambiguous.  The historical raw row below keeps its stable mapping ID, so it
    # remains safely attributable to asset_a even though the daily row does not.
    ambiguous_one = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=asset_a.id,
        source_identifier="1.3.6.1.4.1.2", canonical_metric="humidity_percent", unit="percent", scale=1,
    )
    ambiguous_two = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=asset_b.id,
        source_identifier="1.3.6.1.4.1.3", canonical_metric="humidity_percent", unit="percent", scale=1,
    )
    unattributable = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=None,
        source_identifier="1.3.6.1.4.1.4", canonical_metric="power_kw", unit="kw", scale=1,
    )
    db_session.add_all(
        (collector, integration, asset_a, asset_b, deterministic, ambiguous_one, ambiguous_two, unattributable)
    )
    await db_session.flush()

    def unmanaged_key(external: str, metric: str, unit: str) -> str:
        return telemetry_series_key(integration.id, None, external, metric, unit)

    raw_deterministic = TelemetryReading(
        id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=deterministic.id,
        managed_asset_id=None, external_identifier="sensor-a", series_key=unmanaged_key("sensor-a", "temperature_c", "celsius"),
        dedup_key="deterministic-raw", metric="temperature_c", unit="celsius", value=20,
        occurred_at=old, received_at=old, attributes={},
    )
    raw_unattributable = TelemetryReading(
        id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=unattributable.id,
        managed_asset_id=None, external_identifier="sensor-c", series_key=unmanaged_key("sensor-c", "power_kw", "kw"),
        dedup_key="unattributable-raw", metric="power_kw", unit="kw", value=3,
        occurred_at=old, received_at=old, attributes={},
    )
    daily_deterministic = DailyTelemetryAggregate(
        integration_id=integration.id, managed_asset_id=None, external_identifier="sensor-a",
        series_key=unmanaged_key("sensor-a", "temperature_c", "celsius"), metric="temperature_c", unit="celsius",
        day=old.date(), average_value=20, minimum_value=19, maximum_value=21, sample_count=3,
    )
    daily_ambiguous = DailyTelemetryAggregate(
        integration_id=integration.id, managed_asset_id=None, external_identifier="sensor-b",
        series_key=unmanaged_key("sensor-b", "humidity_percent", "percent"), metric="humidity_percent", unit="percent",
        day=old.date(), average_value=45, minimum_value=40, maximum_value=50, sample_count=3,
    )
    daily_unattributable = DailyTelemetryAggregate(
        integration_id=integration.id, managed_asset_id=None, external_identifier="sensor-c",
        series_key=unmanaged_key("sensor-c", "power_kw", "kw"), metric="power_kw", unit="kw",
        day=old.date(), average_value=3, minimum_value=3, maximum_value=3, sample_count=1,
    )
    db_session.add_all(
        (raw_deterministic, raw_unattributable, daily_deterministic, daily_ambiguous, daily_unattributable)
    )
    await db_session.flush()

    await _execute_upgrade(db_session)
    # The NULL-only predicates make retry safe: a recovery rerun cannot rewrite
    # an already attributed series or add a second aggregate.
    await _execute_upgrade(db_session)
    await db_session.commit()

    await db_session.refresh(raw_deterministic)
    await db_session.refresh(raw_unattributable)
    await db_session.refresh(daily_deterministic)
    await db_session.refresh(daily_ambiguous)
    await db_session.refresh(daily_unattributable)
    assert raw_deterministic.managed_asset_id == asset_a.id
    assert raw_deterministic.series_key == telemetry_series_key(
        integration.id, asset_a.id, "sensor-a", "temperature_c", "celsius"
    )
    assert daily_deterministic.managed_asset_id == asset_a.id
    assert daily_deterministic.series_key == telemetry_series_key(
        integration.id, asset_a.id, "sensor-a", "temperature_c", "celsius"
    )
    # Two candidate mappings and no mapping respectively are retained intact and
    # unassociated; a current integration/metric name is never treated as proof.
    assert daily_ambiguous.managed_asset_id is None
    assert daily_unattributable.managed_asset_id is None
    assert raw_unattributable.managed_asset_id is None

    headers = await auth_headers()
    response = await client.get(
        "/api/v1/telemetry/history",
        params={
            "metric": "temperature_c",
            "managed_asset_id": str(asset_a.id),
            "start": (old - timedelta(days=1)).isoformat(),
            "end": (old + timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [(row["resolution"], row["managed_asset_id"]) for row in response.json()] == [
        ("daily", str(asset_a.id))
    ]
