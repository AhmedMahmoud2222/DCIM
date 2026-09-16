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
