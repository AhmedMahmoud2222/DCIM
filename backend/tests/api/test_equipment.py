import uuid

from tests.api._phase2_helpers import create_equipment, create_rack, create_room


async def test_create_equipment_without_placement(client, auth_headers):
    headers = await auth_headers("Engineer")
    eq = await create_equipment(client, headers)
    assert eq["placement"] is None


async def test_duplicate_idempotency_key_for_equipment_create_replays_the_same_result(client, auth_headers):
    headers = await auth_headers("Engineer")
    tag = f"EQ-IDEM-{uuid.uuid4().hex[:8]}"
    eq = await create_equipment(client, headers, asset_tag=tag)
    revision_id = eq["model_revision_id"]
    key = str(uuid.uuid4())
    body = {"asset_tag": f"EQ-IDEM2-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "hostname": "idem-host"}

    first = await client.post("/api/v1/equipment", json=body, headers={**headers, "Idempotency-Key": key})
    second = await client.post("/api/v1/equipment", json=body, headers={**headers, "Idempotency-Key": key})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


async def test_viewer_cannot_move_or_retire_equipment(client, auth_headers):
    engineer_headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, engineer_headers)

    viewer_headers = await auth_headers("Viewer")
    move_attempt = await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={"placement_type": "floor_standing", "room_id": room_id},
        headers=viewer_headers,
    )
    assert move_attempt.status_code == 403

    retire_attempt = await client.post(f"/api/v1/equipment/{eq['id']}/retire", headers=viewer_headers)
    assert retire_attempt.status_code == 403


async def test_move_equipment_floor_standing_requires_no_rack(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers)

    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["placement"]["placement_type"] == "floor_standing"
    assert resp.json()["placement"]["rack_id"] is None


async def test_move_equipment_rack_mounted_requires_rack_u_range_and_side(client, auth_headers):
    """Client-side mirror of the v1.3 F1 CHECK constraint (rack_mounted_requires_rack_u_range_and_side)
    — must reject with a clean 422 before ever reaching the database."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers)

    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move", json={"placement_type": "rack_mounted", "room_id": room_id}, headers=headers
    )
    assert resp.status_code == 422


async def test_move_equipment_rejects_invalid_side(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq = await create_equipment(client, headers)

    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={
            "placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 2,
            "side": "sideways",
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_move_equipment_rejects_invalid_placement_type(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers)
    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move", json={"placement_type": "orbiting", "room_id": room_id}, headers=headers
    )
    assert resp.status_code == 422


async def test_same_side_overlap_in_same_rack_is_rejected(client, auth_headers):
    """§7a's GiST exclusion constraint (no_front_overlap), surfaced through the API as a
    clean conflict rather than a raw 500."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq_1 = await create_equipment(client, headers)
    eq_2 = await create_equipment(client, headers)

    first = await client.post(
        f"/api/v1/equipment/{eq_1['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 4, "side": "front"},
        headers=headers,
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/equipment/{eq_2['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 2, "u_end": 5, "side": "front"},
        headers=headers,
    )
    # The exclusion constraint fires as a raw IntegrityError, mapped by the global
    # handler (app/core/errors.py) to a clean 409 rather than a raw 500.
    assert second.status_code == 409


async def test_opposite_sides_same_u_range_both_succeed(client, auth_headers):
    """Front+rear coexistence at the identical U-range must succeed — this is the whole
    point of the front/rear-scoped partial exclusion constraints."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq_front = await create_equipment(client, headers)
    eq_rear = await create_equipment(client, headers)

    front = await client.post(
        f"/api/v1/equipment/{eq_front['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 4, "side": "front"},
        headers=headers,
    )
    rear = await client.post(
        f"/api/v1/equipment/{eq_rear['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 4, "side": "rear"},
        headers=headers,
    )
    assert front.status_code == 200
    assert rear.status_code == 200


async def test_adjacent_non_overlapping_u_ranges_both_succeed(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq_a = await create_equipment(client, headers)
    eq_b = await create_equipment(client, headers)

    a = await client.post(
        f"/api/v1/equipment/{eq_a['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 4, "side": "front"},
        headers=headers,
    )
    b = await client.post(
        f"/api/v1/equipment/{eq_b['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 4, "u_end": 6, "side": "front"},
        headers=headers,
    )
    assert a.status_code == 200
    assert b.status_code == 200


async def test_equipment_retire_then_retire_again_is_idempotent(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, headers)
    await client.post(
        f"/api/v1/equipment/{eq['id']}/move", json={"placement_type": "floor_standing", "room_id": room_id}, headers=headers
    )

    first = await client.post(f"/api/v1/equipment/{eq['id']}/retire", headers=headers)
    second = await client.post(f"/api/v1/equipment/{eq['id']}/retire", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200


async def test_list_equipment_filtered_by_rack(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    other_rack = await create_rack(client, headers, room_id=room_id)
    mounted = await create_equipment(client, headers)
    elsewhere = await create_equipment(client, headers)

    await client.post(
        f"/api/v1/equipment/{mounted['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 2, "side": "front"},
        headers=headers,
    )
    await client.post(
        f"/api/v1/equipment/{elsewhere['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": other_rack["id"], "u_start": 1, "u_end": 2, "side": "front"},
        headers=headers,
    )

    resp = await client.get("/api/v1/equipment", params={"rack_id": rack["id"]}, headers=headers)
    ids = [item["id"] for item in resp.json()["items"]]
    assert mounted["id"] in ids
    assert elsewhere["id"] not in ids


async def test_create_equipment_with_nonexistent_model_revision_is_404(client, auth_headers):
    headers = await auth_headers("Engineer")
    resp = await client.post(
        "/api/v1/equipment", json={"asset_tag": "EQ-GHOST", "model_revision_id": str(uuid.uuid4())}, headers=headers
    )
    assert resp.status_code == 404
