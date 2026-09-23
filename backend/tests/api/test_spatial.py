import uuid

from tests.api._phase2_helpers import create_equipment, create_rack, create_rack_model_revision, create_room


async def test_room_spatial_view_includes_placed_racks_and_floor_standing_equipment(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room_id, x_mm=10, y_mm=20)
    floor_eq = await create_equipment(client, headers, auth_headers)
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


async def test_room_spatial_view_separates_rack_mounted_equipment_from_2d_equipment(client, auth_headers):
    """The 2D layer remains free of rack-mounted equipment, while the same authoritative
    placement facts are supplied separately for the interactive 3D projection."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room_id)
    mounted_eq = await create_equipment(client, headers, auth_headers)
    await client.post(
        f"/api/v1/equipment/{mounted_eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"},
        headers=headers,
    )

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    assert resp.json()["equipment"] == []
    assert resp.json()["rack_equipment"] == [{
        "id": mounted_eq["id"], "asset_tag": mounted_eq["asset_tag"], "hostname": mounted_eq["hostname"],
        "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front",
    }]


async def test_room_spatial_view_reports_authoritative_rack_capacity(client, auth_headers):
    """height_u comes from the rack's own model revision (rack capacity), not a
    frontend-fabricated constant — a 3D/elevation renderer needs this to size a rack's
    occupiable U range from real inventory data."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    revision_id = await create_rack_model_revision(client, auth_headers, height_u=30)
    rack = await create_rack(client, headers, auth_headers, room_id=room_id, model_revision_id=revision_id)

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    assert resp.json()["racks"][0]["height_u"] == 30
    assert resp.json()["racks"][0]["id"] == rack["id"]


async def test_room_spatial_view_returns_every_placed_equipment_item_with_authoritative_u_and_side(client, auth_headers):
    """Every rack-mounted item comes back — position is not capped or truncated — and
    each carries its own authoritative u_start/u_end/side, including boundary U values
    (the first and last U of the rack)."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    revision_id = await create_rack_model_revision(client, auth_headers, height_u=12)
    rack = await create_rack(client, headers, auth_headers, room_id=room_id, model_revision_id=revision_id)

    placements = [
        (1, 2, "front"),  # lower boundary
        (3, 4, "rear"),
        (5, 6, "front"),
        (7, 8, "rear"),
        (9, 10, "both"),
        (11, 12, "front"),  # upper boundary
        (11, 12, "rear"),  # same U range, opposite side — legitimate, not a collision
    ]
    created_ids = []
    for u_start, u_end, side in placements:
        eq = await create_equipment(client, headers, auth_headers)
        created_ids.append(eq["id"])
        move = await client.post(
            f"/api/v1/equipment/{eq['id']}/move",
            json={
                "placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"],
                "u_start": u_start, "u_end": u_end, "side": side,
            },
            headers=headers,
        )
        assert move.status_code == 200, move.text

    resp = await client.get(f"/api/v1/spatial/rooms/{room_id}/view", headers=headers)
    rack_equipment = resp.json()["rack_equipment"]
    assert len(rack_equipment) == len(placements)
    assert {item["id"] for item in rack_equipment} == set(created_ids)
    by_id = {item["id"]: item for item in rack_equipment}
    for equipment_id, (u_start, u_end, side) in zip(created_ids, placements, strict=True):
        assert by_id[equipment_id]["u_start"] == u_start
        assert by_id[equipment_id]["u_end"] == u_end
        assert by_id[equipment_id]["side"] == side


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
