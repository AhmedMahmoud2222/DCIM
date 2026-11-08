"""G2: real HTTP + PostgreSQL, with an explicit clock and real compaction."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.telemetry_retention import compact_eligible_raw, merge_late_reading
from app.domain.auth.models import RoleAssignment
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    IntegrationMetricMapping,
    MonitoringPolicy,
    TelemetryReading,
    telemetry_series_key,
)

NOW = datetime(2030, 7, 8, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def series(db_session):
    previous = await db_session.get(MonitoringPolicy, 1)
    old_days = previous.raw_retention_days if previous is not None else None
    await db_session.execute(delete(MonitoringPolicy))
    collector = Collector(id=uuid.uuid4(), name="G2", collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=NOW)
    integration = Integration(id=uuid.uuid4(), name="G2", integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=300)
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="temp",
                                       canonical_metric="temperature_c", unit="degC", scale=1)
    db_session.add_all([collector, integration, mapping])
    await db_session.flush()

    async def add(at, value=20, *, external="sensor", unit="degC", version="1"):
        row = TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id,
            external_identifier=external, series_key=telemetry_series_key(integration.id, None, external, "temperature_c", unit, version),
            dedup_key=uuid.uuid4().hex, metric="temperature_c", unit=unit, value=value,
            registry_version=version, occurred_at=at, received_at=NOW, attributes={},
        )
        db_session.add(row)
        await db_session.flush()
        # ORM defaults a missing version; explicitly retain legacy NULL for identity tests.
        if version is None:
            row.registry_version = None
            await db_session.flush()
        return row

    yield integration, add
    await db_session.rollback()
    await db_session.execute(delete(MonitoringPolicy))
    if old_days is not None:
        db_session.add(MonitoringPolicy(id=1, raw_retention_days=old_days))
    await db_session.commit()


async def policy(db, days):
    row = await db.get(MonitoringPolicy, 1)
    if row is None:
        db.add(MonitoringPolicy(id=1, raw_retention_days=days))
    else:
        row.raw_retention_days = days
    await db.commit()


async def history(client, headers, integration, **overrides):
    params = dict(metric="temperature_c", integration_id=str(integration.id),
                  start=(NOW - timedelta(days=1000)).isoformat(), end=NOW.isoformat())
    params.update(overrides)
    response = await client.get("/api/v1/telemetry/history", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def samples(points):
    return sum(p["sample_count"] if p["resolution"] == "daily" else 1 for p in points)


@pytest.mark.parametrize("days", [90, 365, 730, None])
async def test_policy_compaction_and_history(client, auth_headers, db_session, series, days):
    integration, add = series
    headers = await auth_headers()
    if days is not None:
        await policy(db_session, days)
    effective = days or 365
    for age in (800, 500, 200, 50):
        await add(NOW - timedelta(days=age), value=age)
    await db_session.commit()
    before = await history(client, headers, integration)
    assert samples(before) == 4 and all(p["resolution"] == "raw" for p in before)
    await compact_eligible_raw(db_session, now=NOW)
    await db_session.commit()
    after = await history(client, headers, integration)
    assert samples(after) == 4
    assert [p["resolution"] for p in after] == ["daily" if age > effective else "raw" for age in (800, 500, 200, 50)]
    assert [p["value"] for p in after] == [800, 500, 200, 50]
    assert all(p["presentation_unit"] == "degC" and p["registry_version"] == "1" for p in after)


@pytest.mark.parametrize("initial,changed", [(365, 90), (90, 730)])
async def test_policy_transition_does_not_hide_either_representation(client, auth_headers, db_session, series, initial, changed):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, initial)
    for age in (500, 200, 50):
        await add(NOW - timedelta(days=age))
    await compact_eligible_raw(db_session, now=NOW)
    await db_session.commit()
    assert samples(await history(client, headers, integration)) == 3
    await policy(db_session, changed)
    assert samples(await history(client, headers, integration)) == 3
    await compact_eligible_raw(db_session, now=NOW)
    await db_session.commit()
    assert samples(await history(client, headers, integration)) == 3


@pytest.mark.parametrize("days", [90, 365, 730])
async def test_utc_midnight_independent_of_session_and_input_timezone(client, auth_headers, db_session, series, days):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, days)
    midnight = (NOW - timedelta(days=days)).replace(hour=0)
    for at in (midnight - timedelta(microseconds=1), midnight, midnight + timedelta(microseconds=1)):
        await add(at.astimezone(timezone(timedelta(hours=4))))
    # DATE(timestamptz) must not silently use the database session's day.
    await db_session.execute(text("SET TIME ZONE 'Pacific/Honolulu'"))
    await compact_eligible_raw(db_session, now=NOW.astimezone(timezone(timedelta(hours=-10))))
    await db_session.commit()
    points = await history(client, headers, integration,
                           start=(midnight - timedelta(days=1)).astimezone(timezone(timedelta(hours=4))).isoformat(),
                           end=(midnight + timedelta(microseconds=1)).isoformat())
    assert samples(points) == 3
    assert [p["resolution"] for p in points] == ["daily", "raw", "raw"]
    assert datetime.fromisoformat(points[0]["occurred_at"]) == midnight - timedelta(days=1)
    assert datetime.fromisoformat(points[1]["occurred_at"]) == midnight
    await db_session.execute(text("RESET TIME ZONE"))


async def test_partial_day_skip_locked_overlap_and_late_readings(client, auth_headers, db_session, db_engine, series):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, 90)
    old = NOW - timedelta(days=200)
    a = await add(old, 10)
    await add(old + timedelta(hours=1), 30)
    await db_session.commit()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as locker:
        await locker.execute(select(TelemetryReading).where(TelemetryReading.id == a.id).with_for_update())
        assert await compact_eligible_raw(db_session, now=NOW) == 1
        await db_session.commit()
        points = await history(client, headers, integration)
        assert samples(points) == 2
        assert [p["resolution"] for p in points] == ["daily", "raw"]
        assert [p["value"] for p in points] == [30, 10]
        await locker.rollback()
    await compact_eligible_raw(db_session, now=NOW)
    await db_session.commit()
    points = await history(client, headers, integration)
    assert len(points) == 1 and points[0]["sample_count"] == 2 and points[0]["value"] == 20
    # Exercise the real late-merge service with a controlled clock and the same
    # atomic delete used by ingest_reading, without altering the ingestion clock.
    late = await add(old + timedelta(hours=2), 50)
    assert await merge_late_reading(db_session, late, now=NOW)
    await db_session.delete(late)
    await db_session.commit()
    assert samples(await history(client, headers, integration)) == 3
    # Increasing retention legitimately leaves new late arrivals raw beside daily.
    await policy(db_session, 730)
    late = await add(old + timedelta(hours=3), 70)
    assert not await merge_late_reading(db_session, late, now=NOW)
    await db_session.commit()
    assert samples(await history(client, headers, integration)) == 4
    await policy(db_session, 90)
    await compact_eligible_raw(db_session, now=NOW)
    await compact_eligible_raw(db_session, now=NOW)  # retry consumes nothing twice
    await db_session.commit()
    points = await history(client, headers, integration)
    assert len(points) == 1 and points[0]["sample_count"] == 4 and points[0]["value"] == 40
    assert points[0]["minimum_value"] == 10 and points[0]["maximum_value"] == 70


async def test_order_limit_identity_filters_and_one_history_snapshot(client, auth_headers, db_session, db_engine, series):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, 90)
    old = NOW - timedelta(days=200)
    await add(old, 25)
    await add(old, 77, unit="degF", version=None)
    await add(old, 30, external="other")
    await compact_eligible_raw(db_session, now=NOW)
    for i in range(5):
        await add(NOW - timedelta(hours=i), i)
    await db_session.commit()
    queries = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "telemetry_reading" in statement or "daily_telemetry_aggregate" in statement:
            queries.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", observe)
    try:
        points = await history(client, headers, integration)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", observe)
    assert len(queries) == 1 and "UNION ALL" in queries[0]
    assert samples(points) == 8
    assert len({p["id"] for p in points}) == 8
    assert {p["unit"] for p in points[:3]} == {"degC", "degF"}
    legacy = next(p for p in points if p["unit"] == "degF")
    assert legacy["registry_version"] is None and legacy["presentation_value"] == 77
    assert [p["occurred_at"] for p in points] == sorted(p["occurred_at"] for p in points)
    assert await history(client, headers, integration, limit=4) == points[:4]
    assert await history(client, headers, integration, integration_id=str(uuid.uuid4())) == []
    assert await history(client, headers, integration, managed_asset_id=str(uuid.uuid4())) == []
    response = await client.get("/api/v1/telemetry/history", headers=headers, params={
        "metric": "temperature_c", "integration_id": str(integration.id),
        "start": old.isoformat(), "end": NOW.isoformat(), "limit": 1001,
    })
    assert response.status_code == 422


async def test_existing_authentication_and_restricted_scope_fail_closed(client, auth_headers, make_user, db_session, series):
    integration, add = series
    admin = await auth_headers()
    await add(NOW - timedelta(days=800))
    await compact_eligible_raw(db_session, now=NOW)
    await add(NOW)
    await db_session.commit()
    assert samples(await history(client, admin, integration)) == 2
    user = await make_user("g2-scoped@example.com", "correct horse battery staple", "Administrator")
    assignments = (await db_session.execute(select(RoleAssignment).where(RoleAssignment.user_id == user.id))).scalars().all()
    for assignment in assignments:
        assignment.scope_type = "site"
        assignment.scope_id = uuid.uuid4()
    await db_session.commit()
    login = await client.post("/api/v1/auth/login", json={"email": user.email, "password": "correct horse battery staple"})
    assert login.status_code == 200
    restricted = {"Authorization": "Bearer " + login.json()["access_token"]}
    params = dict(metric="temperature_c", start=(NOW - timedelta(days=1000)).isoformat(), end=NOW.isoformat())
    for target in (integration.id, uuid.uuid4()):
        params["integration_id"] = str(target)
        # Telemetry is not scope-aware in the existing model; site-restricted
        # grants are inactive. Keep that fail-closed boundary unchanged.
        assert (await client.get("/api/v1/telemetry/history", headers=restricted, params=params)).status_code == 403
        client.cookies.clear()
        assert (await client.get("/api/v1/telemetry/history", params=params)).status_code == 401


async def test_history_during_compaction_commit_uses_one_snapshot(client, auth_headers, db_session, db_engine, series, monkeypatch):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, 90)
    old = NOW - timedelta(days=200)
    await add(old, 10)
    await add(old + timedelta(hours=1), 30)
    await db_session.commit()
    original_execute = db_session.execute
    compacted = False

    async def execute_then_compact(statement, *args, **kwargs):
        nonlocal compacted
        result = await original_execute(statement, *args, **kwargs)
        # Commit after history's first evidence SELECT, before it can issue a
        # second. Two independently snapshotted queries would duplicate evidence.
        if not compacted and "telemetry_reading" in str(statement):
            compacted = True
            factory = async_sessionmaker(db_engine, expire_on_commit=False)
            async with factory() as worker:
                assert await compact_eligible_raw(worker, now=NOW) == 1
                await worker.commit()
        return result

    monkeypatch.setattr(db_session, "execute", execute_then_compact)
    points = await history(client, headers, integration)
    assert compacted and samples(points) == 2
    assert [p["resolution"] for p in points] == ["raw", "raw"]
    # A subsequent HTTP request sees the committed daily representation, once.
    points = await history(client, headers, integration)
    assert len(points) == 1 and points[0]["sample_count"] == 2 and points[0]["value"] == 20


async def test_daily_bucket_range_and_raw_endpoints_are_compatible(client, auth_headers, db_session, series):
    integration, add = series
    headers = await auth_headers()
    await policy(db_session, 90)
    old = (NOW - timedelta(days=200)).replace(hour=0)
    await add(old + timedelta(hours=1), 10)
    await add(old + timedelta(hours=23), 30)
    await compact_eligible_raw(db_session, now=NOW)
    await db_session.commit()
    points = await history(client, headers, integration, start=(old + timedelta(hours=12)).isoformat(),
                           end=(old + timedelta(hours=13)).isoformat())
    assert len(points) == 1 and points[0]["sample_count"] == 2
    assert datetime.fromisoformat(points[0]["occurred_at"]) == old
    for delta in (-1, 0, 1, 2):
        await add(NOW + timedelta(seconds=delta), delta)
    await db_session.commit()
    points = await history(client, headers, integration, start=NOW.isoformat(), end=(NOW + timedelta(seconds=1)).isoformat())
    assert [p["value"] for p in points] == [0, 1]
