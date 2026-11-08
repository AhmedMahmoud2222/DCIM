import re
import uuid
from pathlib import Path

from app.api.pagination import MAX_LIMIT
from tests.api.test_user_groups import _group, _group_user, _make_site


async def _create_full_chain(client, headers) -> dict:
    org = (await client.post("/api/v1/organizations", json={"name": "Acme DC"}, headers=headers)).json()
    country = (
        await client.post(
            "/api/v1/countries", json={"organization_id": org["id"], "name": "UAE", "iso_code": "AE"}, headers=headers
        )
    ).json()
    city = (await client.post("/api/v1/cities", json={"country_id": country["id"], "name": "Dubai"}, headers=headers)).json()
    site = (
        await client.post(
            "/api/v1/sites",
            json={"city_id": city["id"], "code": "DXB-DC01", "name": "Dubai DC 01"},
            headers=headers,
        )
    ).json()
    building = (
        await client.post("/api/v1/buildings", json={"site_id": site["id"], "code": "A", "name": "Building A"}, headers=headers)
    ).json()
    floor = (
        await client.post("/api/v1/floors", json={"building_id": building["id"], "name": "Floor 2", "level_number": 2}, headers=headers)
    ).json()
    room = (
        await client.post("/api/v1/rooms", json={"floor_id": floor["id"], "code": "DH03", "name": "Data Hall 03"}, headers=headers)
    ).json()
    return {"organization": org, "country": country, "city": city, "site": site, "building": building, "floor": floor, "room": room}


async def test_full_location_chain_creation_as_dcim_manager(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    assert chain["room"]["code"] == "DH03"
    assert chain["room"]["version"] == 1


async def test_viewer_cannot_create_locations(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.post("/api/v1/organizations", json={"name": "Should Fail"}, headers=headers)
    assert resp.status_code == 403


async def test_viewer_can_read_locations(client, auth_headers):
    manager_headers = await auth_headers("DCIM Manager")
    await _create_full_chain(client, manager_headers)

    viewer_headers = await auth_headers("Viewer")
    resp = await client.get("/api/v1/organizations", headers=viewer_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


async def test_room_update_requires_if_match(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    room_id = chain["room"]["id"]

    resp = await client.patch(f"/api/v1/rooms/{room_id}", json={"name": "Renamed"}, headers=headers)
    assert resp.status_code == 428


async def test_room_update_with_correct_version_succeeds_and_increments_version(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    room_id = chain["room"]["id"]

    resp = await client.patch(
        f"/api/v1/rooms/{room_id}", json={"name": "Renamed"}, headers={**headers, "If-Match": "1"}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert resp.json()["name"] == "Renamed"


async def test_room_update_with_stale_version_is_rejected_with_409(client, auth_headers):
    """C4's concurrency foundation: a stale If-Match must not silently overwrite a
    newer change — the second of two concurrent updates loses cleanly."""
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    room_id = chain["room"]["id"]

    first = await client.patch(f"/api/v1/rooms/{room_id}", json={"name": "First Update"}, headers={**headers, "If-Match": "1"})
    assert first.status_code == 200
    assert first.json()["version"] == 2

    second = await client.patch(
        f"/api/v1/rooms/{room_id}", json={"name": "Stale Update"}, headers={**headers, "If-Match": "1"}
    )
    assert second.status_code == 409

    current = await client.get(f"/api/v1/rooms/{room_id}", headers=headers)
    assert current.json()["name"] == "First Update"


# ------------------------------------------------------------------ #148 UI-facing contract
# The location-hierarchy administration screen (frontend/src/features/locations/
# LocationsPage.tsx) drives exactly these endpoints. The four tests below pin the
# behaviors that UI renders: the positive Administrator path, and the three failure
# shapes it turns into actionable messages (missing parent → 404 detail, duplicate →
# 409 → "already exists", restricted user → 403).


async def test_administrator_can_create_the_full_location_chain(client, auth_headers):
    """Positive access for an authorized Administrator (not only DCIM Manager)."""
    headers = await auth_headers("Administrator")
    chain = await _create_full_chain(client, headers)
    assert chain["site"]["code"] == "DXB-DC01"
    assert chain["room"]["code"] == "DH03"


async def test_missing_parent_is_reported_precisely(client, auth_headers):
    """A vanished/mismatched parent answers 404 with its own precise detail, which the UI
    surfaces verbatim rather than a generic failure."""
    headers = await auth_headers("DCIM Manager")
    missing = str(uuid.uuid4())
    resp = await client.post("/api/v1/countries", json={"organization_id": missing, "name": "Nowhere"}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Organization {missing} not found."


async def test_duplicate_site_code_is_a_conflict(client, auth_headers):
    """A duplicate unique key (site code) answers 409, which the UI renders as an
    actionable "already exists" message."""
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)  # creates site code DXB-DC01
    dup = await client.post(
        "/api/v1/sites",
        json={"city_id": chain["city"]["id"], "code": "DXB-DC01", "name": "Duplicate"},
        headers=headers,
    )
    assert dup.status_code == 409


async def test_site_scoped_user_cannot_create_locations(client, auth_headers):
    """Cross-scope negative (fail closed): `location:manage` is not scope-aware, so it is
    inert for a site-restricted user — the write is refused with 403 and never widened to
    the user's own site."""
    admin = await auth_headers("Administrator")
    site = await _make_site(client, admin)
    gid = await _group(
        client,
        admin,
        allow=["location:read", "organization:read", "location:manage"],
        sites=[{"site_id": site["site"], "rack_scope": "all"}],
    )
    _user, headers = await _group_user(client, admin, [gid])

    resp = await client.post("/api/v1/organizations", json={"name": "Should Fail"}, headers=headers)
    assert resp.status_code == 403


# ------------------------------------------------------------------ #148 pagination contract
# The hierarchy screen lists every level with an explicit `limit`. The backend accepts
# `limit` at most MAX_LIMIT and answers 422 above it, so the client must stay <= that
# ceiling. These tests pin both ends of the contract: the endpoint behavior itself, and
# the frontend constant that produces the requests.


async def test_list_limit_at_backend_maximum_is_accepted(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    resp = await client.get(f"/api/v1/sites?city_id={chain['city']['id']}&limit={MAX_LIMIT}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["limit"] == MAX_LIMIT


async def test_list_limit_above_backend_maximum_is_rejected(client, auth_headers):
    """The regression this guards: a client that requests more than MAX_LIMIT (the UI once
    sent limit=500) gets a 422 and shows no hierarchy at all."""
    headers = await auth_headers("DCIM Manager")
    chain = await _create_full_chain(client, headers)
    resp = await client.get(f"/api/v1/sites?city_id={chain['city']['id']}&limit={MAX_LIMIT + 1}", headers=headers)
    assert resp.status_code == 422


def test_frontend_page_size_never_exceeds_backend_maximum():
    """Cross-language contract: the client's declared cap must equal the backend's
    MAX_LIMIT, so a limit the client would send can never be rejected as out of range."""
    api_ts = Path(__file__).resolve().parents[3] / "frontend" / "src" / "features" / "locations" / "api.ts"
    text = api_ts.read_text(encoding="utf-8")
    match = re.search(r"HIERARCHY_MAX_LIMIT\s*=\s*(\d+)", text)
    assert match, "HIERARCHY_MAX_LIMIT must be declared in the locations API client"
    assert int(match.group(1)) == MAX_LIMIT
    # The page size is what actually goes on the wire; it must not exceed the cap either.
    assert re.search(r"HIERARCHY_PAGE_SIZE\s*=\s*HIERARCHY_MAX_LIMIT", text), (
        "HIERARCHY_PAGE_SIZE must be derived from HIERARCHY_MAX_LIMIT"
    )
