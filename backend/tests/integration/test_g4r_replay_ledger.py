"""Issue #128 / G4-R: replay protection on real, separate PostgreSQL connections.

Races are synchronised through pg_stat_activity (a backend waiting on a lock), never sleeps, so a test cannot pass
vacuously by finishing before the contention happened. Synthetic data only.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import telemetry_retention
from app.application.telemetry_ledger import (
    ReplayHorizonExceeded,
    UnsafeHorizonClear,
    consumed_key_hash,
    effective_horizon,
    guard_horizon_clear,
)
from app.application.telemetry_retention import LateMergeOutcome, compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    MonitoringPolicy,
    TelemetryConsumedKey,
    TelemetryReading,
    TelemetryReplayLedgerState,
    telemetry_series_key,
)
from app.infrastructure.tasks import maintenance

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
BASE = datetime(2025, 8, 15, 6, tzinfo=UTC)  # a day well past the 365-day cutover


@pytest.fixture
async def arena(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as setup:
        collector = Collector(id=uuid.uuid4(), name=f"g4r-{uuid.uuid4().hex}", collector_type="central", status="active",
                              secret_ciphertext="test", secret_rotated_at=NOW)
        integration = Integration(id=uuid.uuid4(), name=f"g4r-{uuid.uuid4().hex}", integration_type="snmp",
                                  target_host="192.0.2.1", config={}, poll_interval_seconds=300)
        mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="p",
                                           canonical_metric="power_kw", unit="kW", scale=1)
        setup.add_all((collector, integration, mapping))
        await setup.execute(update(MonitoringPolicy).values(late_replay_horizon_days=None))
        await setup.execute(update(TelemetryReplayLedgerState).values(
            ledger_complete_since=datetime(2020, 1, 1, tzinfo=UTC), last_pruned_at=None, last_pruned_count=None, pruning_begun_at=None))
        await setup.commit()
        ids = (collector.id, integration.id, mapping.id)
    series_key = telemetry_series_key(ids[1], None, "sensor", "power_kw", "kW")

    def make(value, minute, key=None, base=BASE):
        at = base + timedelta(minutes=minute)
        return TelemetryReading(
            id=uuid.uuid4(), collector_id=ids[0], integration_id=ids[1], mapping_id=ids[2], managed_asset_id=None,
            external_identifier="sensor", series_key=series_key, dedup_key=key or uuid.uuid4().hex, metric="power_kw",
            unit="kW", value=D(value), occurred_at=at, received_at=at, attributes={})

    async def seed_raw(items, start=0):
        """items: list of (value, key)."""
        async with factory() as session:
            session.add_all([make(v, start + i, k) for i, (v, k) in enumerate(items)])
            await session.commit()

    async def compact():
        async with factory() as session:
            count = await compact_eligible_raw(session, now=NOW, retention_days=365)
            await session.commit()
            return count

    async def aggregate():
        async with factory() as session:
            return (await session.execute(select(DailyTelemetryAggregate))).scalar_one()

    async def counts():
        async with factory() as session:
            raw = (await session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one()
            keys = (await session.execute(select(func.count()).select_from(TelemetryConsumedKey))).scalar_one()
            return raw, keys

    async def wait_for_lock_waiter(timeout=15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        async with db_engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            while asyncio.get_running_loop().time() < deadline:
                waiting = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pid <> pg_backend_pid()"))).scalar()
                if waiting:
                    return
                await asyncio.sleep(0.05)
        raise AssertionError("no backend ever waited on a lock: the scenario did not exercise the race")

    async def set_horizon(days):
        async with factory() as session:
            await session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=days))
            await session.commit()

    async def set_complete_since(value):
        async with factory() as session:
            await session.execute(update(TelemetryReplayLedgerState).values(ledger_complete_since=value))
            await session.commit()

    async def deliver(session, value, minute, key, *, now=NOW, base=BASE, retention_days=365):
        """Deliver the way ingest does: insert (unique dedup), merge, delete a merged or duplicate reading."""
        reading = make(value, minute, key, base)
        session.add(reading)
        await session.flush()
        outcome = await merge_late_reading(session, reading, now=now, retention_days=retention_days)
        if outcome is not LateMergeOutcome.STAYS_RAW:
            await session.delete(reading)
            await session.flush()
        return outcome

    return {"factory": factory, "make": make, "seed_raw": seed_raw, "compact": compact, "aggregate": aggregate,
            "counts": counts, "wait": wait_for_lock_waiter, "set_horizon": set_horizon, "deliver": deliver,
            "set_complete_since": set_complete_since, "ids": ids}


async def test_key_hash_is_16_bytes_and_separates_collectors():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert len(consumed_key_hash(a, "k")) == 16
    assert consumed_key_hash(a, "k") == consumed_key_hash(a, "k")
    assert consumed_key_hash(a, "k") != consumed_key_hash(b, "k")
    assert consumed_key_hash(a, "ab") != consumed_key_hash(a, "a\x00b")


async def test_compaction_leaves_one_tombstone_per_consumed_row(arena):
    await arena["seed_raw"]([("1", "a"), ("2", "b"), ("3", "c")])
    assert await arena["compact"]() == 1
    assert (await arena["aggregate"]()).sample_count == 3
    assert await arena["counts"]() == (0, 3)


async def test_redelivery_after_compaction_is_a_duplicate_and_counts_do_not_change(arena):
    await arena["seed_raw"]([("1", "a"), ("2", "b"), ("3", "c")])
    await arena["compact"]()
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "2", 1, "b") is LateMergeOutcome.DUPLICATE
        await session.commit()
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (3, D("6"))
    assert await arena["counts"]() == (0, 3)


async def test_new_late_record_merges_once_and_its_redelivery_is_a_duplicate(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "5", 10, "late-1") is LateMergeOutcome.MERGED
        await session.commit()
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "5", 10, "late-1") is LateMergeOutcome.DUPLICATE
        await session.commit()
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("6"))
    assert await arena["counts"]() == (0, 2)


async def test_raw_row_with_an_existing_tombstone_is_deleted_by_compaction_without_being_folded(arena):
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    await arena["compact"]()
    # A later redelivery of "a" lands as a raw row because no aggregate-aware path saw it (e.g. it was inserted directly).
    await arena["seed_raw"]([("100", "a")], start=30)
    assert await arena["compact"]() == 1
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("3"))
    assert await arena["counts"]() == (0, 2)


async def test_compaction_mixes_fresh_and_already_consumed_rows(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["seed_raw"]([("100", "a"), ("7", "new")], start=30)
    await arena["compact"]()
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("8"))


async def test_redelivery_racing_compaction_is_counted_once(arena):
    """Compaction holds the raw row; the redelivery's unique insert waits, then sees the committed tombstone."""
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    first, second = arena["factory"](), arena["factory"]()
    async with first, second:
        assert await compact_eligible_raw(first, now=NOW, retention_days=365) == 1   # rows and tombstones uncommitted
        redelivery = asyncio.create_task(arena["deliver"](second, "2", 1, "b"))
        await arena["wait"]()
        assert not redelivery.done()
        await first.commit()
        assert await redelivery is LateMergeOutcome.DUPLICATE
        await second.commit()
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("3"))
    assert await arena["counts"]() == (0, 2)


async def test_two_simultaneous_deliveries_of_one_late_key_merge_once(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    first, second = arena["factory"](), arena["factory"]()
    outcomes = []
    async with first, second:
        outcomes.append(await arena["deliver"](first, "5", 10, "late-1"))        # aggregate locked, uncommitted
        other = asyncio.create_task(arena["deliver"](second, "5", 10, "late-1"))
        await arena["wait"]()
        assert not other.done()
        await first.commit()
        # the second insert hit the unique (collector, dedup) key of a reading that is already deleted: it proceeds
        outcomes.append(await other)
        await second.commit()
    assert sorted(o.value for o in outcomes) == ["duplicate", "merged"]
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("6"))
    assert await arena["counts"]() == (0, 2)


async def test_late_merge_failure_rolls_back_the_tombstone(arena, monkeypatch):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()

    def boom(*_args, **_kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(telemetry_retention, "_fold_into", boom)
    async with arena["factory"]() as session:
        with pytest.raises(RuntimeError):
            await arena["deliver"](session, "5", 10, "late-1")
        await session.rollback()
    assert (await arena["aggregate"]()).sample_count == 1
    assert await arena["counts"]() == (0, 1)       # the failed claim left no tombstone


async def test_first_aggregate_contention_rolls_back_and_retry_counts_each_key_once(arena):
    """Two compactors claim disjoint rows of a day that has no aggregate yet: the loser's insert conflicts after the
    winner commits, its transaction (claims included) rolls back, and the retry folds into the winner's aggregate."""
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    first, second = arena["factory"](), arena["factory"]()
    async with first, second:
        assert await compact_eligible_raw(first, now=NOW, retention_days=365) == 1   # aggregate inserted, uncommitted
        await arena["seed_raw"]([("10", "c"), ("20", "d")], start=30)
        loser = asyncio.create_task(compact_eligible_raw(second, now=NOW, retention_days=365))
        await arena["wait"]()
        assert not loser.done()
        await first.commit()
        with pytest.raises(IntegrityError):
            await loser
        await second.rollback()
    assert await arena["counts"]() == (2, 2)          # c and d still raw; only a and b hold tombstones
    assert await arena["compact"]() == 1
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (4, D("33"))
    assert await arena["counts"]() == (0, 4)


async def test_null_horizon_never_refuses_and_never_prunes(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    ancient = datetime(2030, 1, 1, tzinfo=UTC)
    async with arena["factory"]() as session:
        assert await effective_horizon(session, now=ancient) is None
        assert await arena["deliver"](session, "5", 10, "late-1", now=ancient) is LateMergeOutcome.MERGED
        await session.commit()
    result = maintenance.prune_consumed_keys(now=ancient)
    assert result == {"skipped": True, "reason": "horizon_unset", "deleted": 0}
    assert await arena["counts"]() == (0, 2)
    async with arena["factory"]() as session:
        state = (await session.execute(select(TelemetryReplayLedgerState))).scalar_one()
        assert state.ledger_complete_since == datetime(2020, 1, 1, tzinfo=UTC) and state.last_pruned_at is None


async def test_finite_horizon_boundary_is_inclusive(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](400)
    occurred = BASE + timedelta(minutes=10)
    now_equal = occurred + timedelta(days=400)
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "5", 10, "edge", now=now_equal) is LateMergeOutcome.MERGED
        await session.commit()
    async with arena["factory"]() as session:
        with pytest.raises(ReplayHorizonExceeded):
            await arena["deliver"](session, "6", 10, "over", now=now_equal + timedelta(microseconds=1))
        await session.rollback()
    agg = await arena["aggregate"]()
    assert agg.sample_count == 2
    assert await arena["counts"]() == (0, 2)       # the refused record claimed nothing


async def test_refusal_only_applies_when_the_day_is_compacted(arena):
    await arena["set_horizon"](1)
    async with arena["factory"]() as session:
        # no aggregate exists: the record stays raw however old it is (normal compaction handles it)
        assert await arena["deliver"](session, "5", 10, "k", now=NOW) is LateMergeOutcome.STAYS_RAW
        await session.commit()


async def test_effective_horizon_is_limited_by_ledger_complete_since(arena):
    await arena["set_horizon"](365)
    await arena["set_complete_since"](NOW - timedelta(days=3))
    async with arena["factory"]() as session:
        assert await effective_horizon(session, now=NOW) == timedelta(days=3)
    await arena["set_horizon"](2)
    async with arena["factory"]() as session:
        assert await effective_horizon(session, now=NOW) == timedelta(days=2)
    await arena["set_complete_since"](NOW + timedelta(days=1))           # clock skew: never negative
    async with arena["factory"]() as session:
        assert await effective_horizon(session, now=NOW) == timedelta(0)


async def test_pre_ledger_coverage_gap_is_bounded_by_ledger_complete_since(arena):
    """R10(a): keys consumed before the ledger existed have no tombstone and none is invented. A finite horizon never
    relies on that period; with a NULL horizon the gap stays open and is documented."""
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](365)
    await arena["set_complete_since"](NOW - timedelta(days=1))
    async with arena["factory"]() as session:
        with pytest.raises(ReplayHorizonExceeded):
            await arena["deliver"](session, "1", 20, "consumed-before-the-ledger", now=NOW)
        await session.rollback()
    assert (await arena["aggregate"]()).sample_count == 1
    await arena["set_horizon"](None)
    async with arena["factory"]() as session:
        outcome = await arena["deliver"](session, "1", 20, "consumed-before-the-ledger", now=NOW)
        await session.commit()
    assert outcome is LateMergeOutcome.MERGED      # the known, labelled gap: no tombstone exists for it
    assert (await arena["aggregate"]()).sample_count == 2


async def test_prune_boundaries_and_ledger_complete_since_advance(arena):
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    await arena["compact"]()                       # tombstones with consumed_day 2026-09-19
    await arena["set_horizon"](30)
    # Deletes consumed_day < utc_day(now - 30d) - 1 day: 2026-09-19 survives until utc_day(now - 30d) reaches 2026-09-21.
    keep = maintenance.prune_consumed_keys(now=datetime(2026, 10, 20, 23, 59, tzinfo=UTC))
    assert (keep["skipped"], keep["deleted"]) == (False, 0)
    assert await arena["counts"]() == (0, 2)
    gone = maintenance.prune_consumed_keys(now=datetime(2026, 10, 21, tzinfo=UTC))
    assert gone["deleted"] == 2
    assert await arena["counts"]() == (0, 0)
    async with arena["factory"]() as session:
        state = (await session.execute(select(TelemetryReplayLedgerState))).scalar_one()
        assert (state.last_pruned_count, state.ledger_complete_since) == (2, datetime(2026, 9, 20, tzinfo=UTC))


async def test_lowering_then_raising_the_horizon_never_trusts_pruned_keys(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](5)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    maintenance.prune_consumed_keys(now=now)       # advances ledger_complete_since to now-5d-1d and deletes the tombstone
    await arena["set_horizon"](400)                # raised afterwards
    async with arena["factory"]() as session:
        assert await effective_horizon(session, now=now) == timedelta(days=6)
        with pytest.raises(ReplayHorizonExceeded):
            await arena["deliver"](session, "1", 0, "a", now=now)   # the pruned key's redelivery is refused, not recounted
        await session.rollback()
    assert (await arena["aggregate"]()).sample_count == 1


async def test_prune_is_batched_and_resumable(arena, monkeypatch):
    await arena["seed_raw"]([(str(i), f"k{i}") for i in range(7)])
    await arena["compact"]()
    await arena["set_horizon"](1)
    monkeypatch.setattr(maintenance, "LEDGER_BATCH_SIZE", 3)
    monkeypatch.setattr(maintenance, "LEDGER_MAX_BATCHES_PER_RUN", 2)
    far = datetime(2026, 12, 1, tzinfo=UTC)
    first = maintenance.prune_consumed_keys(now=far)
    assert (first["deleted"], first["batch_limit_reached"]) == (6, True)
    second = maintenance.prune_consumed_keys(now=far)
    assert (second["deleted"], second["batch_limit_reached"]) == (1, False)
    assert await arena["counts"]() == (0, 0)


async def test_beat_entry_is_a_bounded_ledger_prune_not_a_compaction_runner():
    from app.infrastructure.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    entry = schedule["prune-consumed-telemetry-keys"]
    assert entry["task"] == "app.infrastructure.tasks.maintenance.prune_consumed_telemetry_keys"
    assert not any("compact" in str(item["task"]) for item in schedule.values())


async def test_downgrade_guard_text_names_the_protection():
    from pathlib import Path

    source = (Path(__file__).parents[2] / "migrations/versions/0049_telemetry_replay_ledger.py").read_text()
    assert "Cannot downgrade 0049" in source and "late_replay_horizon_days is set" in source


async def test_raised_raw_retention_still_recognises_consumed_keys_and_never_claims_fresh_ones(arena):
    """Codex P1-1: the aggregate, not today's raw-retention setting, decides whether a day is compacted."""
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    await arena["compact"]()                         # compacted under 365 days; tombstones for a and b
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "2", 1, "b", retention_days=730) is LateMergeOutcome.DUPLICATE
        await session.commit()
    async with arena["factory"]() as session:
        # A genuinely fresh record stays raw and is NOT claimed merely to perform the duplicate check.
        assert await arena["deliver"](session, "9", 40, "fresh", retention_days=730) is LateMergeOutcome.STAYS_RAW
        await session.commit()
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("3"))
    assert await arena["counts"]() == (1, 2)         # only the fresh raw row exists; no tombstone was added for it
    # A day with no aggregate is untouched by the check.
    async with arena["factory"]() as session:
        assert await arena["deliver"](session, "5", 10, "other-day", base=BASE + timedelta(days=3),
                                      retention_days=730) is LateMergeOutcome.STAYS_RAW
        await session.commit()


async def test_raised_raw_retention_still_applies_the_finite_horizon_to_a_compacted_day(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](100)
    async with arena["factory"]() as session:
        with pytest.raises(ReplayHorizonExceeded):
            await arena["deliver"](session, "9", 40, "old-fresh", retention_days=730)
        await session.rollback()
    assert await arena["counts"]() == (0, 1)


async def test_clearing_the_horizon_is_refused_once_pruning_has_begun(arena):
    """Codex P1-2: NULL mode cannot reconstruct pruned tombstones, so the transition is refused."""
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](5)
    async with arena["factory"]() as session:
        await guard_horizon_clear(session, now=NOW)       # allowed before any prune
        await session.rollback()
    maintenance.prune_consumed_keys(now=datetime(2026, 10, 30, tzinfo=UTC))
    async with arena["factory"]() as session:
        with pytest.raises(UnsafeHorizonClear):
            await guard_horizon_clear(session, now=NOW)
        await session.rollback()
        state = (await session.execute(select(TelemetryReplayLedgerState))).scalar_one()
        assert state.pruning_begun_at is not None


async def test_a_clear_and_a_prune_are_serialised_so_the_prune_skips_after_the_clear_commits(arena):
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    await arena["compact"]()
    await arena["set_horizon"](5)
    far = datetime(2026, 10, 30, tzinfo=UTC)
    clearer = arena["factory"]()
    async with clearer:
        await guard_horizon_clear(clearer, now=far)       # holds the state lock, nothing pruned yet
        await clearer.execute(update(MonitoringPolicy).values(late_replay_horizon_days=None))
        pruning = asyncio.create_task(asyncio.to_thread(maintenance.prune_consumed_keys, now=far))
        await arena["wait"]()
        assert not pruning.done()
        await clearer.commit()
        result = await pruning
    assert result == {"skipped": True, "reason": "horizon_unset", "deleted": 0}
    assert await arena["counts"]() == (0, 2)
    async with arena["factory"]() as session:
        state = (await session.execute(select(TelemetryReplayLedgerState))).scalar_one()
        assert state.pruning_begun_at is None


async def test_a_prune_in_flight_makes_a_concurrent_clear_wait_and_then_refuse(arena):
    await arena["seed_raw"]([("1", "a")])
    await arena["compact"]()
    await arena["set_horizon"](5)
    far = datetime(2026, 10, 30, tzinfo=UTC)
    pruner, clearer = arena["factory"](), arena["factory"]()
    async with pruner, clearer:
        # The pruner holds the state lock and has recorded the marker, uncommitted: the same first step as a real prune run.
        await pruner.execute(text("SELECT 1 FROM telemetry_replay_ledger_state WHERE id = 1 FOR UPDATE"))
        await pruner.execute(update(TelemetryReplayLedgerState).values(pruning_begun_at=far))
        clear = asyncio.create_task(guard_horizon_clear(clearer, now=far))
        await arena["wait"]()
        assert not clear.done()                       # a real overlap: the clear is waiting on the prune
        await pruner.commit()
        with pytest.raises(UnsafeHorizonClear):
            await clear
        await clearer.rollback()


@pytest.mark.parametrize("retention_days", [365, 730], ids=["compacted_branch", "raised_retention_branch"])
async def test_null_mode_decision_is_protected_from_the_first_finite_prune(arena, monkeypatch, retention_days):
    """Codex P1: pause a NULL-mode ingest before its claim/check, enable a finite horizon and start the real prune. The
    prune must wait for the ingest, so the key is still tombstoned when the ingest decides."""
    await arena["seed_raw"]([("1", "a"), ("2", "b")])
    await arena["compact"]()
    reached, release = asyncio.Event(), asyncio.Event()
    name = "claim_consumed_keys" if retention_days == 365 else "consumed_key_exists"
    original = getattr(telemetry_retention, name)

    async def paused(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(telemetry_retention, name, paused)
    far = datetime(2026, 10, 30, tzinfo=UTC)
    ingest_session = arena["factory"]()
    async with ingest_session:
        ingest = asyncio.create_task(arena["deliver"](ingest_session, "2", 1, "b", now=far, retention_days=retention_days))
        await asyncio.wait_for(reached.wait(), 15)    # the NULL decision is made; the claim/check has not run yet
        await arena["set_horizon"](5)                 # a finite horizon is enabled and committed meanwhile
        pruning = asyncio.create_task(asyncio.to_thread(maintenance.prune_consumed_keys, now=far))
        await arena["wait"]()
        assert not pruning.done()                     # the prune waits for the in-flight decision
        release.set()
        assert await ingest is LateMergeOutcome.DUPLICATE
        await ingest_session.commit()
        result = await pruning
    assert result["skipped"] is False and result["deleted"] == 2     # it prunes only after the ingest finished
    agg = await arena["aggregate"]()
    assert (agg.sample_count, agg.sum_value) == (2, D("3"))
    assert await arena["counts"]() == (0, 0)
