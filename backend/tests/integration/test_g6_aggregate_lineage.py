"""G6 PostgreSQL evidence: lineage is atomic with G4-R claims and aggregate folds."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.application import telemetry_retention
from app.application.telemetry_retention import LateMergeOutcome, compact_eligible_raw, merge_late_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    DailyTelemetryAggregateLineage,
    IntegrationMetricMapping,
    IntegrationMetricMappingRevision,
    TelemetryConsumedKey,
    TelemetryReading,
    revision_values,
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
            metric="power_kw", unit="kW", value=D(value), occurred_at=occurred_at, received_at=occurred_at,
            mapping_revision_id=mapping.current_revision_id, contract_evidence="pinned",
            raw_unit="kW", source_scale=D("1"), attributes={"private": "not archived"})

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
            ok = bool(await merge_late_reading(db_session, reading, now=NOW, retention_days=retention_days))
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
            "integration": integration, "make": make, "mapping": mapping}


async def lineage(db):
    await db.flush()
    return (await db.execute(select(DailyTelemetryAggregateLineage).execution_options(populate_existing=True))).scalars().all()


async def test_compaction_tracks_contracts_and_late_merge_upserts_without_source_values(db_session, world):
    second = IntegrationMetricMappingRevision(**revision_values(
        world["mapping"], revision=2, provenance="authored", effective_from=NOW))
    db_session.add(second)
    await db_session.flush()
    rows = [world["make"]("1", day_at(0)), world["make"]("2", day_at(0, 1)), world["make"]("3", day_at(0, 2))]
    rows[2].mapping_revision_id = second.id
    rows[2].raw_unit = "W"
    db_session.add_all(rows)
    await db_session.flush()
    assert await compact_eligible_raw(db_session, now=NOW, retention_days=365) == 1
    agg = await world["aggregate"]()
    assert (agg.lineage_sample_count, agg.sample_count, agg.evidence_class) == (3, 3, "pinned")
    summaries = await lineage(db_session)
    assert sorted(row.sample_count for row in summaries) == [1, 2]
    assert {row.raw_unit for row in summaries} == {"W", "kW"}
    assert (agg.first_compacted_at, agg.raw_retention_days_at_compaction) == (NOW, 365)
    assert await world["late"](["4"]) == [True]
    assert sorted(row.sample_count for row in await lineage(db_session)) == [1, 3]
    assert (await world["aggregate"]()).lineage_sample_count == 4
    assert await compact_eligible_raw(db_session, now=NOW, retention_days=365) == 0
    assert await world["raw_count"]() == 0
    assert not hasattr(summaries[0], "attributes") and not hasattr(summaries[0], "raw_value_text")


async def test_prelineage_history_is_not_fabricated_and_new_contributors_are_partial(db_session, world):
    db_session.add(DailyTelemetryAggregate(
        integration_id=world["integration"].id, external_identifier="sensor", series_key=world["series_key"],
        metric="power_kw", unit="kW", day=day_at(0).date(), average_value=D("2"),
        minimum_value=D("1"), maximum_value=D("3"), sample_count=3))
    await db_session.flush()
    assert (await world["aggregate"]()).lineage_sample_count is None
    assert await lineage(db_session) == []
    assert await world["late"](["5"]) == [True]
    agg = await world["aggregate"]()
    assert (agg.lineage_sample_count, agg.sample_count, agg.evidence_class) == (1, 4, "mixed")
    assert (agg.arithmetic_basis, agg.legacy_sample_count) == ("legacy_seeded", 3)


async def test_duplicate_delivery_never_inflates_lineage_or_aggregate(db_session, world):
    original = (await world["add"](["2"]))[0]
    key = original.dedup_key
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    duplicate = world["make"]("999", day_at(0))
    duplicate.dedup_key = key
    db_session.add(duplicate)
    await db_session.flush()
    assert await merge_late_reading(db_session, duplicate, now=NOW, retention_days=365) is LateMergeOutcome.DUPLICATE
    await db_session.delete(duplicate)
    await db_session.flush()
    agg = await world["aggregate"]()
    assert (agg.sample_count, agg.lineage_sample_count, agg.sum_value) == (1, 1, D("2"))
    assert (await lineage(db_session))[0].sample_count == 1


async def test_lineage_failure_rolls_back_claims_aggregate_and_raw_consumption(db_session, world, monkeypatch):
    await world["add"](["1", "2"])
    await db_session.commit()
    async def fail(*args, **kwargs):
        raise RuntimeError("injected lineage persistence failure")
    monkeypatch.setattr(telemetry_retention, "record_lineage", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    await db_session.rollback()
    assert await world["raw_count"]() == 2
    assert await lineage(db_session) == []
    assert (await db_session.execute(select(TelemetryConsumedKey))).scalars().all() == []
    assert (await db_session.execute(select(DailyTelemetryAggregate))).scalars().all() == []


async def test_legacy_contracts_are_recorded_as_unverified_without_claiming_a_revision(db_session, world):
    row = world["make"]("2", day_at(0))
    row.mapping_revision_id = row.contract_evidence = None
    row.raw_unit = row.source_scale = None
    db_session.add(row)
    await db_session.flush()
    await compact_eligible_raw(db_session, now=NOW, retention_days=365)
    agg = await world["aggregate"]()
    assert (agg.evidence_class, agg.lineage_sample_count) == ("unverified", 1)
    summary = (await lineage(db_session))[0]
    assert summary.mapping_revision_id is None and summary.source_scale is None
