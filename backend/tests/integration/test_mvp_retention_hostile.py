"""PostgreSQL hostile checks for retention safety and series isolation.

These tests deliberately use the actual ORM transaction boundary.  They are not
SQLite substitutes: row locking and the unique constraint are PostgreSQL behavior.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.telemetry_retention import compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    TelemetryReading,
    telemetry_series_key,
)


@pytest.fixture
async def telemetry_series(db_session):
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    collector = Collector(
        id=uuid.uuid4(), name=f"retention-{uuid.uuid4().hex}", collector_type="central", status="active",
        secret_ciphertext="test", secret_rotated_at=now,
    )
    integration = Integration(
        id=uuid.uuid4(), name=f"retention-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.1",
        config={}, poll_interval_seconds=300,
    )
    mapping = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, source_identifier="temp", canonical_metric="temperature_c",
        unit="celsius", scale=1,
    )
    db_session.add_all((collector, integration, mapping))
    await db_session.flush()

    async def add(*, value: float, occurred_at: datetime, external_identifier: str = "sensor-a", metric: str = "temperature_c", unit: str = "celsius", asset_id=None):
        reading = TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id,
            managed_asset_id=asset_id, external_identifier=external_identifier,
            series_key=telemetry_series_key(integration.id, asset_id, external_identifier, metric, unit),
            dedup_key=uuid.uuid4().hex, metric=metric, unit=unit, value=value,
            occurred_at=occurred_at, received_at=occurred_at, attributes={},
        )
        db_session.add(reading)
        await db_session.flush()
        return reading

    return now, integration, add


@pytest.mark.asyncio
async def test_compaction_math_isolated_series_and_complete_day_boundary(db_session, telemetry_series):
    now, integration, add = telemetry_series
    old_day = (now - timedelta(days=366)).replace(hour=3)
    # Independent oracle: (1.25 + 2.75 + 2.75) / 3 == 2.25.
    await add(value=1.25, occurred_at=old_day)
    await add(value=2.75, occurred_at=old_day + timedelta(minutes=17))
    await add(value=2.75, occurred_at=old_day + timedelta(hours=8))
    await add(value=99, occurred_at=old_day, external_identifier="sensor-b")
    await add(value=45, occurred_at=old_day, metric="humidity_percent", unit="percent")
    # The cut-off calendar day remains raw even if an early timestamp is older
    # than the rolling instant, preventing a partial-day aggregate.
    cutoff_day = (now - timedelta(days=365)).replace(hour=0)
    await add(value=9, occurred_at=cutoff_day)
    await compact_eligible_raw(db_session, now=now)
    await db_session.commit()

    aggregates = (await db_session.execute(
        select(DailyTelemetryAggregate).where(DailyTelemetryAggregate.integration_id == integration.id)
    )).scalars().all()
    assert len(aggregates) == 3
    temp_a = next(item for item in aggregates if item.external_identifier == "sensor-a" and item.metric == "temperature_c")
    assert float(temp_a.average_value) == pytest.approx(2.25)
    assert float(temp_a.minimum_value) == pytest.approx(1.25)
    assert float(temp_a.maximum_value) == pytest.approx(2.75)
    assert temp_a.sample_count == 3
    assert (await db_session.execute(select(TelemetryReading).where(TelemetryReading.value == 9))).scalar_one()


@pytest.mark.asyncio
async def test_retry_and_late_arrival_update_only_matching_series(db_session, telemetry_series):
    now, integration, add = telemetry_series
    old = now - timedelta(days=366)
    await add(value=10, occurred_at=old)
    await compact_eligible_raw(db_session, now=now)
    await compact_eligible_raw(db_session, now=now)
    late = await add(value=20, occurred_at=old + timedelta(hours=1))
    assert await merge_late_reading(db_session, late, now=now)
    await db_session.delete(late)
    await db_session.commit()
    aggregate = (await db_session.execute(select(DailyTelemetryAggregate))).scalar_one()
    assert aggregate.sample_count == 2
    assert float(aggregate.average_value) == pytest.approx(15)
    assert float(aggregate.minimum_value) == pytest.approx(10)
    assert float(aggregate.maximum_value) == pytest.approx(20)


@pytest.mark.asyncio
async def test_late_without_aggregate_remains_raw_until_normal_compaction(db_session, telemetry_series):
    now, _, add = telemetry_series
    late = await add(value=4, occurred_at=now - timedelta(days=366))
    assert not await merge_late_reading(db_session, late, now=now)
    await compact_eligible_raw(db_session, now=now)
    await db_session.commit()
    assert (await db_session.execute(select(DailyTelemetryAggregate))).scalar_one().sample_count == 1


@pytest.mark.asyncio
async def test_rollback_after_flush_or_delete_preserves_recoverable_raw(db_session, telemetry_series, monkeypatch):
    now, _, add = telemetry_series
    await add(value=8, occurred_at=now - timedelta(days=366))
    original_flush = db_session.flush
    calls = 0

    async def fail_after_aggregate_flush(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 0:
            raise RuntimeError("injected persistence failure")
        return await original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fail_after_aggregate_flush)
    with pytest.raises(RuntimeError):
        await compact_eligible_raw(db_session, now=now)
    await db_session.rollback()
    # New transaction sees original raw record; no committed aggregate exists.
    assert (await db_session.execute(select(TelemetryReading))).scalar_one()
    assert (await db_session.execute(select(DailyTelemetryAggregate))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_daily_series_day_unique_constraint_is_database_enforced(db_session, telemetry_series):
    now, integration, _ = telemetry_series
    series_key = telemetry_series_key(integration.id, None, "sensor-a", "temperature_c", "celsius")
    day = (now - timedelta(days=366)).date()
    db_session.add_all(
        (
            DailyTelemetryAggregate(
                integration_id=integration.id, external_identifier="sensor-a", series_key=series_key,
                metric="temperature_c", unit="celsius", day=day,
                average_value=1, minimum_value=1, maximum_value=1, sample_count=1,
            ),
            DailyTelemetryAggregate(
                integration_id=integration.id, external_identifier="sensor-a", series_key=series_key,
                metric="temperature_c", unit="celsius", day=day,
                average_value=2, minimum_value=2, maximum_value=2, sample_count=1,
            ),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_overlapping_postgres_compactors_do_not_double_count(db_engine, db_session, telemetry_series):
    now, _, add = telemetry_series
    old = now - timedelta(days=366)
    for value in (1, 2, 4):
        await add(value=value, occurred_at=old)
    await db_session.commit()
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def worker() -> None:
        async with sessions() as session:
            async with session.begin():
                await compact_eligible_raw(session, now=now)

    await asyncio.gather(worker(), worker())
    aggregates = (await db_session.execute(select(DailyTelemetryAggregate))).scalars().all()
    raw = (await db_session.execute(select(TelemetryReading))).scalars().all()
    assert len(aggregates) == 1
    assert aggregates[0].sample_count == 3
    assert float(aggregates[0].average_value) == pytest.approx(7 / 3)
    assert raw == []
