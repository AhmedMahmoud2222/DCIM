"""Issue #128 / G4-A on PostgreSQL: exact daily aggregates through the real compaction and late-merge code."""

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.application import telemetry_retention
from app.application.telemetry_retention import compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.aggregate_math import MAX_COUNT, AggregateArithmeticError
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    TelemetryReading,
    telemetry_series_key,
)

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
BASE = datetime(2025, 8, 15, 6, tzinfo=UTC)  # far older than any tested retention policy


def day_at(offset_days: int, minutes: int = 0) -> datetime:
    return BASE + timedelta(days=offset_days, minutes=minutes)


@pytest.fixture
async def world(db_session):
    collector = Collector(id=uuid.uuid4(), name=f"g4a-{uuid.uuid4().hex}", collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=NOW)
    integration = Integration(id=uuid.uuid4(), name=f"g4a-{uuid.uuid4().hex}", integration_type="snmp",
                              target_host="192.0.2.1", config={}, poll_interval_seconds=300)
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="p",
                                       canonical_metric="power_kw", unit="kW", scale=1)
    db_session.add_all((collector, integration, mapping))
    await db_session.flush()
    series_key = telemetry_series_key(integration.id, None, "sensor", "power_kw", "kW")

    def make(value, occurred_at):
        return TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id,
            managed_asset_id=None, external_identifier="sensor", series_key=series_key, dedup_key=uuid.uuid4().hex,
            metric="power_kw", unit="kW", value=D(value), occurred_at=occurred_at, received_at=occurred_at, attributes={})

    async def add(values, *, day=0, start_minute=0):
        rows = [make(v, day_at(day, start_minute + i)) for i, v in enumerate(values)]
        db_session.add_all(rows)
        await db_session.flush()
        return rows

    async def late(values, *, day=0, start_minute=600, retention_days=365):
        """Deliver values the way ingest does: insert, merge, delete the merged reading."""
        merged = []
        for i, v in enumerate(values):
            reading = make(v, day_at(day, start_minute + i))
            db_session.add(reading)
            await db_session.flush()
            ok = await merge_late_reading(db_session, reading, now=NOW, retention_days=retention_days)
            merged.append(ok)
            if ok:
                await db_session.delete(reading)
                await db_session.flush()
        return merged

    async def aggregate(day=0):
        await db_session.flush()
        return (await db_session.execute(
            select(DailyTelemetryAggregate).where(DailyTelemetryAggregate.series_key == series_key,
                                                  DailyTelemetryAggregate.day == day_at(day).date())
            .execution_options(populate_existing=True))).scalar_one()

    async def raw_count():
        return (await db_session.execute(select(text("count(*)")).select_from(TelemetryReading))).scalar_one()

    return {"add": add, "late": late, "aggregate": aggregate, "raw_count": raw_count, "series_key": series_key,
            "integration": integration, "make": make}


def snapshot(agg):
    return (agg.sum_value, agg.average_value, agg.minimum_value, agg.maximum_value, agg.sample_count, agg.arithmetic_basis)


async def test_issue_case_a_compaction_keeps_average_min_and_max_exactly(db_session, world):
    await world["add"](["9999999999.12345678"] * 3)
    assert await compact_eligible_raw(db_session, now=NOW, retention_days=365) == 1
    agg = await world["aggregate"]()
    assert (agg.average_value, agg.minimum_value, agg.maximum_value) == (D("9999999999.12345678"),) * 3
    assert agg.sum_value == D("29999999997.37037034") and agg.sample_count == 3
    assert agg.arithmetic_basis == "decimal_exact" and agg.legacy_sample_count is None
    assert await world["raw_count"]() == 0


async def test_issue_case_f_a_tie_in_the_mean_does_not_inflate_later_late_merges(db_session, world):
    await world["add"](["0", "0.00000001"])
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    assert all(await world["late"](["0"] * 10))
    agg = await world["aggregate"]()
    assert (agg.sum_value, agg.sample_count, agg.average_value) == (D("0.00000001"), 12, D("0.00000000"))  # was 1e-8 before G4-A


async def test_the_same_samples_give_identical_aggregates_however_they_are_batched_or_merged(db_session, world):
    rng = random.Random(1281)
    values = [str(D(rng.randint(-10**12, 10**12)).scaleb(-8)) for _ in range(40)]
    await world["add"](values, day=0)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    # staged compaction: 10 then 30 raw rows folded into the existing aggregate
    await world["add"](values[:10], day=1)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    await world["add"](values[10:], day=1, start_minute=100)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    # 5 compacted, 35 delivered one by one as late merges
    await world["add"](values[:5], day=2)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    assert all(await world["late"](values[5:], day=2))
    expected = sum(D(v) for v in values)
    snapshots = [snapshot(await world["aggregate"](day)) for day in (0, 1, 2)]
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert snapshots[0][0] == expected and snapshots[0][4] == 40
    assert snapshots[0][2:4] == (min(D(v) for v in values), max(D(v) for v in values))


async def test_negative_and_zero_sums_and_ties_round_half_even(db_session, world):
    await world["add"](["-0.00000003", "0"], day=0)   # -1.5 units -> -2
    await world["add"](["-3", "-4"], day=1)
    await world["add"](["-5", "5"], day=2)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    assert (await world["aggregate"](0)).average_value == D("-0.00000002")
    assert (await world["aggregate"](1)).average_value == D("-3.50000000")
    zero = await world["aggregate"](2)
    assert (zero.sum_value, zero.average_value) == (D("0.00000000"), D("0.00000000"))


async def test_retention_policy_still_decides_which_days_compact_and_utc_days_are_split_exactly(db_session, world):
    rows = await world["add"](["1", "2"], day=0)  # BASE day, about 400 days before NOW
    boundary_before = world["make"]("5", datetime(2025, 8, 15, 23, 59, 59, 999999, tzinfo=UTC))
    boundary_after = world["make"]("7", datetime(2025, 8, 16, 0, 0, 0, tzinfo=UTC))
    db_session.add_all((boundary_before, boundary_after))
    await db_session.flush()
    assert len(rows) == 2
    assert await compact_eligible_raw(db_session, now=NOW, retention_days=730) == 0   # still inside the policy
    assert await world["raw_count"]() == 4
    assert await compact_eligible_raw(db_session, now=NOW, retention_days=365) == 2
    first, second = await world["aggregate"](0), await world["aggregate"](1)
    assert (first.sample_count, first.sum_value) == (3, D("8.00000000"))   # 1, 2 and 23:59:59.999999
    assert (second.sample_count, second.sum_value) == (1, D("7.00000000"))  # 00:00:00 starts the next UTC day
    # a policy change between compaction and a late merge changes nothing about the arithmetic
    assert await world["late"](["3"], retention_days=90) == [True]
    assert snapshot(await world["aggregate"](0))[:2] == (D("11.00000000"), D("2.75000000"))


async def _insert_legacy(db_session, world, *, day, average, minimum, maximum, count):
    db_session.add(DailyTelemetryAggregate(
        integration_id=world["integration"].id, managed_asset_id=None, external_identifier="sensor",
        series_key=world["series_key"], metric="power_kw", unit="kW", day=day_at(day).date(),
        average_value=D(average), minimum_value=D(minimum), maximum_value=D(maximum), sample_count=count))
    await db_session.flush()


async def test_legacy_aggregate_is_seeded_on_first_fold_and_labelled_and_untouched_days_stay_legacy(db_session, world):
    await _insert_legacy(db_session, world, day=0, average="2", minimum="1", maximum="3", count=3)
    await _insert_legacy(db_session, world, day=1, average="9", minimum="9", maximum="9", count=2)
    before = await world["aggregate"](0)
    assert (before.sum_value, before.arithmetic_basis, before.legacy_sample_count) == (None, None, None)
    assert await world["late"](["5"]) == [True]
    seeded = await world["aggregate"](0)
    assert seeded.arithmetic_basis == "legacy_seeded" and seeded.legacy_sample_count == 3
    assert (seeded.sum_value, seeded.sample_count, seeded.average_value) == (D("11.00000000"), 4, D("2.75000000"))
    assert (seeded.minimum_value, seeded.maximum_value) == (D("1.00000000"), D("5.00000000"))
    # later merges and a compaction fold are exact; legacy_sample_count never changes
    assert await world["late"](["0.00000001"] * 2, start_minute=700) == [True, True]
    await world["add"](["4", "6"], day=0, start_minute=800)
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    final = await world["aggregate"](0)
    assert final.arithmetic_basis == "legacy_seeded" and final.legacy_sample_count == 3
    assert (final.sum_value, final.sample_count) == (D("21.00000002"), 8)
    assert final.average_value == D("2.62500000")  # 21.00000002 / 8 = 2.6250000025 -> half-even at 8 decimals
    untouched = await world["aggregate"](1)
    assert (untouched.sum_value, untouched.arithmetic_basis, untouched.average_value) == (None, None, D("9.00000000"))


async def test_a_compaction_fold_into_a_legacy_row_seeds_it_too(db_session, world):
    await _insert_legacy(db_session, world, day=0, average="10", minimum="10", maximum="10", count=4)
    await world["add"](["20"])
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    agg = await world["aggregate"]()
    assert (agg.arithmetic_basis, agg.legacy_sample_count, agg.sum_value, agg.sample_count) == (
        "legacy_seeded", 4, D("60.00000000"), 5)
    assert agg.average_value == D("12.00000000")


@pytest.mark.parametrize("changes", [
    {"arithmetic_basis": "decimal_exact"},                                           # basis without a sum
    {"sum_value": D("3")},                                                           # sum without a basis
    {"arithmetic_basis": "mystery", "sum_value": D("3")},                            # unknown basis
    {"arithmetic_basis": "legacy_seeded", "sum_value": D("3")},                      # seeded row without legacy count
    {"arithmetic_basis": "decimal_exact", "sum_value": D("3"), "legacy_sample_count": 1},  # count on a non-seeded row
    {"arithmetic_basis": "legacy_seeded", "sum_value": D("3"), "legacy_sample_count": 4},  # more than sample_count
    {"arithmetic_basis": "legacy_seeded", "sum_value": D("3"), "legacy_sample_count": 0},
    {"arithmetic_basis": "decimal_exact", "sum_value": D("10")},                     # above count * maximum
    {"arithmetic_basis": "decimal_exact", "sum_value": D("2")},                      # below count * minimum
    {"legacy_sample_count": 1},                                                      # count on a legacy row
])
async def test_check_constraints_reject_inconsistent_aggregates(db_session, world, changes):
    fields = {"average_value": D("2"), "minimum_value": D("1"), "maximum_value": D("3"), "sample_count": 3}
    fields.update(changes)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(DailyTelemetryAggregate(
                integration_id=world["integration"].id, managed_asset_id=None, external_identifier="sensor",
                series_key=world["series_key"], metric="power_kw", unit="kW", day=day_at(0).date(), **fields))
            await db_session.flush()


async def test_constraints_accept_each_valid_state(db_session, world):
    for day, extra in enumerate((
        {},
        {"arithmetic_basis": "decimal_exact", "sum_value": D("6")},
        {"arithmetic_basis": "legacy_seeded", "sum_value": D("7"), "legacy_sample_count": 2},
    )):
        db_session.add(DailyTelemetryAggregate(
            integration_id=world["integration"].id, managed_asset_id=None, external_identifier="sensor",
            series_key=world["series_key"], metric="power_kw", unit="kW", day=day_at(day).date(),
            average_value=D("2"), minimum_value=D("1"), maximum_value=D("3"), sample_count=3, **extra))
    await db_session.flush()
    convalidated = (await db_session.execute(text(
        "SELECT bool_and(convalidated) FROM pg_constraint WHERE conrelid = 'daily_telemetry_aggregate'::regclass "
        "AND conname LIKE 'ck_daily_telemetry_aggregate_%'"))).scalar()
    assert convalidated is True  # the migration validated every CHECK after adding it NOT VALID


async def test_count_limit_aborts_without_touching_the_aggregate_or_deleting_raw_evidence(db_session, world):
    db_session.add(DailyTelemetryAggregate(
        integration_id=world["integration"].id, managed_asset_id=None, external_identifier="sensor",
        series_key=world["series_key"], metric="power_kw", unit="kW", day=day_at(0).date(),
        average_value=D("0"), minimum_value=D("0"), maximum_value=D("0"), sample_count=MAX_COUNT,
        sum_value=D("0"), arithmetic_basis="decimal_exact"))
    await db_session.flush()
    with pytest.raises(AggregateArithmeticError, match="INTEGER"):
        await world["late"](["0"])
    rows = await world["add"](["0", "0"], start_minute=900)
    with pytest.raises(AggregateArithmeticError, match="INTEGER"):
        await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    assert await world["raw_count"]() >= len(rows)  # the raw rows are still there
    assert (await world["aggregate"]()).sample_count == MAX_COUNT


async def test_sum_limit_aborts_without_touching_the_aggregate(db_session, world):
    largest = D("9" * 30 + "." + "9" * 8)  # the NUMERIC(38, 8) maximum
    db_session.add(DailyTelemetryAggregate(
        integration_id=world["integration"].id, managed_asset_id=None, external_identifier="sensor",
        series_key=world["series_key"], metric="power_kw", unit="kW", day=day_at(0).date(),
        average_value=D("9999999999.99999999"), minimum_value=D("1"), maximum_value=D("9999999999.99999999"),
        sample_count=1, sum_value=largest, arithmetic_basis="legacy_seeded", legacy_sample_count=1))
    await db_session.flush()
    with pytest.raises(AggregateArithmeticError, match="NUMERIC"):
        await world["late"](["0.00000001"])
    agg = await world["aggregate"]()
    assert (agg.sum_value, agg.sample_count) == (largest, 1)


async def test_a_failure_after_the_aggregate_flush_keeps_raw_rows_and_the_old_aggregate(db_session, world, monkeypatch):
    await world["add"](["1", "2", "3"])
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    await db_session.commit()
    await world["add"](["10", "20"], start_minute=300)
    await db_session.commit()

    def exploding_delete(*_args, **_kwargs):
        raise RuntimeError("injected failure between the aggregate flush and the raw delete")

    monkeypatch.setattr(telemetry_retention, "delete", exploding_delete)
    with pytest.raises(RuntimeError, match="injected"):
        await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    await db_session.rollback()
    assert await world["raw_count"]() == 2
    agg = await world["aggregate"]()
    assert (agg.sum_value, agg.sample_count, agg.average_value) == (D("6.00000000"), 3, D("2.00000000"))
