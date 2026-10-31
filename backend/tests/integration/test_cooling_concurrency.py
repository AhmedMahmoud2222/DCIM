"""Issue #105: concurrent mutations of cooling configuration. Every request gets its own database session, so the
asyncio.gather calls below are genuinely concurrent transactions."""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.api._spatial_helpers import concurrent_client
from tests.api._thermal_helpers import C, activate, make_sensor, make_unit, make_world, make_zone, seed_plan


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    return {"admin": admin, "site": site["site"], "room": site["room"], "db": db_session}


def statuses(responses):
    return sorted(r.status_code for r in responses)


async def test_two_operators_editing_the_same_unit_one_wins_one_conflicts(client, world):
    unit = await make_unit(client, world["admin"], world["site"], rated_cooling_capacity_kw=100)
    async with concurrent_client() as c:
        results = await asyncio.gather(
            *(c.patch(f"{C}/units/{unit['id']}", json={"name": f"Editor-{i}"}, headers={**world["admin"], "If-Match": "1"}) for i in range(8))
        )
    assert statuses(results) == [200] + [409] * 7
    final = (await client.get(f"{C}/units/{unit['id']}", headers=world["admin"])).json()
    assert final["version"] == 2 and final["name"].startswith("Editor-")


async def test_simultaneous_sensor_moves_leave_exactly_one_current_placement(client, world):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000)
    async with concurrent_client() as c:
        results = await asyncio.gather(
            *(c.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": world["room"], "x_mm": 1000 + i * 100, "y_mm": 2000}, headers={**world["admin"], "If-Match": "1"}) for i in range(6))
        )
    assert statuses(results) == [200] + [409] * 5
    rows = (await world["db"].execute(text("SELECT x_mm, effective_to IS NULL AS current, version FROM equipment_placement WHERE equipment_id = :i ORDER BY effective_from, version"), {"i": sensor["id"]})).all()
    assert [r.current for r in rows].count(True) == 1 and len(rows) == 2 and rows[-1].version == 2


async def test_unconditional_concurrent_moves_still_keep_one_current_row(client, world):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000)
    async with concurrent_client() as c:
        results = await asyncio.gather(
            *(c.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": world["room"], "x_mm": 500 + i * 50, "y_mm": 900}, headers=world["admin"]) for i in range(6))
        )
    assert all(code in (200, 409) for code in statuses(results)) and 200 in statuses(results)
    current = (await world["db"].execute(text("SELECT count(*) FROM equipment_placement WHERE equipment_id = :i AND effective_to IS NULL"), {"i": sensor["id"]})).scalar_one()
    assert current == 1


async def test_concurrent_containment_geometry_edits_serialise_on_the_zone_version(client, world):
    aisle = await make_zone(client, world["admin"], world["room"], zone_kind="hot_aisle", containment="contained", geometry_type="rect", x_mm=1000, y_mm=500, width_mm=1000, height_mm=3000)
    body = lambda i: {"element_kind": "boundary", "x1_mm": 1000, "y1_mm": 500 + i * 10, "x2_mm": 1000, "y2_mm": 1500}  # noqa: E731
    async with concurrent_client() as c:
        added = await asyncio.gather(*(c.post(f"{C}/zones/{aisle['id']}/containment-elements", json=body(i), headers={**world["admin"], "If-Match": "1"}) for i in range(6)))
        moved = await asyncio.gather(
            *(c.patch(f"{C}/zones/{aisle['id']}", json={"x_mm": 1200 + i * 10}, headers={**world["admin"], "If-Match": "2"}) for i in range(4))
        )
    assert statuses(added) == [201] + [409] * 5 and statuses(moved) == [200] + [409] * 3
    zone = (await client.get(f"{C}/zones/{aisle['id']}", headers=world["admin"])).json()
    assert zone["version"] == 3 and len(zone["elements"]) == 1


async def test_retiring_a_unit_races_with_adding_a_zone_relationship(client, world):
    unit = await make_unit(client, world["admin"], world["site"])
    await activate(client, world["admin"], unit["id"])
    zone = await make_zone(client, world["admin"], world["room"])
    async with concurrent_client() as c:
        retire, related = await asyncio.gather(
            c.post(f"{C}/units/{unit['id']}/retire", headers={**world["admin"], "If-Match": "1"}),
            c.post(f"{C}/relations", json={"cooling_unit_id": unit["id"], "thermal_zone_id": zone["id"], "relation_kind": "serves"}, headers=world["admin"]),
        )
    # Either order is acceptable, but never "retired AND related": the database trigger and the checks forbid it.
    state = (await world["db"].execute(text("SELECT a.lifecycle_status, (SELECT count(*) FROM cooling_unit_zone WHERE cooling_unit_id = a.id) AS relations FROM managed_asset a WHERE a.id = :i"), {"i": unit["id"]})).one()
    retired = state.lifecycle_status in ("decommissioned", "removed")
    assert not (retired and state.relations), (retire.status_code, related.status_code, state)
    assert {retire.status_code, related.status_code} <= {200, 201, 409, 422}


async def _wait_for_lock_waiter(monitor, query_like: str, timeout: float = 15.0) -> None:
    """Poll (on an autocommit connection, so pg_stat_activity is never a stale snapshot) until some backend whose
    current statement matches `query_like` is blocked on a heavyweight lock."""
    sql = text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE :q AND pid <> pg_backend_pid()")

    async def _poll():
        while (await monitor.execute(sql, {"q": query_like})).scalar_one() == 0:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout)


@pytest.fixture
async def monitor():
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        yield conn
    await engine.dispose()


async def test_retire_holding_its_locks_while_a_relationship_insert_is_blocked_never_deadlocks(client, world, monitor, monkeypatch):
    """Issue #226, forced interleaving A: the retire has taken every lock it needs before the relationship INSERT starts, and
    the INSERT is then provably blocked on one of them. With the old order (cooling_unit before managed_asset) the INSERT
    held a managed_asset share lock and waited on cooling_unit while the retire waited on managed_asset: 40P01."""
    from app.api.v1 import cooling

    unit = await make_unit(client, world["admin"], world["site"])
    await activate(client, world["admin"], unit["id"])
    zone = await make_zone(client, world["admin"], world["room"])
    locked, proceed = asyncio.Event(), asyncio.Event()
    real_lock = cooling.lock_versioned_row

    async def gated_lock(*args, **kwargs):
        row = await real_lock(*args, **kwargs)  # whichever lock the retire takes last is held when this fires
        locked.set()
        await proceed.wait()
        return row

    monkeypatch.setattr(cooling, "lock_versioned_row", gated_lock)
    async with concurrent_client() as c:
        retire_task = asyncio.create_task(c.post(f"{C}/units/{unit['id']}/retire", headers={**world["admin"], "If-Match": "1"}))
        try:
            await asyncio.wait_for(locked.wait(), 15)
            relate_task = asyncio.create_task(
                c.post(f"{C}/relations", json={"cooling_unit_id": unit["id"], "thermal_zone_id": zone["id"], "relation_kind": "serves"}, headers=world["admin"])
            )
            await _wait_for_lock_waiter(monitor, "INSERT INTO cooling_unit_zone%")
        finally:
            proceed.set()
        retire, related = await asyncio.gather(retire_task, relate_task)
    assert retire.status_code == 200, retire.text  # no deadlock victim, no 500
    assert related.status_code == 409, related.text  # the relationship lost cleanly: a retired unit cannot gain one
    state = (await world["db"].execute(text("SELECT a.lifecycle_status, (SELECT count(*) FROM cooling_unit_zone WHERE cooling_unit_id = a.id) AS relations FROM managed_asset a WHERE a.id = :i"), {"i": unit["id"]})).one()
    assert state.lifecycle_status in ("decommissioned", "removed") and state.relations == 0


async def test_retire_arriving_while_a_relationship_insert_holds_its_locks_is_refused_cleanly(client, world, monitor):
    """Issue #226, forced interleaving B: an uncommitted relationship INSERT already holds its managed_asset share lock and
    cooling_unit key-share lock; the retire must queue behind it (no deadlock), then see the committed relationship."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import TEST_DATABASE_URL

    unit = await make_unit(client, world["admin"], world["site"])
    await activate(client, world["admin"], unit["id"])
    zone = await make_zone(client, world["admin"], world["room"])
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as relating:
            await relating.execute(
                text("INSERT INTO cooling_unit_zone (cooling_unit_id, thermal_zone_id, relation_kind) VALUES (:u, :z, 'serves')"), {"u": unit["id"], "z": zone["id"]}
            )
            async with concurrent_client() as c:
                retire_task = asyncio.create_task(c.post(f"{C}/units/{unit['id']}/retire", headers={**world["admin"], "If-Match": "1"}))
                try:
                    await _wait_for_lock_waiter(monitor, "%FOR UPDATE%")
                finally:
                    await relating.commit()
                retire = await retire_task
    finally:
        await engine.dispose()
    assert retire.status_code == 409, retire.text
    state = (await world["db"].execute(text("SELECT a.lifecycle_status, (SELECT count(*) FROM cooling_unit_zone WHERE cooling_unit_id = a.id) AS relations FROM managed_asset a WHERE a.id = :i"), {"i": unit["id"]})).one()
    assert state.lifecycle_status not in ("decommissioned", "removed") and state.relations == 1


async def test_zone_membership_changes_race_cleanly(client, world):
    zone = await make_zone(client, world["admin"], world["room"])
    units = [await make_unit(client, world["admin"], world["site"]) for _ in range(5)]
    async with concurrent_client() as c:
        created = await asyncio.gather(
            *(c.post(f"{C}/relations", json={"cooling_unit_id": u["id"], "thermal_zone_id": zone["id"], "relation_kind": "serves"}, headers=world["admin"]) for u in units for _ in range(2))
        )
    assert statuses(created).count(201) == 5 and statuses(created).count(409) == 5  # duplicates lose, each unit related exactly once
    assert len((await client.get(f"{C}/relations", params={"thermal_zone_id": zone["id"]}, headers=world["admin"])).json()) == 5


async def test_moving_a_sensor_while_a_map_is_generated_never_yields_a_torn_read(client, world):
    from tests.api._thermal_helpers import TelemetrySeeder

    seed = TelemetrySeeder(world["db"])
    sensors = []
    for i, (x, y, v) in enumerate(((1000, 1000, 20.0), (5000, 1000, 30.0), (1000, 3000, 24.0), (5000, 3000, 26.0))):
        s = await make_sensor(client, world["admin"], world["site"], world["room"], x, y, name=f"S{i}")
        await seed.reading(s["id"], "temperature_c", v)
        sensors.append(s)
    async with concurrent_client() as c:
        moves = [c.put(f"{C}/sensors/{sensors[0]['id']}/placement", json={"room_id": world["room"], "x_mm": 1000 + i * 40, "y_mm": 1000}, headers=world["admin"]) for i in range(6)]
        maps = [c.get(f"{C}/rooms/{world['room']}/heat-map", headers=world["admin"]) for _ in range(6)]
        results = await asyncio.gather(*moves, *maps)
    for response in results[6:]:
        body = response.json()
        assert response.status_code == 200 and body["quality"]["sensor_count"] == 4 and body["state"] == "healthy"  # always exactly one position per sensor
        assert len({s["sensor_id"] for s in body["sensors"]}) == 4
    assert uuid.UUID(sensors[0]["id"])
