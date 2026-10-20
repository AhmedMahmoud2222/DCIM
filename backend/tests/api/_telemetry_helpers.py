"""Seeds authoritative telemetry rows directly (the ingest path has its own tests)."""

import uuid
from datetime import UTC, datetime

from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading, telemetry_series_key

_UNITS = {"temperature_c": "degC", "humidity_percent": "%", "power_kw": "kW"}


async def seed_reading(db_session, asset_id: str, metric: str, value: float, occurred_at: datetime, *, poll: int = 60) -> None:
    now = datetime.now(UTC)
    collector = Collector(
        id=uuid.uuid4(), name=f"sp-{uuid.uuid4().hex}", collector_type="central", status="active", secret_ciphertext="test",
        secret_rotated_at=now,
    )
    integration = Integration(
        id=uuid.uuid4(), name=f"sp-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.1", config={},
        poll_interval_seconds=poll,
    )
    unit = _UNITS[metric]
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="p", canonical_metric=metric, unit=unit, scale=1)
    db_session.add_all((collector, integration, mapping))
    await db_session.flush()
    asset = uuid.UUID(asset_id)
    db_session.add(
        TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id, managed_asset_id=asset,
            external_identifier="x", series_key=telemetry_series_key(integration.id, asset, "x", metric, unit),
            dedup_key=uuid.uuid4().hex, metric=metric, unit=unit, value=value, occurred_at=occurred_at, received_at=occurred_at,
            attributes={},
        )
    )
    await db_session.flush()
