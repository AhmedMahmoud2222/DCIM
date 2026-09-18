"""Transactional raw-telemetry compaction; alarm history is intentionally excluded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.models import DailyTelemetryAggregate, TelemetryReading

RAW_RETENTION_DAYS = 365


async def merge_late_reading(db: AsyncSession, reading: TelemetryReading, *, now: datetime | None = None) -> bool:
    """Merge a post-compaction edge replay into an existing day without reopening raw data."""
    now = now or datetime.now(UTC)
    if reading.occurred_at > now - timedelta(days=RAW_RETENTION_DAYS):
        return False
    day = reading.occurred_at.date()
    aggregate = (
        await db.execute(
            select(DailyTelemetryAggregate)
            .where(
                DailyTelemetryAggregate.integration_id == reading.integration_id,
                DailyTelemetryAggregate.external_identifier == reading.external_identifier,
                DailyTelemetryAggregate.metric == reading.metric,
                DailyTelemetryAggregate.day == day,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if aggregate is None:
        return False  # normal compactor will create the first aggregate from retained raw rows
    count = aggregate.sample_count + 1
    aggregate.average_value = (float(aggregate.average_value) * aggregate.sample_count + float(reading.value)) / count
    aggregate.minimum_value = min(float(aggregate.minimum_value), float(reading.value))
    aggregate.maximum_value = max(float(aggregate.maximum_value), float(reading.value))
    aggregate.sample_count = count
    return True


async def compact_eligible_raw(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Aggregate then delete groups older than one year in one caller-owned transaction.

    If persistence fails, the exception rolls the transaction back and raw rows remain.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=RAW_RETENTION_DAYS)
    day_expr = func.date(TelemetryReading.occurred_at)
    groups = (
        await db.execute(
            select(
                TelemetryReading.integration_id,
                TelemetryReading.managed_asset_id,
                TelemetryReading.external_identifier,
                TelemetryReading.metric,
                TelemetryReading.unit,
                day_expr.label("day"),
                func.avg(TelemetryReading.value).label("avg"),
                func.min(TelemetryReading.value).label("min"),
                func.max(TelemetryReading.value).label("max"),
                func.count(TelemetryReading.id).label("count"),
            )
            .where(TelemetryReading.occurred_at < cutoff)
            .group_by(
                TelemetryReading.integration_id,
                TelemetryReading.managed_asset_id,
                TelemetryReading.external_identifier,
                TelemetryReading.metric,
                TelemetryReading.unit,
                day_expr,
            )
        )
    ).all()
    compacted = 0
    for row in groups:
        existing = (
            await db.execute(
                select(DailyTelemetryAggregate)
                .where(
                    DailyTelemetryAggregate.integration_id == row.integration_id,
                    DailyTelemetryAggregate.external_identifier == row.external_identifier,
                    DailyTelemetryAggregate.metric == row.metric,
                    DailyTelemetryAggregate.day == row.day,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                DailyTelemetryAggregate(
                    integration_id=row.integration_id,
                    managed_asset_id=row.managed_asset_id,
                    external_identifier=row.external_identifier,
                    metric=row.metric,
                    unit=row.unit,
                    day=row.day,
                    average_value=row.avg,
                    minimum_value=row.min,
                    maximum_value=row.max,
                    sample_count=row.count,
                )
            )
        else:
            total = existing.sample_count + row.count
            old_total = float(existing.average_value) * existing.sample_count
            existing.average_value = (old_total + float(row.avg) * row.count) / total
            existing.minimum_value = min(float(existing.minimum_value), float(row.min))
            existing.maximum_value = max(float(existing.maximum_value), float(row.max))
            existing.sample_count = total
        await db.flush()  # must succeed before deletion
        await db.execute(
            delete(TelemetryReading).where(
                TelemetryReading.integration_id == row.integration_id,
                TelemetryReading.external_identifier == row.external_identifier,
                TelemetryReading.metric == row.metric,
                func.date(TelemetryReading.occurred_at) == row.day,
                TelemetryReading.occurred_at < cutoff,
            )
        )
        compacted += 1
    return compacted
