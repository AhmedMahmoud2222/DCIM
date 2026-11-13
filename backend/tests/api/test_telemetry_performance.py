"""Exercise the equipment performance view through HTTP against populated PostgreSQL."""

import asyncio
import tracemalloc
import uuid
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.telemetry_retention import compact_eligible_raw
from app.db.session import get_db
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, telemetry_series_key
from app.main import app
from tests.api._phase2_helpers import create_equipment
from tests.api.test_user_groups_authz import _all, _group, _site, _user

START = datetime(2024, 1, 1, tzinfo=UTC)
PATH = "/api/v1/telemetry/performance"


@pytest.fixture
async def source(db_session):
    collector = Collector(
        id=uuid.uuid4(),
        name="Performance",
        collector_type="central",
        status="active",
        secret_ciphertext="test",
        secret_rotated_at=START,
    )
    integration = Integration(
        id=uuid.uuid4(),
        name="Performance",
        integration_type="snmp",
        target_host="192.0.2.1",
        config={},
        poll_interval_seconds=300,
    )
    mapping = IntegrationMetricMapping(
        id=uuid.uuid4(),
        integration_id=integration.id,
        source_identifier="temp",
        canonical_metric="temperature_c",
        unit="degC",
        scale=1,
    )
    db_session.add_all([collector, integration, mapping])
    await db_session.flush()

    async def seed(asset_id, at=START, count=1, seconds=300, external="sensor"):
        key = telemetry_series_key(integration.id, uuid.UUID(asset_id), external, "temperature_c", "degC", "1")
        await db_session.execute(
            text("""
            INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, managed_asset_id,
                external_identifier, series_key, dedup_key, metric, unit, value, registry_version,
                occurred_at, received_at, attributes)
            SELECT :collector, :integration, :mapping, :asset, :external, :key, gen_random_uuid()::text,
                'temperature_c', 'degC', i, '1', CAST(:at AS timestamptz) + i * CAST(:seconds AS integer)
                * interval '1 second', CAST(:at AS timestamptz), '{}'::jsonb
            FROM generate_series(0, CAST(:count AS integer) - 1) AS i
        """),
            dict(
                collector=collector.id,
                integration=integration.id,
                mapping=mapping.id,
                asset=uuid.UUID(asset_id),
                external=external,
                key=key,
                at=at,
                seconds=seconds,
                count=count,
            ),
        )
        await db_session.commit()
        return key

    return seed


def window(asset_id, start=START, days=90):
    return dict(
        managed_asset_id=asset_id, metric="temperature_c", start=start.isoformat(), end=(start + timedelta(days=days)).isoformat()
    )


@pytest.mark.parametrize("days", [90, 366])
async def test_full_window_bounded_chart(client, auth_headers, db_engine, source, days, record_property):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    count = days * 24  # hourly readings through the window, plus an excluded reading at its exclusive end
    key = await source(asset, count=count + 1, seconds=3600)
    evidence_queries = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "telemetry_reading" in statement or "daily_telemetry_aggregate" in statement:
            evidence_queries.append(statement)

    # The repository's normal HTTP fixture shares one session. Production requests
    # each own a session, including when the two chart requests overlap.
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    async def request_db():
        async with sessions() as session:
            yield session

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = request_db
    event.listen(db_engine.sync_engine, "before_cursor_execute", observe)
    tracemalloc.start()
    try:
        before = perf_counter()
        results = await asyncio.gather(*(client.get(PATH, headers=admin, params=window(asset, days=days)) for _ in range(2)))
        response, concurrent = results
        _, peak = tracemalloc.get_traced_memory()
        elapsed = perf_counter() - before
    finally:
        tracemalloc.stop()
        app.dependency_overrides[get_db] = previous
        event.remove(db_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    assert concurrent.content == response.content
    [series] = response.json()["series"]
    assert series["series_key"] == key
    assert series["sample_count"] == count
    assert float(series["minimum"]) == 0 and float(series["maximum"]) == count - 1
    assert float(series["average"]) == float(series["p50"]) == (count - 1) / 2
    assert float(series["p95"]) == pytest.approx((count - 1) * 0.95)
    assert sum(p["sample_count"] for p in series["points"]) == count
    assert len(series["points"]) <= 300
    assert len(evidence_queries) == 2
    assert elapsed < 15
    assert peak < 64 * 1024 * 1024
    assert len(response.content) < 256 * 1024
    record_property("concurrency", 2)
    record_property("peak_python_bytes", peak)
    record_property("window_days", days)
    record_property("response_bytes", len(response.content))
    record_property("elapsed_seconds", elapsed)
    record_property("evidence_queries", len(evidence_queries))


async def test_compacted_and_raw_days(client, auth_headers, db_session, source):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    await source(asset, count=3)
    await compact_eligible_raw(db_session, now=START + timedelta(days=730))
    await db_session.commit()
    await source(asset, at=START + timedelta(days=1), count=1)
    response = await client.get(PATH, headers=admin, params=window(asset, days=2))
    assert response.status_code == 200, response.text
    [series] = response.json()["series"]
    assert series["sample_count"] == 4 and float(series["average"]) == 0.75
    assert float(series["minimum"]) == 0 and float(series["maximum"]) == 2
    assert series["p50"] is None and series["p95"] is None
    partial = await client.get(PATH, headers=admin, params=window(asset, START + timedelta(hours=12), days=1))
    assert partial.status_code == 200, partial.text
    [series] = partial.json()["series"]
    assert series["partial_days"] and series["sample_count"] == 1


async def test_site_scope_and_direct_equipment_access(client, auth_headers, source):
    admin = await auth_headers()
    sites = [await _site(client, admin), await _site(client, admin)]
    assets = []
    for site in sites:
        asset = (await create_equipment(client, admin, auth_headers))["id"]
        moved = await client.post(
            f"/api/v1/equipment/{asset}/move", headers=admin, json={"placement_type": "floor_standing", "room_id": site["room"]}
        )
        assert moved.status_code == 200, moved.text
        await source(asset)
        assets.append(asset)
    for site, asset in zip(sites, assets, strict=True):
        page = await client.get("/api/v1/equipment", headers=admin, params={"site_id": site["site"]})
        assert page.status_code == 200 and page.json()["total"] == 1
        assert [item["id"] for item in page.json()["items"]] == [asset]
    group = await _group(client, admin, allow=["telemetry:read"], sites=[_all(sites[0]["site"])])
    _, scoped = await _user(client, admin, [group])
    _, unprivileged = await _user(client, admin, [])
    for path in [PATH, "/api/v1/telemetry/latest", "/api/v1/telemetry/history"]:
        params = window(assets[0])
        client.cookies.clear()
        assert (await client.get(path, params=params)).status_code == 401
        assert (await client.get(path, headers=unprivileged, params=params)).status_code == 403
        visible = await client.get(path, params=params, headers=admin)
        assert visible.status_code == 200, visible.text
        assert visible.json()["series"] if path == PATH else visible.json()
        # Restricted telemetry grants are inactive for this module. Both visible
        # and foreign direct IDs must require a globally active telemetry grant.
        for asset in assets:
            params["managed_asset_id"] = asset
            assert (await client.get(path, params=params, headers=scoped)).status_code == 403
    for path in [PATH, "/api/v1/telemetry/latest"]:
        params = {**window(assets[0]), "site_id": sites[0]["site"]}
        visible = await client.get(path, headers=admin, params=params)
        assert visible.status_code == 200
        assert visible.json()["series"] if path == PATH else visible.json()
        params["site_id"] = sites[1]["site"]
        mismatch = await client.get(path, headers=admin, params=params)
        assert mismatch.status_code == 200
        assert (mismatch.json()["series"] if path == PATH else mismatch.json()) == []


async def test_validation_empty_and_independent_series(client, auth_headers, source):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    for days in (0, -1, 367):
        assert (await client.get(PATH, headers=admin, params=window(asset, days=days))).status_code == 422
    empty = await client.get(PATH, headers=admin, params=window(asset))
    assert empty.status_code == 200 and empty.json()["series"] == []
    first = await source(asset, count=2, seconds=89 * 86400)
    second = await source(asset, external="sensor-b")
    response = await client.get(PATH, headers=admin, params=window(asset))
    assert response.status_code == 200
    by_key = {s["series_key"]: s for s in response.json()["series"]}
    assert set(by_key) == {first, second}
    points = by_key[first]["points"]
    assert len(points) == 2 and points[0]["end"] < points[1]["start"]
    assert by_key[second]["sample_count"] == 1 and by_key[second]["average"] == 0
    for index in range(31):
        await source(asset, external=f"extra-{index}")
    overflow = await client.get(PATH, headers=admin, params=window(asset))
    assert overflow.status_code == 422 and "32" in overflow.json()["detail"]


async def test_live_latest_per_series_keeps_slow_sensor(client, auth_headers, source):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    slow = await source(asset, external="slow")
    fast = await source(asset, at=START + timedelta(minutes=1), count=101, seconds=1, external="fast")
    params = {"managed_asset_id": asset, "metric": "temperature_c", "limit": 3}
    path = "/api/v1/telemetry/latest"
    legacy = await client.get(path, headers=admin, params=params)
    assert legacy.status_code == 200
    assert [row["series_key"] for row in legacy.json()] == [fast] * 3
    params["per_series"] = "true"
    latest = await client.get(path, headers=admin, params=params)
    assert latest.status_code == 200, latest.text
    rows = latest.json()
    assert [(r["series_key"], r["value"]) for r in rows] == [(fast, 100), (slow, 0)]
    assert all(r["expected_poll_interval_seconds"] == 300 for r in rows)
    params["site_id"] = str(uuid.uuid4())
    assert (await client.get(path, headers=admin, params=params)).json() == []
    _, denied = await _user(client, admin, [])
    assert (await client.get(path, headers=denied, params=params)).status_code == 403


async def test_multiday_buckets_conserve_raw_and_daily_evidence(client, auth_headers, db_session, source):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    await source(asset, count=4, seconds=86400)
    await compact_eligible_raw(db_session, now=START + timedelta(days=730))
    await db_session.commit()
    # The first two-day bucket contains two daily samples and two late raw samples.
    await source(asset, at=START + timedelta(hours=12), count=2)
    response = await client.get(PATH, headers=admin, params=window(asset, days=366))
    assert response.status_code == 200, response.text
    [series] = response.json()["series"]
    first, second = series["points"]
    assert [p["resolution"] for p in (first, second)] == ["mixed", "daily"]
    assert [p["sample_count"] for p in (first, second)] == [4, 2]
    assert [p["average"] for p in (first, second)] == [0.5, 2.5]
    assert datetime.fromisoformat(first["start"]) == START
    assert datetime.fromisoformat(first["end"]) == datetime.fromisoformat(second["start"]) == START + timedelta(days=2)
    assert datetime.fromisoformat(second["end"]) == START + timedelta(days=4)
    assert series["sample_count"] == 6 and series["average"] == pytest.approx(7 / 6)
    assert series["p50"] is None and series["p95"] is None


async def test_partial_leading_daily_evidence_with_only_raw_points(client, auth_headers, db_session, source):
    admin = await auth_headers()
    asset = (await create_equipment(client, admin, auth_headers))["id"]
    await source(asset, count=4)
    await compact_eligible_raw(db_session, now=START + timedelta(days=730))
    await db_session.commit()
    start = START + timedelta(hours=12)
    await source(asset, at=start, count=3, seconds=1800)
    response = await client.get(PATH, headers=admin, params=window(asset, start, days=1))
    assert response.status_code == 200, response.text
    [series] = response.json()["series"]
    assert series["partial_days"] == 1
    assert series["sample_count"] == 3 and series["average"] == 1
    assert series["p50"] is None and series["p95"] is None
    points = series["points"]
    assert [p["average"] for p in points] == [0, 1, 2]
    assert all(p["resolution"] == "raw" and p["sample_count"] == 1 for p in points)
    assert datetime.fromisoformat(points[0]["start"]) == start
    assert all(start <= datetime.fromisoformat(p["start"]) < datetime.fromisoformat(p["end"]) for p in points)
