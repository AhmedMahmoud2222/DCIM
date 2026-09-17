"""Shared fixture-building helpers for Phase 2 API tests — creates the location chain,
catalog rows, racks, and equipment a given test needs via the real HTTP API (not direct
ORM inserts), the same style as tests/api/test_locations.py's `_create_full_chain`."""

import uuid


async def create_room(client, auth_headers) -> str:
    """Builds the full location chain using a DCIM Manager token regardless of which
    role the calling test is actually exercising — location creation is prerequisite
    scaffolding, not the behavior under test (only Administrator/DCIM Manager hold
    organization:manage per app/application/rbac.py)."""
    headers = await auth_headers("DCIM Manager")
    org = (await client.post("/api/v1/organizations", json={"name": f"Org-{uuid.uuid4().hex[:8]}"}, headers=headers)).json()
    country = (
        await client.post(
            "/api/v1/countries", json={"organization_id": org["id"], "name": "Testland", "iso_code": "TL"}, headers=headers
        )
    ).json()
    city = (await client.post("/api/v1/cities", json={"country_id": country["id"], "name": "Testville"}, headers=headers)).json()
    site = (
        await client.post(
            "/api/v1/sites", json={"city_id": city["id"], "code": f"S-{uuid.uuid4().hex[:6]}", "name": "Test Site"}, headers=headers
        )
    ).json()
    building = (
        await client.post("/api/v1/buildings", json={"site_id": site["id"], "code": "A", "name": "Building A"}, headers=headers)
    ).json()
    floor = (
        await client.post("/api/v1/floors", json={"building_id": building["id"], "name": "Floor 1", "level_number": 1}, headers=headers)
    ).json()
    room = (
        await client.post(
            "/api/v1/rooms",
            json={"floor_id": floor["id"], "code": f"R-{uuid.uuid4().hex[:6]}", "name": "Test Room"},
            headers=headers,
        )
    ).json()
    return room["id"]


async def create_rack_model_revision(client, headers, *, height_u: int = 42) -> str:
    model = (
        await client.post(
            "/api/v1/rack-models", json={"manufacturer": "Acme", "model_name": f"RM-{uuid.uuid4().hex[:8]}"}, headers=headers
        )
    ).json()
    revision = (
        await client.post(
            f"/api/v1/rack-models/{model['id']}/revisions",
            json={"height_u": height_u, "width_mm": 600, "depth_mm": 1000},
            headers=headers,
        )
    ).json()
    return revision["id"]


async def create_equipment_model_revision(client, headers) -> str:
    model = (
        await client.post(
            "/api/v1/equipment-models", json={"manufacturer": "Acme", "model_name": f"EM-{uuid.uuid4().hex[:8]}"}, headers=headers
        )
    ).json()
    revision = (
        await client.post(f"/api/v1/equipment-models/{model['id']}/revisions", json={}, headers=headers)
    ).json()
    return revision["id"]


async def create_rack(client, headers, *, room_id: str | None = None, **extra) -> dict:
    revision_id = await create_rack_model_revision(client, headers)
    body = {"asset_tag": f"RACK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "name": "Test Rack"}
    if room_id is not None:
        body["room_id"] = room_id
    body.update(extra)
    resp = await client.post("/api/v1/racks", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_equipment(client, headers, **extra) -> dict:
    revision_id = await create_equipment_model_revision(client, headers)
    body = {"asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "hostname": "test-host"}
    body.update(extra)
    resp = await client.post("/api/v1/equipment", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()
