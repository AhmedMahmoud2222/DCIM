"""Issue #128 / G4-A: merge-versus-compaction races on real, separate PostgreSQL connections.

Synchronisation uses pg_stat_activity (a backend waiting on a lock), never sleeps, so a test cannot pass vacuously by
finishing before the contention happened.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.telemetry_retention import compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    DailyTelemetryAggregateLineage,
    IntegrationMetricMapping,
    TelemetryReading,
    telemetry_series_key,
)

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
BASE = datetime(2025, 8, 15, 6, tzinfo=UTC)


@pytest.fixture
async def arena(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as setup:
        collector = Collector(id=uuid.uuid4(), name=f"race-{uuid.uuid4().hex}", collector_type="central", status="active",
                              secret_ciphertext="test", secret_rotated_at=NOW)
        integration = Integration(id=uuid.uuid4(), name=f"race-{uuid.uuid4().hex}", integration_type="snmp",
                                  target_host="192.0.2.1", config={}, poll_interval_seconds=300)
        mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="p",
                                           canonical_metric="power_kw", unit="kW", scale=1)
        setup.add_all((collector, integration, mapping))
        await setup.commit()
        ids = (collector.id, integration.id, mapping.id)
    series_key = telemetry_series_key(ids[1], None, "sensor", "power_kw", "kW")

    def make(value, minute):
        at = BASE + timedelta(minutes=minute)
        return TelemetryReading(
            id=uuid.uuid4(), collector_id=ids[0], integration_id=ids[1], mapping_id=ids[2], managed_asset_id=None,
            external_identifier="sensor", series_key=series_key, dedup_key=uuid.uuid4().hex, metric="power_kw", unit="kW",
            value=D(value), occurred_at=at, received_at=at, attributes={})

    async def seed_raw(values, start=0):
        async with factory() as session:
            session.add_all([make(v, start + i) for i, v in enumerate(values)])
            await session.commit()

    async def seed_compacted(values):
        await seed_raw(values)
        async with factory() as session:
            await compact_eligible_raw(session, now=NOW, retention_days=365)
            await session.commit()

    async def result():
        async with factory() as session:
            agg = (await session.execute(select(DailyTelemetryAggregate))).scalar_one()
            raw = (await session.execute(select(text("count(*)")).select_from(TelemetryReading))).scalar_one()
            return agg, raw

    async def wait_for_lock_waiter(timeout=15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        # AUTOCOMMIT: pg_stat_activity is snapshotted per transaction, so a polling loop inside one transaction
        # would keep seeing the state from its first poll and miss a waiter that appears later.
        async with db_engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            while asyncio.get_running_loop().time() < deadline:
                waiting = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pid <> pg_backend_pid()"))).scalar()
                if waiting:
                    return
                await asyncio.sleep(0.05)
            activity = (await conn.execute(text(
                "SELECT pid, state, wait_event_type, wait_event, left(query, 80) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"))).all()
        raise AssertionError(f"no backend ever waited on a lock: the scenario did not exercise the race: {activity}")

    return {"factory": factory, "make": make, "seed_raw": seed_raw, "seed_compacted": seed_compacted,
            "result": result, "wait": wait_for_lock_waiter, "series_key": series_key}


async def _late(session, arena, value, minute):
    reading = arena["make"](value, minute)
    session.add(reading)
    await session.flush()
    merged = bool(await merge_late_reading(session, reading, now=NOW, retention_days=365))
    if merged:
        await session.delete(reading)
        await session.flush()
    return merged


def exact(values):
    return sum(D(v) for v in values), len(values)



async def assert_coverage(arena):
    agg, raw = await arena["result"]()
    async with arena["factory"]() as session:
        count = await session.scalar(select(func.sum(DailyTelemetryAggregateLineage.sample_count)))
    assert count == agg.lineage_sample_count == agg.sample_count
    assert agg.evidence_class == "unverified"  # these race fixtures explicitly have no pinned contract
    return agg, raw


async def test_lineage_late_merge_vs_compaction_waits_on_parent_and_counts_every_fresh_key_once(arena):
    await arena["seed_compacted"](["1", "2"])
    await arena["seed_raw"](["10", "20"], start=50)
    async with arena["factory"]() as first, arena["factory"]() as second:
        assert await _late(first, arena, "6", 100)
        work = asyncio.create_task(compact_eligible_raw(second, now=NOW, retention_days=365))
        await arena["wait"]()
        assert not work.done()
        await first.commit()
        await work
        await second.commit()
    agg, raw = await assert_coverage(arena)
    assert (agg.sample_count, agg.sum_value, raw) == (5, D("39"), 0)


async def test_lineage_simultaneous_late_merges_refresh_parent_after_wait(arena):
    await arena["seed_compacted"](["1", "2"])
    async with arena["factory"]() as first, arena["factory"]() as second:
        assert await _late(first, arena, "6", 100)
        work = asyncio.create_task(_late(second, arena, "7", 101))
        await arena["wait"]()
        assert not work.done()
        await first.commit()
        assert await work
        await second.commit()
    agg, raw = await assert_coverage(arena)
    assert (agg.sample_count, agg.sum_value, raw) == (4, D("16"), 0)


async def test_first_aggregate_collision_rolls_back_lineage_and_claims_before_retry(arena):
    await arena["seed_raw"](["1", "2"])
    async with arena["factory"]() as first, arena["factory"]() as second:
        await compact_eligible_raw(first, now=NOW, retention_days=365)
        await arena["seed_raw"](["10", "20"], start=50)
        work = asyncio.create_task(compact_eligible_raw(second, now=NOW, retention_days=365))
        await arena["wait"]()
        assert not work.done()
        await first.commit()
        with pytest.raises(IntegrityError):
            await work
        await second.rollback()
    agg, raw = await assert_coverage(arena)
    assert (agg.sample_count, raw) == (2, 2)
    async with arena["factory"]() as retry:
        await compact_eligible_raw(retry, now=NOW, retention_days=365)
        await retry.commit()
    agg, raw = await assert_coverage(arena)
    assert (agg.sample_count, agg.sum_value, raw) == (4, D("33"), 0)
