"""Telemetry storage contract (alarm history is intentionally excluded).

Policy controls when complete UTC days become eligible for compaction, not history
visibility. Stored raw rows and daily aggregates own disjoint samples: aggregate
updates and deletion of exactly the consumed raw rows commit atomically. Aggregate arithmetic is exact (see
``app.domain.telemetry.aggregate_math``; Issue #128 / G4-A): sums are integers in units of 1e-8, the average is one
half-even division of the exact sum, min/max are compared as Decimals. A partial
compaction or late arrival can therefore leave both representations for one series
and day. History must read both in ONE PostgreSQL statement/snapshot, without a
policy-age filter or day-level anti-join (which would discard unconsumed samples).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.aggregate_math import (
    BASIS_EXACT,
    BASIS_LEGACY_SEEDED,
    AggregateState,
    fold,
    state_of,
    to_units,
)
from app.domain.telemetry.models import DailyTelemetryAggregate, MonitoringPolicy, TelemetryReading

RAW_RETENTION_DAYS = 365


def as_utc(value: datetime) -> datetime:
    """Interpret timezone-less API timestamps as UTC, independent of server timezone."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_day(value: datetime) -> date:
    return as_utc(value).date()


async def compaction_cutover(
    db: AsyncSession, *, now: datetime | None = None, retention_days: int | None = None
) -> date:
    """Days strictly before this UTC date may transfer ownership from raw to daily."""
    return utc_day((as_utc(now) if now is not None else datetime.now(UTC))
                   - timedelta(days=await _retention_days(db, retention_days)))


async def _retention_days(db: AsyncSession, configured: int | None) -> int:
    if configured is not None:
        return configured
    policy = await db.get(MonitoringPolicy, 1)
    return policy.raw_retention_days if policy is not None else RAW_RETENTION_DAYS


async def merge_late_reading(
    db: AsyncSession, reading: TelemetryReading, *, now: datetime | None = None, retention_days: int | None = None
) -> bool:
    """Merge a post-compaction edge replay into an existing day without reopening raw data."""
    cutoff = await compaction_cutover(db, now=now, retention_days=retention_days)
    # A day is compacted only after the whole UTC calendar day has aged out.
    if utc_day(reading.occurred_at) >= cutoff:
        return False
    day = utc_day(reading.occurred_at)
    aggregate = await _locked_aggregate(db, reading.series_key, day)
    if aggregate is None:
        return False  # normal compactor will create the first aggregate from retained raw rows
    # Compute before touching the row: an AggregateArithmeticError leaves the aggregate and the reading unchanged and
    # propagates to the caller-owned transaction (the ingest savepoint), so no evidence is deleted on failure.
    _fold_into(aggregate, state_of([cast(Decimal, reading.value)]))
    return True


async def _locked_aggregate(db: AsyncSession, series_key: str, day: date) -> DailyTelemetryAggregate | None:
    """The day's aggregate, row-locked. ``populate_existing`` makes the locked read authoritative.

    Under READ COMMITTED a lock wait re-reads the committed row, but the ORM identity map would otherwise keep the
    attributes of an instance this session loaded earlier and silently overwrite a concurrent writer's sum.
    """
    return (
        await db.execute(
            select(DailyTelemetryAggregate)
            .where(DailyTelemetryAggregate.series_key == series_key, DailyTelemetryAggregate.day == day)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _fold_into(aggregate: DailyTelemetryAggregate, extra: AggregateState) -> None:
    """Add disjoint samples to an existing aggregate exactly, applying the basis transitions.

    * ``decimal_exact`` / ``legacy_seeded``: continue from the stored exact sum.
    * NULL basis (computed before G4-A): seed ``sum = average * sample_count`` from the stored rounded average and mark the
      row ``legacy_seeded``. That keeps the old rounding uncertainty, fixed and labelled; no numeric bound is claimed.
      Every sample added from now on is exact.
    """
    legacy = aggregate.arithmetic_basis is None
    if legacy:
        base_units = to_units(aggregate.average_value) * aggregate.sample_count
    else:
        base_units = to_units(cast(Decimal, aggregate.sum_value))
    merged = fold(
        AggregateState(base_units, aggregate.sample_count, aggregate.minimum_value, aggregate.maximum_value), extra
    )
    if legacy:
        aggregate.arithmetic_basis = BASIS_LEGACY_SEEDED
        aggregate.legacy_sample_count = aggregate.sample_count
    aggregate.sum_value = merged.sum_value
    aggregate.average_value = merged.average
    aggregate.minimum_value = merged.minimum
    aggregate.maximum_value = merged.maximum
    aggregate.sample_count = merged.count


async def compact_eligible_raw(
    db: AsyncSession, *, now: datetime | None = None, retention_days: int | None = None, max_groups: int = 200
) -> int:
    """Aggregate then delete eligible complete UTC days in one caller-owned transaction.

    If persistence fails, the exception rolls the transaction back and raw rows remain.
    """
    if max_groups < 1:
        raise ValueError("max_groups must be positive")
    cutoff = await compaction_cutover(db, now=now, retention_days=retention_days)
    day_expr = func.date(func.timezone("UTC", TelemetryReading.occurred_at))
    groups = (
        await db.execute(
            select(
                TelemetryReading.series_key,
                day_expr.label("day"),
            )
            .where(day_expr < cutoff)
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
        # Exact integer-unit state of the locked rows. Raised errors leave raw rows untouched (nothing is deleted
        # before the aggregate is flushed) and abort the caller-owned transaction.
        group = state_of([cast(Decimal, item.value) for item in raw_rows])
        existing = await _locked_aggregate(db, row.series_key, row.day)
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
                    average_value=group.average,
                    minimum_value=group.minimum,
                    maximum_value=group.maximum,
                    sample_count=group.count,
                    registry_version=first.registry_version,
                    sum_value=group.sum_value,
                    arithmetic_basis=BASIS_EXACT,
                )
            )
        else:
            _fold_into(existing, group)
        await db.flush()  # must succeed before deletion
        await db.execute(
            delete(TelemetryReading).where(TelemetryReading.id.in_([item.id for item in raw_rows]))
        )
        compacted += 1
    return compacted
