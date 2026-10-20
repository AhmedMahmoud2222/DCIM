"""PR #59 scope evidence not otherwise on main: hierarchy listings, rack-limited users, stale rack grants.

Object-level isolation and the route sweep live in test_pr69_object_matrix_strict.py and test_pr69_route_sweep_strict.py."""

import pytest

from tests.api._phase2_helpers import create_equipment
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

DATA_PERMS = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)  # separate organization: also tests org/country/city leakage
    w = {"a": a, "b": b}
    for key, site in (("a1", a), ("a2", a), ("b1", b)):
        w[f"rack_{key}"] = await _make_rack(client, admin, auth_headers, site["room"])
    for key, rack, site in (("a1", w["rack_a1"], a), ("a2", w["rack_a2"], a), ("b1", w["rack_b1"], b)):
        eq = await create_equipment(client, admin, auth_headers)
        mv = await client.post(
            f"/api/v1/equipment/{eq['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": site["room"], "rack_id": rack, "u_start": 1, "u_end": 2, "side": "front"},
            headers=admin,
        )
        assert mv.status_code == 200, mv.text
        w[f"eq_{key}"] = eq["id"]
    eq_room_a = await create_equipment(client, admin, auth_headers)
    mv = await client.post(f"/api/v1/equipment/{eq_room_a['id']}/move", json={"placement_type": "floor_standing", "room_id": a["room"]}, headers=admin)
    assert mv.status_code == 200, mv.text  # setup must not silently drop the room-placed equipment
    w["eq_room_a"] = eq_room_a["id"]
    return w


async def _restricted(client, admin, site, *, rack_scope="all", rack_ids=(), perms=DATA_PERMS):
    gid = await _group(client, admin, allow=perms, sites=[{"site_id": site, "rack_scope": rack_scope, "rack_ids": list(rack_ids)}])
    return (await _group_user(client, admin, [gid]))[1]


def _ids(page):
    return {item["id"] for item in page["items"]}


async def test_collection_endpoints_exclude_other_site_and_report_true_totals(client, admin, world):
    ua = await _restricted(client, admin, world["a"]["site"])
    racks = (await client.get("/api/v1/racks?limit=200", headers=ua)).json()
    assert world["rack_b1"] not in _ids(racks)
    assert {world["rack_a1"], world["rack_a2"]} <= _ids(racks)
    assert racks["total"] == len(_ids(racks)) == 2  # count must not include Site B racks
    assert (await client.get(f"/api/v1/racks?site_id={world['b']['site']}", headers=ua)).json()["total"] == 0
    eq = (await client.get("/api/v1/equipment?limit=200", headers=ua)).json()
    assert world["eq_b1"] not in _ids(eq) and eq["total"] == len(_ids(eq))
    assert {world["eq_a1"], world["eq_a2"]} <= _ids(eq)
    for path, own in (("sites", world["a"]["site"]), ("rooms", world["a"]["room"]), ("organizations", world["a"]["org"])):
        page = (await client.get(f"/api/v1/{path}?limit=200", headers=ua)).json()
        assert _ids(page) == {own}, path
    for path in ("countries", "cities", "buildings", "floors"):
        page = (await client.get(f"/api/v1/{path}?limit=200", headers=ua)).json()
        assert page["total"] == len(page["items"]) == 1, path


async def test_rack_limited_user_sees_only_selected_rack_and_its_equipment(client, admin, world):
    ur = await _restricted(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    assert (await client.get(f"/api/v1/racks/{world['rack_a1']}", headers=ur)).status_code == 200
    assert (await client.get(f"/api/v1/racks/{world['rack_a2']}", headers=ur)).status_code == 404
    assert _ids((await client.get("/api/v1/racks", headers=ur)).json()) == {world["rack_a1"]}
    eq_ids = _ids((await client.get("/api/v1/equipment", headers=ur)).json())
    assert world["eq_a1"] in eq_ids and world["eq_a2"] not in eq_ids and world["eq_b1"] not in eq_ids
    assert (await client.get(f"/api/v1/equipment/{world['eq_a2']}", headers=ur)).status_code == 404
    # Room-placed equipment is only visible with rack_scope=all: hidden from the rack-limited user, visible to an all-racks user.
    assert world["eq_room_a"] not in eq_ids
    assert (await client.get(f"/api/v1/equipment/{world['eq_room_a']}", headers=ur)).status_code == 404
    ua = await _restricted(client, admin, world["a"]["site"])
    assert (await client.get(f"/api/v1/equipment/{world['eq_room_a']}", headers=ua)).status_code == 200


async def test_stale_rack_grant_does_not_follow_a_rack_to_another_site(client, admin, world):
    ur = await _restricted(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    moved = await client.post(
        f"/api/v1/racks/{world['rack_a1']}/move", json={"room_id": world["b"]["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin
    )
    assert moved.status_code == 200, moved.text
    assert (await client.get(f"/api/v1/racks/{world['rack_a1']}", headers=ur)).status_code == 404
