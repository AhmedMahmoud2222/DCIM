"""Application boundary for append-only telemetry ingestion."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


class MetricMappingNotFound(ValueError):
    pass


@dataclass(frozen=True)
class TelemetryIngestResult:
    reading_id: uuid.UUID | None
    duplicate: bool


async def ingest_reading(
    db: AsyncSession,
    *,
    collector_id: uuid.UUID,
    integration_id: uuid.UUID,
    dedup_key: str,
    external_identifier: str,
    source_identifier: str,
    occurred_at: datetime,
    value: float,
    attributes: dict | None = None,
) -> TelemetryIngestResult:
    mapping = (
        await db.execute(
            select(IntegrationMetricMapping).where(
                IntegrationMetricMapping.integration_id == integration_id,
                IntegrationMetricMapping.source_identifier == source_identifier,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise MetricMappingNotFound("No metric mapping exists for this integration source identifier.")
    received_at = datetime.now(UTC)
    statement = (
        insert(TelemetryReading)
        .values(
            id=uuid.uuid4(),
            collector_id=collector_id,
            integration_id=integration_id,
            mapping_id=mapping.id,
            external_identifier=external_identifier,
            dedup_key=dedup_key,
            metric=mapping.canonical_metric,
            unit=mapping.unit,
            value=value * float(mapping.scale),
            occurred_at=occurred_at,
            received_at=received_at,
            attributes=attributes or {},
        )
        .on_conflict_do_nothing(constraint="uq_telemetry_reading_collector_dedup")
        .returning(TelemetryReading.id)
    )
    reading_id = (await db.execute(statement)).scalar_one_or_none()
    if reading_id is not None:
        # The explicit application boundary prevents the telemetry ORM domain from
        # depending on alarm persistence while retaining one transaction.
        from app.application.alarm_service import evaluate_reading

        reading = await db.get(TelemetryReading, reading_id)
        assert reading is not None
        await evaluate_reading(db, reading)
        # A delayed edge replay for a day already compacted is incorporated into the
        # authoritative daily statistics; occurred_at, never receipt time, chooses it.
        from app.application.telemetry_retention import merge_late_reading

        merged = await merge_late_reading(db, reading)
        if merged:
            await db.delete(reading)  # aggregate update is in the same transaction
    return TelemetryIngestResult(reading_id=reading_id, duplicate=reading_id is None)
