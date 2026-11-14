"""Equipment responses carry the Site > Building > Floor > Room > Rack path of the current placement."""

import uuid
from types import SimpleNamespace

from app.api.v1.equipment import _location_out
from app.application.access_control import EMPTY_SCOPE, UNRESTRICTED, AccessScope
from tests.api._phase2_helpers import create_equipment, create_rack_model_revision
from tests.api.test_user_groups import _make_site


async def _rack(client, admin, auth_headers, room_id, name):
    revision = await create_rack_model_revision(client, auth_headers)
    resp = await client.post(
        "/api/v1/racks", json={"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision, "name": name, "room_id": room_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mount(client, headers, equipment_id, room_id, rack_id):
    resp = await client.post(
        f"/api/v1/equipment/{equipment_id}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack_id, "u_start": 1, "u_end": 2, "side": "front"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_get_list_and_move_return_the_full_location_path(client, auth_headers):
    admin = await auth_headers("Administrator")
    world = await _make_site(client, admin)
    rack = await _rack(client, admin, auth_headers, world["room"], "Rack-A1")
    equipment = await create_equipment(client, admin, auth_headers)
    assert equipment["location"] is None  # unplaced equipment has no location

    moved = await _mount(client, admin, equipment["id"], world["room"], rack["id"])
    expected = {
        "site": world["site"], "building": world["building"], "floor": world["floor"], "room": world["room"], "rack": rack["id"],
    }
    for payload in (
        moved,
        (await client.get(f"/api/v1/equipment/{equipment['id']}", headers=admin)).json(),
        next(i for i in (await client.get("/api/v1/equipment", headers=admin)).json()["items"] if i["id"] == equipment["id"]),
    ):
        location = payload["location"]
        assert {key: location[key]["id"] for key in expected} == expected
        assert location["site"]["name"] == "Site"
        assert location["building"]["name"] == "B"
        assert location["floor"]["name"] == "F"
        assert location["room"]["name"] == "Room"
        assert location["rack"]["name"] == "Rack-A1"


async def test_floor_standing_equipment_has_room_but_no_rack(client, auth_headers):
    admin = await auth_headers("Administrator")
    world = await _make_site(client, admin)
    equipment = await create_equipment(client, admin, auth_headers)
    resp = await client.post(
        f"/api/v1/equipment/{equipment['id']}/move", json={"placement_type": "floor_standing", "room_id": world["room"]}, headers=admin,
    )
    location = resp.json()["location"]
    assert location["rack"] is None
    assert location["room"]["id"] == world["room"]
    assert location["site"]["id"] == world["site"]


def test_the_whole_location_is_withheld_when_the_caller_may_not_see_the_equipment():
    ids = {key: uuid.uuid4() for key in ("room", "floor", "building", "site", "rack", "other_rack")}
    row = SimpleNamespace(
        room_id=ids["room"], room_name="R", room_code="r", floor_id=ids["floor"], floor_name="F", building_id=ids["building"],
        building_name="B", building_code="b", site_id=ids["site"], site_name="S", site_code="s",
    )
    placement = SimpleNamespace(room_id=ids["room"], rack_id=ids["rack"])
    rooms, racks = {ids["room"]: row}, {ids["rack"]: "K"}
    for scope in (EMPTY_SCOPE, AccessScope(unrestricted=False, site_ids=frozenset({ids["site"]}), rack_ids=frozenset({ids["other_rack"]}))):
        hidden = _location_out(placement, rooms, racks, scope)
        assert hidden is not None and not any((hidden.site, hidden.building, hidden.floor, hidden.room, hidden.rack)), scope
    own_rack = AccessScope(unrestricted=False, site_ids=frozenset({ids["site"]}), rack_ids=frozenset({ids["rack"]}))
    granted = _location_out(placement, rooms, racks, own_rack)
    assert granted.rack.name == "K" and granted.room.name == "R"
    whole_site = AccessScope(unrestricted=False, site_ids=frozenset({ids["site"]}), full_site_ids=frozenset({ids["site"]}))
    assert _location_out(placement, rooms, racks, whole_site).site.name == "S"
    shown = _location_out(placement, rooms, racks, UNRESTRICTED)
    assert shown is not None and shown.site.name == "S" and shown.building.name == "B" and shown.floor.name == "F"
    assert _location_out(None, {}, {}, UNRESTRICTED) is None


async def test_a_scoped_caller_who_moves_equipment_into_a_foreign_site_is_not_told_where_it_went(client, auth_headers):
    """The move endpoint checks the permission, not the caller's site scope (that gap is older than this feature). Whatever it
    allows, its response must not carry the foreign room, rack or site names."""
    from tests.api.test_user_groups import _group, _group_user

    admin = await auth_headers("Administrator")
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)
    rack_a = await _rack(client, admin, auth_headers, a["room"], "A-ONE")
    rack_b = await _rack(client, admin, auth_headers, b["room"], "SECRET-B-RACK")
    mine = await create_equipment(client, admin, auth_headers)
    await _mount(client, admin, mine["id"], a["room"], rack_a["id"])
    gid = await _group(client, admin, allow=["equipment:read", "equipment:place", "rack:read", "location:read"],
                       sites=[{"site_id": a["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    resp = await client.post(
        f"/api/v1/equipment/{mine['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": b["room"], "rack_id": rack_b["id"], "u_start": 5, "u_end": 7, "side": "front"},
        headers=headers,
    )
    assert resp.status_code in (200, 403, 404), resp.text
    assert "SECRET-B-RACK" not in resp.text and b["site"] not in resp.text
    if resp.status_code == 200:
        assert resp.json()["location"] == {"site": None, "building": None, "floor": None, "room": None, "rack": None}


async def test_a_rack_scoped_caller_sees_locations_only_for_equipment_in_their_racks(client, auth_headers):
    """Rack-only and foreign-site callers: equipment outside their grant is absent, and what they do see carries only
    its own hierarchy (never another site's names)."""
    from tests.api.test_user_groups import _group, _group_user

    admin = await auth_headers("Administrator")
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)
    rack_a1 = await _rack(client, admin, auth_headers, a["room"], "A-ONE")
    rack_a2 = await _rack(client, admin, auth_headers, a["room"], "A-TWO")
    rack_b = await _rack(client, admin, auth_headers, b["room"], "B-ONE")
    mine = await create_equipment(client, admin, auth_headers)
    sibling = await create_equipment(client, admin, auth_headers)
    foreign = await create_equipment(client, admin, auth_headers)
    await _mount(client, admin, mine["id"], a["room"], rack_a1["id"])
    await _mount(client, admin, sibling["id"], a["room"], rack_a2["id"])
    await _mount(client, admin, foreign["id"], b["room"], rack_b["id"])

    gid = await _group(client, admin, allow=["equipment:read", "rack:read", "location:read"],
                       sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack_a1["id"]]}])
    _, headers = await _group_user(client, admin, [gid])
    items = (await client.get("/api/v1/equipment", headers=headers)).json()["items"]
    assert [i["id"] for i in items] == [mine["id"]]
    assert items[0]["location"]["rack"]["name"] == "A-ONE" and items[0]["location"]["site"]["id"] == a["site"]
    assert (await client.get(f"/api/v1/equipment/{foreign['id']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/equipment/{sibling['id']}", headers=headers)).status_code == 404
    assert "B-ONE" not in (await client.get("/api/v1/equipment", headers=headers)).text


async def test_the_hierarchy_lookup_runs_once_per_page_not_once_per_item(client, auth_headers, db_engine):
    """Query-count assertion: the location lookup (room > floor > building > site join) must not grow with the page size."""
    from sqlalchemy import event

    admin = await auth_headers("Administrator")
    world = await _make_site(client, admin)
    rack = await _rack(client, admin, auth_headers, world["room"], "Q-ONE")
    for _ in range(6):
        eq = await create_equipment(client, admin, auth_headers)
        await _mount_unique(client, admin, eq["id"], world["room"], rack["id"], _)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        await client.get("/api/v1/equipment", headers=admin)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)
    hierarchy = [s for s in statements if "FROM room" in s and "JOIN floor" in s and "JOIN building" in s and "JOIN site" in s]
    assert len(hierarchy) <= 1, f"{len(hierarchy)} hierarchy lookups for 6 items"


async def _mount_unique(client, headers, equipment_id, room_id, rack_id, slot):
    resp = await client.post(
        f"/api/v1/equipment/{equipment_id}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack_id, "u_start": 1 + slot * 2, "u_end": 3 + slot * 2, "side": "front"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
