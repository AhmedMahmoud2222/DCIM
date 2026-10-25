"""Issue #195 / P2 F3: group detail and list must not reveal members the caller cannot see in the user directory.

The user-directory policy (GET /users, GET /users/{id}) shows a caller the users whose whole scope lies inside the caller's
`user:read` scope, plus the caller. A group with no site grants is "contained" by every scope, so any `group:read` holder
sees it; its members must nevertheless be masked by the same policy, in `member_ids` and in `member_count`, in both the
detail and the list representation, so that neither can be used to enumerate or count hidden users.
"""

import pytest
from sqlalchemy import select

from app.domain.auth.models import Role
from tests.api.test_sec_arch_01_role_assignment_scope import _assign
from tests.api.test_user_groups import _group, _group_user, _make_site

ADMIN_CODES = ["user:manage", "user:read", "group:manage", "group:read", "rack:read", "location:read"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def global_admin_in(client, admin, db_session, groups: list[str]) -> dict:
    user, _headers = await _group_user(client, admin, groups)
    role_id = (await db_session.execute(select(Role.id).where(Role.name == "Administrator"))).scalar_one()
    await _assign(db_session, user["id"], role_id, "global", None)
    return user


@pytest.fixture
async def world(client, admin, db_session):
    site = await _make_site(client, admin)
    scoped_group = await _group(client, admin, allow=ADMIN_CODES, sites=[{"site_id": site["site"], "rack_scope": "all"}])
    actor, actor_headers = await _group_user(client, admin, [scoped_group])
    member_group = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": site["site"], "rack_scope": "all"}])
    shared = await _group(client, admin, allow=["rack:read"])  # no site grants: visible to every group:read holder
    hidden = await global_admin_in(client, admin, db_session, [shared])
    visible, _ = await _group_user(client, admin, [shared, member_group])
    return {"scope_group": scoped_group, "actor": actor, "h": actor_headers, "shared": shared, "hidden": hidden, "visible": visible, "site": site}


async def detail(client, headers, gid):
    response = await client.get(f"/api/v1/groups/{gid}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def listed(client, headers, gid) -> dict:
    page = (await client.get("/api/v1/groups?limit=200", headers=headers)).json()
    (item,) = [g for g in page["items"] if g["id"] == gid]
    return item


async def test_the_precondition_hidden_user_is_invisible_in_the_user_directory(client, world):
    ids = {u["id"] for u in (await client.get("/api/v1/users?limit=200", headers=world["h"])).json()["items"]}
    assert world["hidden"]["id"] not in ids and world["visible"]["id"] in ids
    assert (await client.get(f"/api/v1/users/{world['hidden']['id']}", headers=world["h"])).status_code == 404


async def test_group_detail_masks_hidden_members_and_the_count(client, world):
    body = await detail(client, world["h"], world["shared"])
    assert world["hidden"]["id"] not in body["member_ids"]
    assert body["member_ids"] == [world["visible"]["id"]]
    assert body["member_count"] == 1, "the count must not reveal the hidden member"


async def test_group_list_count_matches_the_masked_detail(client, world):
    row = await listed(client, world["h"], world["shared"])
    assert row["member_count"] == 1
    assert row["member_count"] == len((await detail(client, world["h"], world["shared"]))["member_ids"])


async def test_a_group_with_only_hidden_members_reports_none(client, admin, db_session, world):
    only_hidden = await _group(client, admin)
    await global_admin_in(client, admin, db_session, [only_hidden])
    body = await detail(client, world["h"], only_hidden)
    assert body["member_ids"] == [] and body["member_count"] == 0
    assert (await listed(client, world["h"], only_hidden))["member_count"] == 0


async def test_every_returned_member_id_resolves_in_the_directory_and_the_group_filter_agrees(client, world):
    body = await detail(client, world["h"], world["shared"])
    for uid in body["member_ids"]:
        assert (await client.get(f"/api/v1/users/{uid}", headers=world["h"])).status_code == 200
    filtered = (await client.get(f"/api/v1/users?group_id={world['shared']}&limit=200", headers=world["h"])).json()
    assert {u["id"] for u in filtered["items"]} == set(body["member_ids"])
    assert filtered["total"] == body["member_count"]


async def test_the_caller_is_always_visible_to_themselves(client, admin):
    """Hidden here means 'a site-B user': unlike a global Administrator peer, the admin can legitimately place them in a group."""
    site_a, site_b = await _make_site(client, admin), await _make_site(client, admin)
    mixed = await _group(client, admin)
    scoped = await _group(client, admin, allow=ADMIN_CODES, sites=[{"site_id": site_a["site"], "rack_scope": "all"}])
    other = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": site_b["site"], "rack_scope": "all"}])
    actor, headers = await _group_user(client, admin, [scoped, mixed])
    in_b, _ = await _group_user(client, admin, [other, mixed])
    ids = {u["id"] for u in (await client.get("/api/v1/users?limit=200", headers=headers)).json()["items"]}
    assert in_b["id"] not in ids, "precondition: the site-B user is hidden from the caller"
    body = await detail(client, headers, mixed)
    assert body["member_ids"] == [actor["id"]]
    assert body["member_count"] == 1

async def test_a_caller_without_user_read_learns_no_member_ids_or_count(client, admin, world):
    gid = await _group(client, admin, allow=["group:read"], sites=[{"site_id": world["site"]["site"], "rack_scope": "all"}])
    _user, headers = await _group_user(client, admin, [gid])
    body = await detail(client, headers, world["shared"])
    assert body["member_ids"] == [] and body["member_count"] == 0
    assert (await listed(client, headers, world["shared"]))["member_count"] == 0


async def test_global_administrator_keeps_full_visibility(client, admin, world):
    body = await detail(client, admin, world["shared"])
    assert {world["hidden"]["id"], world["visible"]["id"]} <= set(body["member_ids"])
    assert body["member_count"] == len(body["member_ids"]) >= 2
    assert (await listed(client, admin, world["shared"]))["member_count"] == body["member_count"]


async def test_delegated_containment_is_unchanged_for_groups_with_hidden_members(client, world):
    """Masking is read-side only: a delegate still cannot modify a group that holds a member who outranks them."""
    denied = await client.put(f"/api/v1/groups/{world['shared']}/members", json={"user_ids": [world["visible"]["id"]]}, headers=world["h"])
    assert denied.status_code in (403, 404)
    again = await detail(client, world["h"], world["shared"])
    assert again["member_ids"] == [world["visible"]["id"]]
