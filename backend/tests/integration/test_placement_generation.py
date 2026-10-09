"""Issue #105 (review blocker B3): placement generations are strictly monotonic per asset across its WHOLE history.

place -> unplace -> re-place used to restart at version 1 (the version was derived from the current row only), so an
ancient version-1 If-Match could match the new placement (ABA). The shared service now takes a per-asset advisory lock
and allocates `max(version over every row of the asset) + 1`.

Concurrency claims are tested with two real sessions and an explicit hold: session A keeps its transaction open after
writing, session B is started and must be blocked on the per-asset lock until A commits. Nothing relies on timing luck."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.placement_service import (
    PlacementConflict,
    move_equipment,
    move_rack,
    retire_equipment_placement,
    retire_rack_placement,
)
from tests.api._phase2_helpers import create_equipment, create_rack, create_room
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest.fixture
async def assets(client, auth_headers):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    equipment = await create_equipment(client, headers, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room, x_mm=100, y_mm=100, rotation_deg=0)
    return {"room": uuid.UUID(room), "equipment": uuid.UUID(equipment["id"]), "rack": uuid.UUID(rack["id"])}


async def versions(factory, table: str, column: str, asset_id: uuid.UUID) -> list[tuple[int, bool]]:
    async with factory() as s:
        rows = (await s.execute(text(f"SELECT version, effective_to IS NULL FROM {table} WHERE {column} = :i ORDER BY effective_from, version"), {"i": asset_id})).all()  # noqa: S608
    return [(r[0], r[1]) for r in rows]


async def place_eq(factory, assets, *, if_match=None, commit=True):
    async with factory() as s:
        row = await move_equipment(s, equipment_id=assets["equipment"], placement_type="floor_standing", room_id=assets["room"], if_match_version=if_match)
        if commit:
            await s.commit()
        return row.version


async def unplace_eq(factory, assets, *, if_match=None):
    async with factory() as s:
        row = await retire_equipment_placement(s, equipment_id=assets["equipment"], if_match_version=if_match)
        await s.commit()
        return row


async def place_rack(factory, assets, *, if_match=None, x=100):
    async with factory() as s:
        row = await move_rack(s, rack_id=assets["rack"], room_id=assets["room"], x_mm=x, y_mm=100, rotation_deg=0, if_match_version=if_match)
        await s.commit()
        return row.version


async def unplace_rack(factory, assets, *, if_match=None):
    async with factory() as s:
        row = await retire_rack_placement(s, rack_id=assets["rack"], if_match_version=if_match)
        await s.commit()
        return row


# ----------------------------------------------------------------------------------------------- equipment


async def test_equipment_place_unplace_replace_is_strictly_increasing(factory, assets):
    assert await place_eq(factory, assets) == 1
    assert await unplace_eq(factory, assets) is not None
    assert await place_eq(factory, assets) == 2  # used to restart at 1
    assert await versions(factory, "equipment_placement", "equipment_id", assets["equipment"]) == [(1, False), (2, True)]


async def test_equipment_place_move_unplace_replace_keeps_increasing(factory, assets):
    seen = [await place_eq(factory, assets)]
    seen.append(await place_eq(factory, assets, if_match=1))
    await unplace_eq(factory, assets, if_match=2)
    seen.append(await place_eq(factory, assets))
    seen.append(await place_eq(factory, assets, if_match=3))
    await unplace_eq(factory, assets)
    seen.append(await place_eq(factory, assets))
    assert seen == [1, 2, 3, 4, 5]
    rows = await versions(factory, "equipment_placement", "equipment_id", assets["equipment"])
    assert [v for v, _ in rows] == [1, 2, 3, 4, 5] and [c for _, c in rows].count(True) == 1


async def test_an_old_token_from_the_first_placement_is_rejected_against_the_new_placement(factory, assets):
    await place_eq(factory, assets)  # v1
    await unplace_eq(factory, assets)
    await place_eq(factory, assets)  # v2
    with pytest.raises(PlacementConflict):
        await place_eq(factory, assets, if_match=1)  # the ancient token used to match the restarted row
    with pytest.raises(PlacementConflict):
        await unplace_eq(factory, assets, if_match=1)
    assert await place_eq(factory, assets, if_match=2) == 3


async def test_a_token_against_an_unplaced_asset_is_a_conflict_and_creates_nothing(factory, assets):
    with pytest.raises(PlacementConflict) as raised:
        await place_eq(factory, assets, if_match=1)
    assert raised.value.current is None
    await place_eq(factory, assets)
    await unplace_eq(factory, assets)
    with pytest.raises(PlacementConflict):
        await place_eq(factory, assets, if_match=1)  # unplaced now; the old generation must not be honoured
    assert await versions(factory, "equipment_placement", "equipment_id", assets["equipment"]) == [(1, False)]


# ----------------------------------------------------------------------------------------------------- racks


async def test_rack_place_unplace_replace_is_strictly_increasing_and_old_tokens_fail(factory, assets):
    # the rack fixture already placed the rack (version 1)
    assert await unplace_rack(factory, assets) is not None
    assert await place_rack(factory, assets, x=200) == 2
    assert await place_rack(factory, assets, if_match=2, x=300) == 3
    await unplace_rack(factory, assets, if_match=3)
    assert await place_rack(factory, assets, x=400) == 4
    with pytest.raises(PlacementConflict):
        await place_rack(factory, assets, if_match=1, x=500)
    rows = await versions(factory, "rack_placement", "rack_id", assets["rack"])
    assert [v for v, _ in rows] == [1, 2, 3, 4] and [c for _, c in rows].count(True) == 1


async def test_a_token_against_an_unplaced_rack_is_a_conflict(factory, assets):
    await unplace_rack(factory, assets)
    with pytest.raises(PlacementConflict):
        await place_rack(factory, assets, if_match=1)
    assert [c for _, c in await versions(factory, "rack_placement", "rack_id", assets["rack"])] == [False]


# ------------------------------------------------------------------------------------------- concurrency


async def test_concurrent_first_placements_are_serialised_and_never_share_a_generation(factory, assets):
    async with factory() as a:
        first = await move_equipment(a, equipment_id=assets["equipment"], placement_type="floor_standing", room_id=assets["room"])
        assert first.version == 1  # A holds the asset lock with its row written but NOT committed
        second = asyncio.create_task(place_eq(factory, assets))
        await asyncio.sleep(1.0)
        assert not second.done(), "B must be blocked on the per-asset lock until A commits"
        await a.commit()
    # B observed "unplaced" before waiting; A created the placement meanwhile, so B loses with a conflict (never a second v1)
    with pytest.raises(PlacementConflict):
        await asyncio.wait_for(second, 10)
    rows = await versions(factory, "equipment_placement", "equipment_id", assets["equipment"])
    assert rows == [(1, True)]


async def test_concurrent_replacement_after_unplace_is_serialised(factory, assets):
    await place_eq(factory, assets)
    await unplace_eq(factory, assets)
    async with factory() as a:
        replaced = await move_equipment(a, equipment_id=assets["equipment"], placement_type="floor_standing", room_id=assets["room"])
        assert replaced.version == 2
        rival = asyncio.create_task(place_eq(factory, assets))
        await asyncio.sleep(1.0)
        assert not rival.done()
        await a.commit()
    with pytest.raises(PlacementConflict):
        await asyncio.wait_for(rival, 10)
    rows = await versions(factory, "equipment_placement", "equipment_id", assets["equipment"])
    assert [v for v, _ in rows] == [1, 2] and [c for _, c in rows].count(True) == 1
    assert await place_eq(factory, assets) == 3  # a later caller that observes the new state proceeds with the next generation


async def test_a_waiting_stale_writer_loses_cleanly_after_the_winner_commits(factory, assets):
    await place_eq(factory, assets)  # v1
    async with factory() as a:
        winner = await move_equipment(a, equipment_id=assets["equipment"], placement_type="floor_standing", room_id=assets["room"], if_match_version=1)
        assert winner.version == 2
        loser = asyncio.create_task(place_eq(factory, assets, if_match=1))
        await asyncio.sleep(1.0)
        assert not loser.done()
        await a.commit()
    with pytest.raises(PlacementConflict):
        await asyncio.wait_for(loser, 10)
    assert await versions(factory, "equipment_placement", "equipment_id", assets["equipment"]) == [(1, False), (2, True)]


async def test_concurrent_rack_replacement_is_serialised(factory, assets):
    await unplace_rack(factory, assets)
    async with factory() as a:
        mine = await move_rack(a, rack_id=assets["rack"], room_id=assets["room"], x_mm=700, y_mm=100, rotation_deg=0)
        assert mine.version == 2
        rival = asyncio.create_task(place_rack(factory, assets, x=800))
        await asyncio.sleep(1.0)
        assert not rival.done()
        await a.commit()
    with pytest.raises(PlacementConflict):
        await asyncio.wait_for(rival, 10)
    rows = await versions(factory, "rack_placement", "rack_id", assets["rack"])
    assert [v for v, _ in rows] == [1, 2] and [c for _, c in rows].count(True) == 1
    assert await place_rack(factory, assets, x=900) == 3


async def test_many_simultaneous_first_placements_yield_one_winner_and_no_duplicate_generation(factory, assets):
    results = await asyncio.gather(*(place_eq(factory, assets) for _ in range(8)), return_exceptions=True)
    assert [r for r in results if not isinstance(r, Exception)] == [1]
    assert all(isinstance(r, PlacementConflict) for r in results if isinstance(r, Exception)), results
    rows = await versions(factory, "equipment_placement", "equipment_id", assets["equipment"])
    assert rows == [(1, True)]


async def test_sequential_placements_keep_counting_after_such_a_race(factory, assets):
    await asyncio.gather(*(place_eq(factory, assets) for _ in range(4)), return_exceptions=True)
    for expected in (2, 3, 4):
        assert await place_eq(factory, assets) == expected
    rows = await versions(factory, "equipment_placement", "equipment_id", assets["equipment"])
    assert [v for v, _ in rows] == [1, 2, 3, 4] and [c for _, c in rows].count(True) == 1
