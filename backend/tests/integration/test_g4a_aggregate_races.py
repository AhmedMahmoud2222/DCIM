"""Issue #128 / G4-A: merge-versus-compaction races on real, separate PostgreSQL connections.

Synchronisation uses pg_stat_activity (a backend waiting on a lock), never sleeps, so a test cannot pass vacuously by
finishing before the contention happened.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import telemetry_retention
from app.application.telemetry_retention import compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
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
        async with db_engine.connect() as conn:
            while asyncio.get_running_loop().time() < deadline:
                waiting = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pid <> pg_backend_pid()"))).scalar()
                if waiting:
                    return
                await asyncio.sleep(0.05)
        raise AssertionError("no backend ever waited on a lock: the scenario did not exercise the race")

    return {"factory": factory, "make": make, "seed_raw": seed_raw, "seed_compacted": seed_compacted,
            "result": result, "wait": wait_for_lock_waiter, "series_key": series_key}


async def _late(session, arena, value, minute):
    reading = arena["make"](value, minute)
    session.add(reading)
    await session.flush()
    merged = await merge_late_reading(session, reading, now=NOW, retention_days=365)
    if merged:
        await session.delete(reading)
        await session.flush()
    return merged


def exact(values):
    return sum(D(v) for v in values), len(values)


async def test_r1_merge_holds_the_aggregate_and_compaction_waits_then_both_are_counted(arena):
    await arena["seed_compacted"](["1", "2", "3"])
    await arena["seed_raw"](["10", "20"], start=50)
    first, second = arena["factory"](), arena["factory"]()
    async with first, second:
        assert await _late(first, arena, "6", 100) is True            # holds the aggregate row lock, uncommitted
        compaction = asyncio.create_task(compact_eligible_raw(second, now=NOW, retention_days=365))
        await arena["wait"]()
        assert not compaction.done()
        await first.commit()
        assert await compaction == 1
        await second.commit()
    agg, raw = await arena["result"]()
    total, count = exact(["1", "2", "3", "10", "20", "6"])
    assert (agg.sum_value, agg.sample_count, agg.average_value, agg.minimum_value, agg.maximum_value) == (
        total, count, D("7.00000000"), D("1"), D("20"))
    assert raw == 0


async def test_r2_compaction_holds_the_aggregate_and_a_late_merge_waits_then_both_are_counted(arena):
    await arena["seed_compacted"](["1", "2", "3"])
    await arena["seed_raw"](["10", "20"], start=50)
    first, second = arena["factory"](), arena["factory"]()
    async with first, second:
        assert await compact_eligible_raw(first, now=NOW, retention_days=365) == 1   # aggregate locked, uncommitted
        merge = asyncio.create_task(_late(second, arena, "6", 100))
        await arena["wait"]()
        assert not merge.done()
        await first.commit()
        assert await merge is True
        await second.commit()
    agg, raw = await arena["result"]()
    assert (agg.sum_value, agg.sample_count) == exact(["1", "2", "3", "10", "20", "6"]) and raw == 0


async def test_r3_two_compactors_split_the_rows_with_skip_locked_and_the_totals_match_a_single_pass(arena):
    values = [str(i) for i in range(1, 9)]
    await arena["seed_raw"](values)
    holder, compactor = arena["factory"](), arena["factory"]()
    async with holder, compactor:
        # another worker is already consuming the rows with ids in the lower half of the day
        held = (await holder.execute(
            select(TelemetryReading).order_by(TelemetryReading.occurred_at).limit(4).with_for_update())).scalars().all()
        assert len(held) == 4
        assert await compact_eligible_raw(compactor, now=NOW, retention_days=365) == 1
        await compactor.commit()
        await holder.rollback()
    agg, raw = await arena["result"]()
    assert (agg.sample_count, raw) == (4, 4) and agg.sum_value == D("26")  # rows 5 to 8 consumed, rows 1 to 4 still raw
    async with arena["factory"]() as session:
        await compact_eligible_raw(session, now=NOW, retention_days=365)
        await session.commit()
    agg, raw = await arena["result"]()
    assert (agg.sum_value, agg.sample_count, agg.average_value, raw) == (D("36"), 8, D("4.50000000"), 0)


async def test_r4_a_merge_cannot_see_an_uncommitted_aggregate_so_the_reading_stays_raw_and_is_compacted_later(arena):
    await arena["seed_raw"](["1", "2", "3"])
    compactor, merger = arena["factory"](), arena["factory"]()
    async with compactor, merger:
        assert await compact_eligible_raw(compactor, now=NOW, retention_days=365) == 1   # aggregate not yet committed
        assert await _late(merger, arena, "6", 100) is False        # no visible aggregate: the reading stays raw
        await compactor.commit()
        await merger.commit()
    agg, raw = await arena["result"]()
    assert (agg.sample_count, raw) == (3, 1)                        # nothing lost or double counted
    async with arena["factory"]() as session:
        await compact_eligible_raw(session, now=NOW, retention_days=365)
        await session.commit()
    agg, raw = await arena["result"]()
    assert (agg.sum_value, agg.sample_count, raw) == (D("12"), 4, 0)


async def test_r5_failure_after_the_aggregate_flush_rolls_everything_back(arena, monkeypatch):
    await arena["seed_compacted"](["1", "2", "3"])
    await arena["seed_raw"](["10", "20"], start=50)

    def exploding_delete(*_args, **_kwargs):
        raise RuntimeError("injected failure")

    async with arena["factory"]() as session:
        monkeypatch.setattr(telemetry_retention, "delete", exploding_delete)
        with pytest.raises(RuntimeError, match="injected"):
            await compact_eligible_raw(session, now=NOW, retention_days=365)
        await session.rollback()
    agg, raw = await arena["result"]()
    assert (agg.sum_value, agg.sample_count, raw) == (D("6"), 3, 2)


async def test_r6_two_concurrent_late_merges_serialise_and_both_count(arena):
    await arena["seed_compacted"](["1", "2", "3"])
    first, second = arena["factory"](), arena["factory"]()
    async with first, second:
        assert await _late(first, arena, "0.00000001", 100) is True
        other = asyncio.create_task(_late(second, arena, "0.00000002", 101))
        await arena["wait"]()
        assert not other.done()
        await first.commit()
        assert await other is True
        await second.commit()
    agg, raw = await arena["result"]()
    assert (agg.sum_value, agg.sample_count, raw) == (D("6.00000003"), 5, 0)
    assert agg.average_value == D("1.20000001")  # 6.00000003 / 5 = 1.200000006 -> 1.20000001


async def test_r7_a_stale_session_copy_of_the_aggregate_is_refreshed_by_the_locked_read(arena):
    await arena["seed_compacted"](["1", "2", "3"])
    stale, other = arena["factory"](), arena["factory"]()
    async with stale, other:
        cached = (await stale.execute(select(DailyTelemetryAggregate))).scalar_one()    # session holds count 3
        assert cached.sample_count == 3
        assert await _late(other, arena, "6", 100) is True
        await other.commit()                                                             # database now has count 4
        assert await _late(stale, arena, "4", 101) is True                              # must build on count 4, not 3
        await stale.commit()
    agg, _ = await arena["result"]()
    assert (agg.sum_value, agg.sample_count, agg.average_value) == (D("16"), 5, D("3.20000000"))
