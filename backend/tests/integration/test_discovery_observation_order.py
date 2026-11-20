"""DISC-ORDER-01 PostgreSQL proof: `ingest_discovery` is a monotonic, observation-time-based
upsert. Older and equal observations never overwrite newer discovered-device state, and
concurrent out-of-order deliveries and first-insert races converge on the newest
observation with creation events emitted exactly once.

The concurrency tests use real PostgreSQL row/unique-index locks with deterministic
barriers (a blocked session is observed to be blocked before the lock holder commits);
they contain no sleeps-as-synchronization other than a bounded "still blocked" probe."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import discovery_service
from app.application.discovery_service import ingest_discovery
from app.application.network.neighbor_service import MAX_FUTURE_SKEW

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
EXTERNAL_ID = "10.20.30.40"
BLOCKED_PROBE_SECONDS = 0.5


async def seed_integration(db_engine) -> uuid.UUID:
    integration_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, "
            "consecutive_failures, version) VALUES (:id, :n, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"),
            {"id": integration_id, "n": f"i-{integration_id}"})
    return integration_id


def factory_for(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


async def ingest(
    session, integration_id: uuid.UUID, *, observed_at: datetime | None,
    attrs: dict | None = None, external_identifier: str = EXTERNAL_ID,
):
    return await ingest_discovery(
        session, integration_id=integration_id, external_identifier=external_identifier,
        raw_attributes=attrs if attrs is not None else {"v": "x"}, correlation_id=None, causation_id=None,
        observed_at=observed_at,
    )


async def device_row(factory, integration_id, external_identifier=EXTERNAL_ID) -> dict | None:
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT id, discovered_at, last_seen_at, raw_attributes, profile_match, status, matched_managed_asset_id "
            "FROM discovered_device WHERE integration_id = :i AND external_identifier = :e"),
            {"i": integration_id, "e": external_identifier})).mappings().first()
        return dict(row) if row else None


async def counts(factory) -> dict:
    async with factory() as session:
        return {
            "devices": (await session.execute(text("SELECT count(*) FROM discovered_device"))).scalar_one(),
            "diffs": (await session.execute(text("SELECT count(*) FROM reconciliation_diff"))).scalar_one(),
            "discovered_events": (await session.execute(text(
                "SELECT count(*) FROM outbox_event WHERE event_type = 'DeviceDiscovered'"))).scalar_one(),
            "required_events": (await session.execute(text(
                "SELECT count(*) FROM outbox_event WHERE event_type = 'ReconciliationRequired'"))).scalar_one(),
        }


async def commit_ingest(
    factory, integration_id: uuid.UUID, *, observed_at: datetime | None,
    attrs: dict | None = None, external_identifier: str = EXTERNAL_ID,
):
    """`observed_at` is a required keyword on purpose: every test states its observation
    time, and `None` ("use the current time") must be written explicitly."""
    async with factory() as session:
        result = await ingest(
            session, integration_id, observed_at=observed_at, attrs=attrs, external_identifier=external_identifier)
        await session.commit()
        return result


# 1. creation and creation-only events -----------------------------------------------------------------------------


async def test_new_device_is_created_with_observation_time_and_creation_events(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    before = datetime.now(UTC)
    _device, is_new = await commit_ingest(factory, integration_id, observed_at=T0, attrs={"v": "first"})
    after = datetime.now(UTC)

    assert is_new is True
    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] == T0  # observation time, not receipt time
    assert before <= row["discovered_at"] <= after  # first Central receipt time
    assert row["raw_attributes"] == {"v": "first"} and row["status"] == "new"
    assert await counts(factory) == {"devices": 1, "diffs": 1, "discovered_events": 1, "required_events": 1}


# 2. newer observation updates -------------------------------------------------------------------------------------


async def test_newer_observation_updates_fields_and_refreshes_profile_match(db_engine, monkeypatch):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    resolutions: list[str] = []

    class _Resolution:
        def __init__(self, model):
            self.model = model

        def as_dict(self):
            return {"state": "matched", "model": self.model}

    async def fake_resolve(_db, facts):
        resolutions.append(facts.model)
        return _Resolution(facts.model)

    monkeypatch.setattr(discovery_service, "resolve_profile", fake_resolve)

    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"facts": {"model": "A"}})
    first = await device_row(factory, integration_id)
    _device, is_new = await commit_ingest(
        factory, integration_id, observed_at=T0 + timedelta(minutes=1), attrs={"facts": {"model": "B"}})

    assert is_new is False
    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] == T0 + timedelta(minutes=1)
    assert row["raw_attributes"] == {"facts": {"model": "B"}}
    assert row["profile_match"]["model"] == "B" and row["profile_match"]["facts"] == {"model": "B"}
    assert row["discovered_at"] == first["discovered_at"]  # first receipt time is never rewritten
    assert resolutions == ["A", "B"]
    # Update path is creation-only for events and the diff.
    assert await counts(factory) == {"devices": 1, "diffs": 1, "discovered_events": 1, "required_events": 1}


# 3 and 4. older / equal observations are a complete no-op ------------------------------------------------------


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(0)], ids=["older", "equal"])
async def test_older_or_equal_observation_leaves_the_complete_record_unchanged(db_engine, monkeypatch, delta):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    resolutions: list[str] = []

    class _Resolution:
        def as_dict(self):
            return {"state": "matched"}

    async def fake_resolve(_db, facts):
        resolutions.append(facts.model)
        return _Resolution()

    monkeypatch.setattr(discovery_service, "resolve_profile", fake_resolve)

    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"facts": {"model": "NEW"}})
    # A human reconciliation state that a discarded observation must never disturb.
    async with factory() as session:
        await session.execute(text("UPDATE discovered_device SET status = 'ignored'"))
        await session.commit()
    before = await device_row(factory, integration_id)
    counts_before = await counts(factory)

    device, is_new = await commit_ingest(
        factory, integration_id, observed_at=T0 + delta, attrs={"facts": {"model": "STALE"}, "extra": 1})

    assert is_new is False
    assert device.raw_attributes == before["raw_attributes"]
    assert await device_row(factory, integration_id) == before  # every column, byte for byte
    assert await counts(factory) == counts_before  # no events, no diff
    assert resolutions == ["NEW"]  # profile-match refresh ran only for the accepted observation


async def test_equal_timestamp_is_first_writer_wins_and_does_not_inflate_freshness(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"v": "first"})
    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"v": "second"})
    row = await device_row(factory, integration_id)
    assert row["raw_attributes"] == {"v": "first"} and row["last_seen_at"] == T0


# 5. concurrent reverse-order deliveries ------------------------------------------------------------------------


@pytest.mark.parametrize("holder_is_newer", [True, False], ids=["newer-holds-lock", "older-holds-lock"])
async def test_concurrent_out_of_order_updates_converge_on_the_newest_observation(db_engine, holder_is_newer):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"v": "base"})
    older, newer = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    holder_time, waiter_time = (newer, older) if holder_is_newer else (older, newer)

    holder_has_lock, release_holder = asyncio.Event(), asyncio.Event()

    async def holder():
        async with factory() as session:
            await ingest(session, integration_id, observed_at=holder_time, attrs={"v": "holder"})
            holder_has_lock.set()  # row is FOR UPDATE locked, change uncommitted
            await release_holder.wait()
            await session.commit()

    async def waiter():
        async with factory() as session:
            await ingest(session, integration_id, observed_at=waiter_time, attrs={"v": "waiter"})
            await session.commit()

    holder_task = asyncio.create_task(holder())
    await holder_has_lock.wait()
    waiter_task = asyncio.create_task(waiter())
    done, _pending = await asyncio.wait({waiter_task}, timeout=BLOCKED_PROBE_SECONDS)
    assert not done, "the second delivery must block on the row lock until the first commits"
    release_holder.set()
    await asyncio.gather(holder_task, waiter_task)

    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] == newer  # always the maximum, whatever the arrival/commit order
    assert row["raw_attributes"] == {"v": "holder" if holder_is_newer else "waiter"}
    assert (await counts(factory))["devices"] == 1


# 6 and 7. concurrent first-insert race --------------------------------------------------------------------------


@pytest.mark.parametrize("creator_is_newer", [True, False], ids=["creator-newer", "creator-older"])
async def test_concurrent_first_insert_converges_to_one_device_and_one_set_of_events(db_engine, creator_is_newer):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    older, newer = T0, T0 + timedelta(minutes=5)
    creator_time, loser_time = (newer, older) if creator_is_newer else (older, newer)

    creator_inserted, release_creator = asyncio.Event(), asyncio.Event()
    outcomes: dict[str, bool] = {}

    async def creator():
        async with factory() as session:
            _device, is_new = await ingest(session, integration_id, observed_at=creator_time, attrs={"v": "creator"})
            outcomes["creator"] = is_new
            creator_inserted.set()  # row inserted in this tx, not yet visible to anyone else
            await release_creator.wait()
            await session.commit()

    async def loser():
        async with factory() as session:
            _device, is_new = await ingest(session, integration_id, observed_at=loser_time, attrs={"v": "loser"})
            outcomes["loser"] = is_new
            await session.commit()

    creator_task = asyncio.create_task(creator())
    await creator_inserted.wait()
    loser_task = asyncio.create_task(loser())  # sees no row, attempts its own INSERT, blocks on the unique index
    done, _pending = await asyncio.wait({loser_task}, timeout=BLOCKED_PROBE_SECONDS)
    assert not done, "the losing insert must wait for the winner's commit, not fail or duplicate"
    release_creator.set()
    await asyncio.gather(creator_task, loser_task)  # no INTERNAL_PROCESSING_ERROR-style exception

    assert outcomes == {"creator": True, "loser": False}
    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] == newer
    assert row["raw_attributes"] == {"v": "creator" if creator_is_newer else "loser"}
    # Only the winning creator emitted discovery/reconciliation events.
    assert await counts(factory) == {"devices": 1, "diffs": 1, "discovered_events": 1, "required_events": 1}


# 8 to 10. observation time normalization -----------------------------------------------------------------------


async def test_missing_observed_at_uses_current_utc_time(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    before = datetime.now(UTC)
    await commit_ingest(factory, integration_id, observed_at=None)
    after = datetime.now(UTC)
    row = await device_row(factory, integration_id)
    assert before <= row["last_seen_at"] <= after


async def test_excessive_future_skew_is_clamped_and_cannot_pin_later_observations(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    before = datetime.now(UTC)
    await commit_ingest(factory, integration_id, observed_at=before + timedelta(days=365), attrs={"v": "far-future-1"})
    first = (await device_row(factory, integration_id))["last_seen_at"]
    assert before + MAX_FUTURE_SKEW <= first <= datetime.now(UTC) + MAX_FUTURE_SKEW

    # A second excessively-future observation is clamped to a *later* bound and is still accepted:
    # a far-future clock cannot freeze the row at its first value.
    await commit_ingest(factory, integration_id, observed_at=before + timedelta(days=400), attrs={"v": "far-future-2"})
    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] > first and row["raw_attributes"] == {"v": "far-future-2"}


async def test_very_old_historical_observation_is_preserved(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    ancient = datetime(2001, 1, 1, tzinfo=UTC)
    await commit_ingest(factory, integration_id, observed_at=ancient)
    assert (await device_row(factory, integration_id))["last_seen_at"] == ancient


async def test_naive_observation_time_is_treated_as_utc(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=datetime(2026, 1, 1, 12, 0, 0), attrs={"v": "naive"})
    assert (await device_row(factory, integration_id))["last_seen_at"] == T0
    # Comparable with aware values: a naive-equal replay is a no-op, a later aware value wins.
    await commit_ingest(factory, integration_id, observed_at=datetime(2026, 1, 1, 12, 0, 0), attrs={"v": "replay"})
    assert (await device_row(factory, integration_id))["raw_attributes"] == {"v": "naive"}
    await commit_ingest(factory, integration_id, observed_at=T0 + timedelta(seconds=1), attrs={"v": "later"})
    assert (await device_row(factory, integration_id))["raw_attributes"] == {"v": "later"}


# 12. non-duplicate integrity errors are not recovered ------------------------------------------------------------


async def test_non_duplicate_integrity_error_is_reraised_and_leaves_no_state(db_engine):
    factory = factory_for(db_engine)
    missing_integration = uuid.uuid4()  # FK violation on integration_id, not a duplicate key
    async with factory() as session:
        with pytest.raises(IntegrityError) as caught:
            await ingest(session, missing_integration, observed_at=T0)
        assert getattr(caught.value.orig, "sqlstate", None) == "23503"  # foreign_key_violation
        # The savepoint rolled back cleanly: the session is still usable.
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        await session.rollback()
    assert await counts(factory) == {"devices": 0, "diffs": 0, "discovered_events": 0, "required_events": 0}


# 14. legacy receipt-time transition ---------------------------------------------------------------------------------


async def test_legacy_receipt_time_last_seen_at_transition_drops_one_gap_observation_then_recovers(db_engine):
    """Rows written before DISC-ORDER-01 hold receipt time in `last_seen_at`, which can be
    later than the true observation. A genuinely newer record that falls inside that gap
    is dropped once (documented, accepted); the next observation supersedes it."""
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    true_observation, legacy_receipt = T0, T0 + timedelta(minutes=10)
    await commit_ingest(factory, integration_id, observed_at=true_observation, attrs={"v": "legacy"})
    async with factory() as session:  # simulate the legacy value
        await session.execute(text("UPDATE discovered_device SET last_seen_at = :t"), {"t": legacy_receipt})
        await session.commit()

    await commit_ingest(factory, integration_id, observed_at=T0 + timedelta(minutes=5), attrs={"v": "gap"})
    assert (await device_row(factory, integration_id))["raw_attributes"] == {"v": "legacy"}  # dropped once

    await commit_ingest(factory, integration_id, observed_at=T0 + timedelta(minutes=11), attrs={"v": "next"})
    row = await device_row(factory, integration_id)
    assert row["raw_attributes"] == {"v": "next"} and row["last_seen_at"] == T0 + timedelta(minutes=11)


# --- PR #369 review remediation: C1 replay stability, C2 legacy transition, C3 classifier ---------------------------

CLOCK_BASE = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)


def set_clock(monkeypatch, moment: datetime) -> None:
    """Pins the application clock `ingest_discovery` reads (`datetime.now(UTC)`)."""
    import datetime as _dt

    class _Fixed(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(discovery_service, "datetime", _Fixed)


def collector_attrs(occurred_at: datetime, **extra) -> dict:
    """What the collector ingest path stores: the record's own occurred_at, unclamped."""
    return {"occurred_at": occurred_at.isoformat(), **extra}


async def test_c1_replayed_far_future_observation_cannot_advance_ordering_with_wall_clock(db_engine, monkeypatch):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    far = CLOCK_BASE + timedelta(days=30)

    set_clock(monkeypatch, CLOCK_BASE)
    await commit_ingest(factory, integration_id, observed_at=far, attrs=collector_attrs(far, v="A"))
    pinned = (await device_row(factory, integration_id))["last_seen_at"]
    assert pinned == CLOCK_BASE + MAX_FUTURE_SKEW

    for minutes in (3, 4, 20):  # exact replays under later delivery clocks (distinct dedup keys upstream)
        set_clock(monkeypatch, CLOCK_BASE + timedelta(minutes=minutes))
        await commit_ingest(factory, integration_id, observed_at=far, attrs=collector_attrs(far, v=f"replay-{minutes}"))
        row = await device_row(factory, integration_id)
        assert row["last_seen_at"] == pinned and row["raw_attributes"]["v"] == "A"

    # A legitimate observation after the original 5-minute bound is accepted; replays did not extend the pin.
    set_clock(monkeypatch, CLOCK_BASE + timedelta(minutes=6))
    legit = CLOCK_BASE + timedelta(minutes=5, seconds=30)
    await commit_ingest(factory, integration_id, observed_at=legit, attrs=collector_attrs(legit, v="legit"))
    row = await device_row(factory, integration_id)
    assert row["last_seen_at"] == legit and row["raw_attributes"]["v"] == "legit"


async def test_c1_older_far_future_record_does_not_overwrite_a_newer_one_and_a_newer_one_wins(db_engine, monkeypatch):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    newer_far, older_far = CLOCK_BASE + timedelta(days=60), CLOCK_BASE + timedelta(days=30)

    set_clock(monkeypatch, CLOCK_BASE)
    await commit_ingest(factory, integration_id, observed_at=newer_far, attrs=collector_attrs(newer_far, v="newer"))
    set_clock(monkeypatch, CLOCK_BASE + timedelta(minutes=2))
    await commit_ingest(factory, integration_id, observed_at=older_far, attrs=collector_attrs(older_far, v="older"))
    assert (await device_row(factory, integration_id))["raw_attributes"]["v"] == "newer"

    newest_far = CLOCK_BASE + timedelta(days=90)
    await commit_ingest(factory, integration_id, observed_at=newest_far, attrs=collector_attrs(newest_far, v="newest"))
    row = await device_row(factory, integration_id)
    assert row["raw_attributes"]["v"] == "newest"
    assert row["last_seen_at"] == CLOCK_BASE + timedelta(minutes=2) + MAX_FUTURE_SKEW


async def test_c2_legacy_collector_row_accepts_multiple_buffered_observations_without_follow_up(db_engine):
    """Legacy row: last_seen_at = Central receipt time R (later than the true observation O0, which
    the collector path persisted in raw_attributes.occurred_at). Buffered observations that fall
    between O0 and R are delivered after R, out of order, and nothing is polled afterwards."""
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    o0, receipt = T0, T0 + timedelta(minutes=10)
    await commit_ingest(factory, integration_id, observed_at=o0, attrs=collector_attrs(o0, v="O0"))
    async with factory() as session:  # simulate a pre-DISC-ORDER-01 row: receipt time in last_seen_at
        await session.execute(text("UPDATE discovered_device SET last_seen_at = :t"), {"t": receipt})
        await session.commit()

    o1, o2, o3 = (T0 + timedelta(minutes=m) for m in (3, 6, 9))
    for observed, tag in ((o2, "O2"), (o1, "O1"), (o3, "O3")):  # arrival order O2, O1, O3
        await commit_ingest(factory, integration_id, observed_at=observed, attrs=collector_attrs(observed, v=tag))
        if tag == "O1":
            assert (await device_row(factory, integration_id))["raw_attributes"]["v"] == "O2"  # older than O2: no-op
    row = await device_row(factory, integration_id)
    assert row["raw_attributes"]["v"] == "O3" and row["last_seen_at"] == o3  # newest buffered state, no follow-up poll

    # A replay of the original legacy observation stays a no-op.
    await commit_ingest(factory, integration_id, observed_at=o0, attrs=collector_attrs(o0, v="O0-replay"))
    assert (await device_row(factory, integration_id))["raw_attributes"]["v"] == "O3"


async def test_c2_legacy_row_without_persisted_observation_time_keeps_the_documented_limitation(db_engine):
    """Residual, accepted limitation (e.g. a legacy centrally-polled row: receipt ~ observation):
    without a persisted occurred_at the row's receipt-time last_seen_at is the only order key."""
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=T0, attrs={"protocol": "icmp"})
    async with factory() as session:
        await session.execute(text("UPDATE discovered_device SET last_seen_at = :t"), {"t": T0 + timedelta(minutes=10)})
        await session.commit()
    await commit_ingest(factory, integration_id, observed_at=T0 + timedelta(minutes=5), attrs={"protocol": "icmp", "v": "gap"})
    assert "v" not in (await device_row(factory, integration_id))["raw_attributes"]  # dropped once
    await commit_ingest(factory, integration_id, observed_at=T0 + timedelta(minutes=11), attrs={"protocol": "icmp", "v": "next"})
    assert (await device_row(factory, integration_id))["raw_attributes"]["v"] == "next"  # superseded by the next poll


def _unique_violation(constraint_name):
    class _Orig(Exception):
        sqlstate = "23505"

    orig = _Orig()
    orig.constraint_name = constraint_name
    return IntegrityError("INSERT ...", {}, orig)


def test_c3_classifier_accepts_only_the_device_key_constraint():
    assert discovery_service._is_device_key_conflict(_unique_violation(discovery_service._DEVICE_UNIQUE_CONSTRAINT))
    assert not discovery_service._is_device_key_conflict(_unique_violation("discovered_device_pkey"))
    assert not discovery_service._is_device_key_conflict(_unique_violation("uq_some_other_table_col"))
    assert not discovery_service._is_device_key_conflict(_unique_violation(None))  # unknown constraint: not a match

    class _FK(Exception):
        sqlstate = "23503"

    assert not discovery_service._is_device_key_conflict(IntegrityError("INSERT ...", {}, _FK()))
    assert not discovery_service._is_device_key_conflict(IntegrityError("INSERT ...", {}, Exception("no sqlstate")))


async def test_c3_real_postgres_duplicate_key_error_carries_the_constraint_name_the_classifier_expects(db_engine):
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=T0)
    async with factory() as session:
        duplicate = discovery_service.DiscoveredDevice(
            id=uuid.uuid4(), integration_id=integration_id, external_identifier=EXTERNAL_ID,
            discovered_at=T0, last_seen_at=T0, raw_attributes={}, status="new")
        with pytest.raises(IntegrityError) as caught:
            async with session.begin_nested():
                session.add(duplicate)
                await session.flush()
    assert discovery_service._is_device_key_conflict(caught.value)


async def test_c3_unrelated_unique_violation_is_reraised_not_recovered(db_engine, monkeypatch):
    """A primary-key collision on `discovered_device` is a unique violation (23505) on a different
    constraint; it must propagate, not be treated as the device-key race."""
    integration_id = await seed_integration(db_engine)
    factory = factory_for(db_engine)
    await commit_ingest(factory, integration_id, observed_at=T0, external_identifier="existing")
    existing_id = (await device_row(factory, integration_id, "existing"))["id"]
    monkeypatch.setattr(discovery_service.uuid, "uuid4", lambda: existing_id)  # forces a PK collision

    before = await counts(factory)
    async with factory() as session:
        with pytest.raises(IntegrityError) as caught:
            await ingest(session, integration_id, observed_at=T0, external_identifier="brand-new")
        assert not discovery_service._is_device_key_conflict(caught.value)
        assert getattr(caught.value.orig, "sqlstate", None) == "23505"
        await session.rollback()
    assert await counts(factory) == before
    assert await device_row(factory, integration_id, "brand-new") is None
