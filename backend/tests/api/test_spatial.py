import uuid

from tests.api._phase2_helpers import create_equipment, create_rack, create_room


async def test_room_spatial_view_includes_placed_racks_and_floor_standing_equipment(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id, x_mm=10, y_mm=20)
    floor_eq = await create_equipment(client, headers)
    await client.post(
        f"/api/v1/equipment/{floor_eq['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id}, headers=headers
    )

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["racks"]) == 1
    assert body["racks"][0]["id"] == rack["id"]
    assert len(body["equipment"]) == 1
    assert body["equipment"][0]["id"] == floor_eq["id"]
    assert body["active_floor_plan_id"] is None


async def test_room_spatial_view_excludes_rack_mounted_equipment_from_the_equipment_list(client, auth_headers):
    """Rack-mounted equipment belongs to the rack's elevation view (§8), not the 2D room
    view — it must not appear twice across the two projections."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    mounted_eq = await create_equipment(client, headers)
    await client.post(
        f"/api/v1/equipment/{mounted_eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"},
        headers=headers,
    )

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    assert resp.json()["equipment"] == []


async def test_room_spatial_view_reports_the_active_floor_plan(client, auth_headers):
    headers = await auth_headers("DCIM Manager")  # floor_plan:manage — Engineer cannot create/activate floor plans
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    await client.post(f"/api/v1/floor-plans/{floor_plan['id']}/activate", headers={**headers, "If-Match": "1"})

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    assert resp.json()["active_floor_plan_id"] == floor_plan["id"]


async def test_room_spatial_view_for_nonexistent_room_is_404(client, auth_headers):
    headers = await auth_headers("Engineer")
    resp = await client.get(f"/api/v1/spatial/rooms/{uuid.uuid4()}/view", headers=headers)
    assert resp.status_code == 404


async def test_viewer_can_read_spatial_view(client, auth_headers):
    room_id = await create_room(client, auth_headers)

    viewer_headers = await auth_headers("Viewer")
    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=viewer_headers)
    assert resp.status_code == 200
