"""Missing and hidden principal IDs must be indistinguishable on writes."""

import uuid

import pytest

from tests.api.test_pr67_strict_outranking import BASE, USER_ROUTES, _entry, _principal
from tests.api.test_user_groups import PW, _group, _make_site


@pytest.fixture
async def principals(client, auth_headers):
    admin = await auth_headers("Administrator")
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    actor = await _principal(client, admin, BASE, [_entry(a["site"])])
    lower = await _principal(client, admin, ["rack:read"], [_entry(a["site"])])
    hidden = await _principal(client, admin, ["equipment:read"], [_entry(b["site"])])
    forbidden = await _group(client, admin, allow=["equipment:read"], sites=[_entry(a["site"])])
    empty = await _group(client, admin, allow=["rack:read"], sites=[_entry(a["site"])])
    return admin, actor, lower, hidden, forbidden, empty


def _same_missing(hidden, missing):
    assert hidden.status_code == missing.status_code == 404, (hidden.text, missing.text)
    keys = ("type", "title", "status", "detail")
    assert {k: hidden.json()[k] for k in keys} == {k: missing.json()[k] for k in keys}
    assert hidden.json()["detail"] == "The requested resource was not found."


@pytest.mark.parametrize("route", sorted(USER_ROUTES))
async def test_hidden_user_write_matches_missing_and_preserves_state(client, principals, route):
    admin, actor, _, hidden, _, _ = principals
    before = (await client.get(f"/api/v1/users/{hidden['id']}", headers=admin)).json()
    response = await USER_ROUTES[route](client, actor["headers"], hidden["id"], hidden["group"])
    absent = await USER_ROUTES[route](client, actor["headers"], str(uuid.uuid4()), hidden["group"])
    _same_missing(response, absent)
    assert (await client.get(f"/api/v1/users/{hidden['id']}", headers=admin)).json() == before
    assert (await client.post("/api/v1/auth/login", json={"email": hidden["email"], "password": PW})).status_code == 200


@pytest.mark.parametrize("mixed", [False, True])
async def test_membership_batch_masks_hidden_user_before_peer_comparison(client, principals, mixed):
    admin, actor, _, hidden, _, empty = principals
    prefix = [actor["id"]] if mixed else []
    # Use a visible peer, not self, to keep the self-membership guard independent.
    if mixed:
        site = (await client.get(f"/api/v1/groups/{actor['group']}", headers=admin)).json()["sites"][0]["site_id"]
        peer = await _principal(client, admin, BASE, [_entry(site)])
        prefix = [peer["id"]]
    responses = []
    for uid in (hidden["id"], str(uuid.uuid4())):
        responses.append(await client.put(f"/api/v1/groups/{empty}/members", json={"user_ids": [*prefix, uid]}, headers=actor["headers"]))
    _same_missing(*responses)
    assert (await client.get(f"/api/v1/groups/{empty}", headers=admin)).json()["member_ids"] == []


@pytest.mark.parametrize("surface", ["create", "update"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_group_assignment_masks_hidden_grants_and_rolls_back(client, principals, surface, mixed):
    admin, actor, lower, hidden, forbidden, _ = principals
    before = (await client.get(f"/api/v1/users/{lower['id']}", headers=admin)).json()
    email = f"blocked-{uuid.uuid4().hex}@example.com"
    responses = []
    for gid in (hidden["group"], str(uuid.uuid4())):
        ids = [forbidden, gid] if mixed else [gid]
        if surface == "create":
            response = await client.post("/api/v1/users", json={"email": email, "full_name": "Blocked", "password": PW, "group_ids": ids}, headers=actor["headers"])
        else:
            response = await client.patch(f"/api/v1/users/{lower['id']}", json={"full_name": "Blocked", "password": "blocked-password-123", "group_ids": ids}, headers=actor["headers"])
        responses.append(response)
    _same_missing(*responses)
    assert (await client.get(f"/api/v1/users/{lower['id']}", headers=admin)).json() == before
    assert (await client.post("/api/v1/auth/login", json={"email": lower["email"], "password": PW})).status_code == 200
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": PW})).status_code == 401
