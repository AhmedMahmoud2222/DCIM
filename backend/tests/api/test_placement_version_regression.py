"""Issue #105 made equipment placement versions increase per asset (they used to restart at 1 on every move, which let a
stale If-Match match the new row). This module pins that change against the general placement behaviour it shares with
racks, equipment and #104: history, stale-token rejection, current-row uniqueness and rack-unit exclusion."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.api._phase2_helpers import create_equipment, create_rack, create_room

MOVE = "/api/v1/equipment/{id}/move"


async def versions(db, asset_id):
    rows = (await db.execute(text("SELECT version, effective_to IS NULL AS current, placement_type FROM equipment_placement WHERE equipment_id = :i ORDER BY effective_from, version"), {"i": asset_id})).all()
    return [(r.version, r.current, r.placement_type) for r in rows]


async def test_equipment_moves_keep_history_and_versions_increase(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers, auth_headers)
    for _ in range(3):
        assert (await client.post(MOVE.format(id=eq["id"]), json={"placement_type": "floor_standing", "room_id": room}, headers=headers)).status_code == 200
    assert await versions(db_session, eq["id"]) == [(1, False, "floor_standing"), (2, False, "floor_standing"), (3, True, "floor_standing")]


async def test_a_stale_if_match_is_rejected_and_the_current_one_accepted(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers, auth_headers)
    body = {"placement_type": "floor_standing", "room_id": room}
    await client.post(MOVE.format(id=eq["id"]), json=body, headers=headers)  # version 1
    ok = await client.post(MOVE.format(id=eq["id"]), json=body, headers={**headers, "If-Match": "1"})  # -> version 2
    assert ok.status_code == 200
    stale = await client.post(MOVE.format(id=eq["id"]), json=body, headers={**headers, "If-Match": "1"})  # the old token must not match the new row
    assert stale.status_code == 409 and "current version=2" in stale.json()["detail"]
    assert (await client.post(MOVE.format(id=eq["id"]), json=body, headers={**headers, "If-Match": "2"})).status_code == 200
    assert [v for v, _, _ in await versions(db_session, eq["id"])] == [1, 2, 3]


async def test_the_database_still_allows_only_one_current_placement_per_equipment(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers, auth_headers)
    await client.post(MOVE.format(id=eq["id"]), json={"placement_type": "floor_standing", "room_id": room}, headers=headers)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 9)"),
            {"e": eq["id"], "r": room},
        )
    await db_session.rollback()


async def test_rack_mounted_moves_and_unit_overlap_exclusion_are_unchanged(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room)
    first, second = await create_equipment(client, headers, auth_headers), await create_equipment(client, headers, auth_headers)
    mounted = {"placement_type": "rack_mounted", "room_id": room, "rack_id": rack["id"], "u_start": 5, "u_end": 7, "side": "front"}
    assert (await client.post(MOVE.format(id=first["id"]), json=mounted, headers=headers)).status_code == 200
    clash = await client.post(MOVE.format(id=second["id"]), json=mounted, headers=headers)
    assert clash.status_code == 409  # the GiST exclusion constraint still refuses overlapping rack units
    await db_session.rollback()  # the shared test session is left mid-failed-flush by the exclusion violation
    moved = await client.post(MOVE.format(id=first["id"]), json={**mounted, "u_start": 9, "u_end": 11}, headers={**headers, "If-Match": "1"})
    assert moved.status_code == 200
    assert [(v, c) for v, c, _ in await versions(db_session, first["id"])] == [(1, False), (2, True)]


async def test_rack_move_versions_and_stale_token_behaviour_are_unchanged(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room, x_mm=100, y_mm=100, rotation_deg=0)
    move = lambda x, v=None: client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room, "x_mm": x, "y_mm": 100, "rotation_deg": 0}, headers={**headers, **({"If-Match": str(v)} if v else {})})  # noqa: E731
    assert (await move(200, 1)).status_code == 200
    assert (await move(300, 1)).status_code == 409
    assert (await move(300, 2)).status_code == 200
    rows = (await db_session.execute(text("SELECT version, x_mm, effective_to IS NULL AS c FROM rack_placement WHERE rack_id = :r ORDER BY version"), {"r": rack["id"]})).all()
    assert [(r.version, r.x_mm, r.c) for r in rows] == [(1, 100, False), (2, 200, False), (3, 300, True)]


async def test_sensor_moves_use_the_same_monotonic_versions(client, auth_headers, db_session):
    from tests.api._thermal_helpers import C, make_sensor, make_world, seed_plan

    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    sensor = await make_sensor(client, admin, site["site"], site["room"], 100, 100)
    url = f"{C}/sensors/{sensor['id']}/placement"
    assert (await client.put(url, json={"room_id": site["room"], "x_mm": 200, "y_mm": 100}, headers=admin, params={"if_match": 1})).status_code == 200
    assert (await client.put(url, json={"room_id": site["room"], "x_mm": 300, "y_mm": 100}, headers=admin, params={"if_match": 1})).status_code == 409
    assert (await client.put(url, json={"room_id": site["room"], "x_mm": 300, "y_mm": 100}, headers=admin, params={"if_match": 2})).status_code == 200
    assert [(v, c) for v, c, _ in await versions(db_session, sensor["id"])] == [(1, False), (2, False), (3, True)]
    assert uuid.UUID(sensor["id"])
