"""Issue #128 / G1: immutable mapping revisions and event-time contract pinning, against real PostgreSQL."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.alarm_service import evaluate_reading  # noqa: F401  (alarm path is exercised by ingest)
from app.application.mapping_revision_service import append_mapping_revision
from app.application.telemetry_retention import compact_eligible_raw
from app.application.telemetry_service import (
    AmbiguousMappingContract,
    ConversionContractDrift,
    MappingRevisionMismatch,
    UnknownMappingRevision,
    ingest_reading,
)
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry import registry
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    IntegrationMetricMappingRevision,
    TelemetryReading,
)
from tests.conftest import TEST_DATABASE_URL


async def _world(db, *, unit="degF", metric="temperature_c", source="sensor", registry_version="1"):
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=datetime.now(UTC))
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db.add_all([collector, integration])
    await db.flush()
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier=source,
                                       canonical_metric=metric, unit=unit, scale=1, registry_version=registry_version)
    db.add(mapping)
    await db.flush()
    return collector, integration, mapping


def _ingest(db, collector, integration, *, value, key=None, source="sensor", occurred_at=None, **pin):
    return ingest_reading(
        db, collector_id=collector.id, integration_id=integration.id, dedup_key=key or uuid.uuid4().hex,
        external_identifier="ext", source_identifier=source,
        occurred_at=occurred_at or datetime.now(UTC) + timedelta(seconds=1), value=value, **pin,
    )


async def test_new_mapping_starts_with_an_authored_revision_one_and_a_current_pointer(db_session):
    _, _, mapping = await _world(db_session)
    revision = await db_session.get(IntegrationMetricMappingRevision, mapping.current_revision_id)
    assert (revision.revision, revision.provenance, revision.source_unit, revision.registry_version) == (1, "authored", "degF", "1")
    assert revision.integration_id == mapping.integration_id and revision.source_identifier == "sensor"
    assert len(revision.conversion_hash) == 64


async def test_revisions_are_append_only_in_the_database(db_session):
    _, _, mapping = await _world(db_session)
    rev = mapping.current_revision_id
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE integration_metric_mapping_revision SET source_unit = 'degC' WHERE id = :i"), {"i": rev})
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM integration_metric_mapping_revision WHERE id = :i"), {"i": rev})
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE integration_metric_mapping_revision SET effective_from = now() WHERE id = :i"), {"i": rev})


async def test_deleting_a_mapping_removes_its_revisions_but_nothing_else_can(db_session):
    _, _, mapping = await _world(db_session)
    await db_session.execute(text("UPDATE integration_metric_mapping SET current_revision_id = NULL WHERE id = :i"), {"i": mapping.id})
    await db_session.execute(text("DELETE FROM integration_metric_mapping WHERE id = :i"), {"i": mapping.id})
    assert (await db_session.execute(text("SELECT count(*) FROM integration_metric_mapping_revision WHERE mapping_id = :i"), {"i": mapping.id})).scalar_one() == 0


async def test_68_fahrenheit_queued_under_revision_one_stays_20_celsius_after_the_source_becomes_celsius(db_session):
    collector, integration, mapping = await _world(db_session, unit="degF")
    rev1 = mapping.current_revision_id
    rule = AlarmRule(id=uuid.uuid4(), integration_id=integration.id, metric="temperature_c", rule_type="threshold_high",
                     threshold=Decimal("25"), unit="degC", registry_version="1", name="hot")
    db_session.add(rule)
    rev2 = (await append_mapping_revision(db_session, mapping.id, unit="degC")).id
    assert rev2 != rev1 and mapping.current_revision_id == rev2 and mapping.unit == "degC"

    old = await _ingest(db_session, collector, integration, value=68, mapping_revision_id=rev1)
    new = await _ingest(db_session, collector, integration, value=68, mapping_revision_id=rev2)
    old_row = await db_session.get(TelemetryReading, old.reading_id)
    new_row = await db_session.get(TelemetryReading, new.reading_id)
    assert (old_row.value, old_row.unit, old_row.raw_value, old_row.raw_unit) == (Decimal("20.00000000"), "degC", Decimal("68"), "degF")
    assert (old_row.mapping_revision_id, old_row.contract_evidence) == (rev1, "pinned")
    assert (new_row.value, new_row.raw_unit, new_row.mapping_revision_id) == (Decimal("68.00000000"), "degC", rev2)
    # Alarm evaluation used the pinned meaning: only the genuinely 68 degC sample breaches the 25 degC threshold.
    alarms = (await db_session.execute(select(Alarm).where(Alarm.rule_id == rule.id))).scalars().all()
    assert [alarm.telemetry_reading_id for alarm in alarms] == [new.reading_id]


async def test_a_pinned_late_reading_survives_compaction_and_aggregates_in_its_original_meaning(db_session):
    collector, integration, mapping = await _world(db_session, unit="degF")
    rev1 = mapping.current_revision_id
    await append_mapping_revision(db_session, mapping.id, unit="degC")
    now = datetime.now(UTC)
    old = (now - timedelta(days=400)).replace(hour=3, minute=0)  # fixed mid-day: all samples share one UTC day
    await _ingest(db_session, collector, integration, value=68, mapping_revision_id=rev1, occurred_at=old)
    await _ingest(db_session, collector, integration, value=86, mapping_revision_id=rev1, occurred_at=old + timedelta(hours=1))
    await compact_eligible_raw(db_session, now=now)
    aggregate = (await db_session.execute(select(DailyTelemetryAggregate))).scalar_one()
    assert aggregate.unit == "degC" and aggregate.sample_count == 2
    assert float(aggregate.minimum_value) == pytest.approx(20) and float(aggregate.maximum_value) == pytest.approx(30)
    # A third pinned sample arriving after compaction merges in its original meaning too.
    late = await _ingest(db_session, collector, integration, value=104, mapping_revision_id=rev1, occurred_at=old + timedelta(hours=2))
    assert late.reading_id is not None
    await db_session.flush()
    await db_session.refresh(aggregate)
    assert aggregate.sample_count == 3 and float(aggregate.maximum_value) == pytest.approx(40)


async def test_unpinned_record_is_held_once_a_second_revision_exists(db_session):
    collector, integration, mapping = await _world(db_session, unit="degF")
    await append_mapping_revision(db_session, mapping.id, unit="degC")
    with pytest.raises(AmbiguousMappingContract) as held:
        await _ingest(db_session, collector, integration, value=68)
    assert held.value.reason == "MULTIPLE_REVISIONS"
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


async def test_unpinned_record_on_a_never_changed_mapping_is_inferred_but_labelled(db_session):
    collector, integration, _ = await _world(db_session, unit="degF")
    result = await _ingest(db_session, collector, integration, value=68)
    row = await db_session.get(TelemetryReading, result.reading_id)
    assert (row.value, row.contract_evidence) == (Decimal("20.00000000"), "inferred_single_revision")
    assert row.mapping_revision_id is not None


async def test_unpinned_record_older_than_the_only_revision_is_held(db_session):
    collector, integration, _ = await _world(db_session, unit="degF")
    with pytest.raises(AmbiguousMappingContract) as held:
        await _ingest(db_session, collector, integration, value=68, occurred_at=datetime.now(UTC) - timedelta(hours=1))
    assert held.value.reason == "BEFORE_REVISION_EFFECTIVE"
    # ...but the same old record pinned to that revision is the collector's own evidence and is accepted.
    mapping = (await db_session.execute(select(IntegrationMetricMapping))).scalar_one()
    result = await _ingest(db_session, collector, integration, value=68, occurred_at=datetime.now(UTC) - timedelta(hours=1),
                           mapping_revision_id=mapping.current_revision_id)
    assert result.reading_id is not None


async def test_a_mapping_without_any_revision_is_ambiguous_never_trusted(db_session):
    collector, integration, mapping = await _world(db_session)
    await db_session.execute(text("UPDATE integration_metric_mapping SET current_revision_id = NULL WHERE id = :i"), {"i": mapping.id})
    await db_session.execute(text("ALTER TABLE integration_metric_mapping_revision DISABLE TRIGGER trg_mapping_revision_append_only"))
    await db_session.execute(text("DELETE FROM integration_metric_mapping_revision WHERE mapping_id = :i"), {"i": mapping.id})
    with pytest.raises(AmbiguousMappingContract) as held:
        await _ingest(db_session, collector, integration, value=1)
    assert held.value.reason == "NO_REVISION_RECORDED"


async def test_cross_source_cross_integration_unknown_and_contradicting_pins_are_rejected(db_session):
    collector, integration, mapping = await _world(db_session, source="a")
    other = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="b",
                                     canonical_metric="temperature_c", unit="degC", scale=1, registry_version="1")
    db_session.add(other)
    _, other_integration, foreign = await _world(db_session, source="a")
    await db_session.flush()
    with pytest.raises(MappingRevisionMismatch):  # revision of source b pinned on a record for source a
        await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=other.current_revision_id)
    with pytest.raises(MappingRevisionMismatch):  # revision of another integration's identically named source
        await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=foreign.current_revision_id)
    with pytest.raises(UnknownMappingRevision):
        await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=uuid.uuid4())
    with pytest.raises(MappingRevisionMismatch):  # echoed unit contradicts the pinned revision
        await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=mapping.current_revision_id, source_unit="degC")
    with pytest.raises(MappingRevisionMismatch):  # echoed scale contradicts the pinned revision
        await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=mapping.current_revision_id, source_scale=Decimal("2"))
    ok = await _ingest(db_session, collector, integration, value=1, source="a", mapping_revision_id=mapping.current_revision_id,
                       source_unit="degF", source_scale=Decimal("1.0"))
    assert ok.reading_id is not None
    assert other_integration.id != integration.id


async def test_dedup_semantics_are_unchanged_across_revisions_and_ambiguity(db_session):
    collector, integration, mapping = await _world(db_session, unit="degF")
    rev1 = mapping.current_revision_id
    first = await _ingest(db_session, collector, integration, key="k1", value=68, mapping_revision_id=rev1)
    rev2 = (await append_mapping_revision(db_session, mapping.id, unit="degC")).id
    again = await _ingest(db_session, collector, integration, key="k1", value=99, mapping_revision_id=rev2)
    assert (again.duplicate, again.reading_id) == (True, None)
    # The delivered record is not re-converted: it still has the first value and revision.
    stored = await db_session.get(TelemetryReading, first.reading_id)
    assert (stored.value, stored.mapping_revision_id) == (Decimal("20.00000000"), rev1)
    # An unpinned replay of an already stored key answers duplicate; it is not held again.
    replay = await _ingest(db_session, collector, integration, key="k1", value=68)
    assert replay.duplicate is True


async def test_registry_drift_is_refused_for_a_pinned_revision(db_session, monkeypatch):
    collector, integration, mapping = await _world(db_session, unit="degF")
    monkeypatch.setitem(registry.UNITS, "degF", registry.UnitDefinition("degF", "temperature", Decimal("0.56"), Decimal("-32")))
    with pytest.raises(ConversionContractDrift):
        await _ingest(db_session, collector, integration, value=68, mapping_revision_id=mapping.current_revision_id)
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


async def test_legacy_pre_registry_mapping_keeps_its_scaled_source_unit_semantics(db_session):
    from sqlalchemy import null
    collector, integration, _ = await _world(db_session, unit="widgets", registry_version=null())
    result = await _ingest(db_session, collector, integration, value=Decimal("2.5"))
    row = await db_session.get(TelemetryReading, result.reading_id)
    assert (row.unit, row.value, row.registry_version, row.contract_evidence) == ("widgets", Decimal("2.5"), None, "inferred_single_revision")


async def test_legacy_readings_without_a_revision_remain_unverified_not_rewritten(db_session):
    collector, integration, mapping = await _world(db_session)
    legacy = TelemetryReading(
        id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id,
        external_identifier="x", series_key="legacy", dedup_key="legacy", metric="temperature_c", unit="degC", value=1,
        occurred_at=datetime.now(UTC), received_at=datetime.now(UTC), attributes={},
    )
    db_session.add(legacy)
    await db_session.flush()
    assert (legacy.mapping_revision_id, legacy.contract_evidence) == (None, None)
    with pytest.raises(DBAPIError, match="revision_matches_evidence"):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE telemetry_reading SET contract_evidence = 'pinned' WHERE id = :i"), {"i": legacy.id})


# ---- concurrency: real, separate connections ------------------------------------------------------------

@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=10)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def _committed_world(sessions):
    async with sessions() as db:
        collector, integration, mapping = await _world(db, unit="degF")
        ids = SimpleNamespace(collector=collector.id, integration=integration.id, mapping=mapping.id, rev1=mapping.current_revision_id)
        await db.commit()
    return ids


async def test_concurrent_revision_creation_yields_consecutive_unique_revisions(sessions):
    ids = await _committed_world(sessions)

    async def append(unit):
        async with sessions() as db:
            revision = await append_mapping_revision(db, ids.mapping, unit=unit)
            await db.commit()
            return revision.revision

    numbers = await asyncio.gather(*(append(unit) for unit in ["degC", "degF", "K", "degC", "degF"]))
    assert sorted(numbers) == [2, 3, 4, 5, 6]
    async with sessions() as db:
        mapping = await db.get(IntegrationMetricMapping, ids.mapping)
        newest = await db.get(IntegrationMetricMappingRevision, mapping.current_revision_id)
        assert newest.revision == 6 and newest.source_unit == mapping.unit


async def test_ingest_racing_a_revision_change_keeps_every_pinned_record_on_its_original_contract(sessions):
    ids = await _committed_world(sessions)

    async def ingest(index):
        async with sessions() as db:
            result = await ingest_reading(
                db, collector_id=ids.collector, integration_id=ids.integration, dedup_key=f"race-{index}",
                external_identifier="ext", source_identifier="sensor", occurred_at=datetime.now(UTC), value=68,
                mapping_revision_id=ids.rev1,
            )
            await db.commit()
            return result.reading_id

    async def change():
        async with sessions() as db:
            await append_mapping_revision(db, ids.mapping, unit="degC")
            await db.commit()

    results = await asyncio.gather(*(ingest(i) for i in range(10)), change(), *(ingest(i) for i in range(10, 20)))
    assert all(item is not None for item in results[:10] + results[11:])
    async with sessions() as db:
        rows = (await db.execute(select(TelemetryReading).where(TelemetryReading.integration_id == ids.integration))).scalars().all()
    assert len(rows) == 20
    assert {(row.value, row.unit, row.raw_unit, row.mapping_revision_id) for row in rows} == {
        (Decimal("20.00000000"), "degC", "degF", ids.rev1)
    }


async def test_concurrent_duplicate_delivery_stores_exactly_one_reading(sessions):
    ids = await _committed_world(sessions)

    async def deliver():
        async with sessions() as db:
            result = await ingest_reading(
                db, collector_id=ids.collector, integration_id=ids.integration, dedup_key="same",
                external_identifier="ext", source_identifier="sensor", occurred_at=datetime.now(UTC), value=68,
                mapping_revision_id=ids.rev1,
            )
            await db.commit()
            return result.duplicate

    outcomes = await asyncio.gather(*(deliver() for _ in range(8)))
    assert sorted(outcomes) == [False] + [True] * 7
