"""A selected-rack grant that outlived its rack's placement must not cover that rack at another site.

Rack R sits in site B and the actor's group grants `selected [R]` at B. R then moves to site A, where the actor holds
only `selected [a1]`. The actor's grant at B now confers nothing for R (R is no longer in B), and the actor must not be
treated as holding R at A. Comparing racks per site, as AccessScope.contains does on main, keeps that true. A comparison
that flattens the rack ids across sites would count the stale grant at B as covering R at A, and the actor could then
confer R at A on a new group or administer a user who holds R at A (review finding F1 on PR #91)."""

import pytest

from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

BASE = ["user:read", "user:manage", "group:read", "group:manage", "rack:read", "rack:manage", "rack:place"]


def _entry(site, scope="all", racks=()):
    return {"site_id": site, "rack_scope": scope, "rack_ids": list(racks)}


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def stale(client, admin, auth_headers):
    """An actor holding `selected [a1]` at A and a now-stale `selected [R]` at B; R has moved to A."""
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    rack_a1 = await _make_rack(client, admin, auth_headers, a["room"])
    rack_r = await _make_rack(client, admin, auth_headers, b["room"])
    gid = await _group(client, admin, allow=BASE, sites=[_entry(a["site"], "selected", [rack_a1]), _entry(b["site"], "selected", [rack_r])])
    actor, headers = await _group_user(client, admin, [gid])
    moved = await client.post(f"/api/v1/racks/{rack_r}/move", json={"room_id": a["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin)
    assert moved.status_code == 200, moved.text
    assert (await client.get(f"/api/v1/racks/{rack_r}", headers=headers)).status_code == 404, "the actor really cannot see R at A"
    return {"a": a, "b": b, "rack_a1": rack_a1, "rack_r": rack_r, "actor": actor, "headers": headers}


async def test_actor_cannot_confer_a_rack_it_holds_only_through_a_stale_grant_at_another_site(client, admin, stale):
    group = (await client.post("/api/v1/groups", json={"name": "stale-conferral"}, headers=stale["headers"])).json()["id"]
    resp = await client.put(
        f"/api/v1/groups/{group}/site-access",
        json={"sites": [_entry(stale["a"]["site"], "selected", [stale["rack_r"]])]},
        headers=stale["headers"],
    )
    assert resp.status_code == 403, resp.text
    assert (await client.get(f"/api/v1/groups/{group}", headers=admin)).json()["sites"] == []


async def test_actor_cannot_administer_a_user_holding_a_rack_it_holds_only_through_a_stale_grant(client, admin, stale):
    target_group = await _group(client, admin, allow=["rack:read"], sites=[_entry(stale["a"]["site"], "selected", [stale["rack_r"]])])
    target, _ = await _group_user(client, admin, [target_group])
    before = (await client.get(f"/api/v1/users/{target['id']}", headers=admin)).json()["full_name"]
    resp = await client.patch(f"/api/v1/users/{target['id']}", json={"full_name": "taken over"}, headers=stale["headers"])
    assert resp.status_code in (403, 404), resp.text
    assert (await client.get(f"/api/v1/users/{target['id']}", headers=admin)).json()["full_name"] == before


async def test_control_a_rack_held_at_its_own_site_can_still_be_conferred(client, admin, stale):
    """The same requests succeed when the actor really holds the rack at A (so the 403s above come from the stale grant)."""
    holder_group = await _group(client, admin, allow=BASE, sites=[_entry(stale["a"]["site"], "selected", [stale["rack_a1"], stale["rack_r"]])])
    _, holder = await _group_user(client, admin, [holder_group])
    group = (await client.post("/api/v1/groups", json={"name": "legit-conferral"}, headers=holder)).json()["id"]
    resp = await client.put(
        f"/api/v1/groups/{group}/site-access",
        json={"sites": [_entry(stale["a"]["site"], "selected", [stale["rack_r"]])]},
        headers=holder,
    )
    assert resp.status_code == 200, resp.text


async def test_actor_cannot_confer_a_moved_rack_at_its_new_site_through_a_stale_grant_at_the_old_one(client, admin, auth_headers):
    """The mirror image (Codex review of 7674ddf): the actor holds `selected [R]` at A and an empty `selected` grant at B.
    R moves to B, so the actor loses it. Conferring R under B passed the flattened comparison because R was already in
    the actor's rack ids, although the actor cannot see R at B."""
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    rack_r = await _make_rack(client, admin, auth_headers, a["room"])
    gid = await _group(client, admin, allow=BASE, sites=[_entry(a["site"], "selected", [rack_r]), _entry(b["site"], "selected", [])])
    _, headers = await _group_user(client, admin, [gid])
    moved = await client.post(f"/api/v1/racks/{rack_r}/move", json={"room_id": b["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin)
    assert moved.status_code == 200, moved.text
    assert (await client.get(f"/api/v1/racks/{rack_r}", headers=headers)).status_code == 404
    group = (await client.post("/api/v1/groups", json={"name": "mirror-conferral"}, headers=headers)).json()["id"]
    resp = await client.put(f"/api/v1/groups/{group}/site-access", json={"sites": [_entry(b["site"], "selected", [rack_r])]}, headers=headers)
    assert resp.status_code == 403, resp.text
    assert (await client.get(f"/api/v1/groups/{group}", headers=admin)).json()["sites"] == []
