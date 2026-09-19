"""Transactional raw-telemetry compaction; alarm history is intentionally excluded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.models import DailyTelemetryAggregate, MonitoringPolicy, TelemetryReading

RAW_RETENTION_DAYS = 365


async def _retention_days(db: AsyncSession, configured: int | None) -> int:
    if configured is not None:
        return configured
    policy = await db.get(MonitoringPolicy, 1)
    return policy.raw_retention_days if policy is not None else RAW_RETENTION_DAYS


async def merge_late_reading(
    db: AsyncSession, reading: TelemetryReading, *, now: datetime | None = None, retention_days: int | None = None
) -> bool:
    """Merge a post-compaction edge replay into an existing day without reopening raw data."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=await _retention_days(db, retention_days))
    # A day is compacted only after the whole UTC calendar day has aged out.
    if reading.occurred_at.date() >= cutoff.date():
        return False
    day = reading.occurred_at.date()
    aggregate = (
        await db.execute(
            select(DailyTelemetryAggregate)
            .where(
                DailyTelemetryAggregate.series_key == reading.series_key,
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


async def compact_eligible_raw(
    db: AsyncSession, *, now: datetime | None = None, retention_days: int | None = None, max_groups: int = 200
) -> int:
    """Aggregate then delete groups older than one year in one caller-owned transaction.

    If persistence fails, the exception rolls the transaction back and raw rows remain.
    """
    if max_groups < 1:
        raise ValueError("max_groups must be positive")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=await _retention_days(db, retention_days))
    day_expr = func.date(TelemetryReading.occurred_at)
    groups = (
        await db.execute(
            select(
                TelemetryReading.series_key,
                day_expr.label("day"),
            )
            .where(day_expr < cutoff.date())
            .group_by(
                TelemetryReading.series_key,
                day_expr,
            )
            .limit(max_groups)
        )
    ).all()
    compacted = 0
    for row in groups:
        # Lock individual raw rows, rather than an aggregate query.  A concurrent
        # worker skips rows held by this transaction and cannot double-count them.
        raw_rows = (
            await db.execute(
                select(TelemetryReading)
                .where(TelemetryReading.series_key == row.series_key, day_expr == row.day)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        if not raw_rows:
            continue
        first = raw_rows[0]
        sample_count = len(raw_rows)
        average_value = sum(float(item.value) for item in raw_rows) / sample_count
        minimum_value = min(float(item.value) for item in raw_rows)
        maximum_value = max(float(item.value) for item in raw_rows)
        existing = (
            await db.execute(
                select(DailyTelemetryAggregate)
                .where(
                    DailyTelemetryAggregate.series_key == row.series_key,
                    DailyTelemetryAggregate.day == row.day,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                DailyTelemetryAggregate(
                    integration_id=first.integration_id,
                    managed_asset_id=first.managed_asset_id,
                    external_identifier=first.external_identifier,
                    series_key=first.series_key,
                    metric=first.metric,
                    unit=first.unit,
                    day=row.day,
                    average_value=average_value,
                    minimum_value=minimum_value,
                    maximum_value=maximum_value,
                    sample_count=sample_count,
                )
            )
        else:
            total = existing.sample_count + sample_count
            old_total = float(existing.average_value) * existing.sample_count
            existing.average_value = (old_total + average_value * sample_count) / total
            existing.minimum_value = min(float(existing.minimum_value), minimum_value)
            existing.maximum_value = max(float(existing.maximum_value), maximum_value)
            existing.sample_count = total
        await db.flush()  # must succeed before deletion
        await db.execute(
            delete(TelemetryReading).where(TelemetryReading.id.in_([item.id for item in raw_rows]))
        )
        compacted += 1
    return compacted
