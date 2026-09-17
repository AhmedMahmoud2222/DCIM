import uuid

from tests.api._phase2_helpers import create_equipment, create_rack, create_rack_model_revision, create_room


async def test_create_rack_without_placement(client, auth_headers):
    headers = await auth_headers("Engineer")
    rack = await create_rack(client, headers)
    assert rack["placement"] is None
    assert rack["version"] == 1


async def test_duplicate_idempotency_key_for_rack_create_replays_the_same_result(client, auth_headers):
    """Mirrors the Phase 1 managed-assets idempotency test — Phase 2 reuses the exact
    same mechanism (app.application.idempotency), never a second one."""
    headers = await auth_headers("Engineer")
    revision_id = await create_rack_model_revision(client, headers)
    key = str(uuid.uuid4())
    body = {"asset_tag": f"RACK-IDEM-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "name": "Idempotent Rack"}

    first = await client.post("/api/v1/racks", json=body, headers={**headers, "Idempotency-Key": key})
    second = await client.post("/api/v1/racks", json=body, headers={**headers, "Idempotency-Key": key})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listing = await client.get("/api/v1/racks", headers=headers)
    matching = [r for r in listing.json()["items"] if r["asset_tag"] == body["asset_tag"]]
    assert len(matching) == 1


async def test_viewer_cannot_move_or_retire_a_rack(client, auth_headers):
    engineer_headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, engineer_headers, room_id=room_id)

    viewer_headers = await auth_headers("Viewer")
    move_attempt = await client.post(
        f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id}, headers=viewer_headers
    )
    assert move_attempt.status_code == 403

    retire_attempt = await client.post(f"/api/v1/racks/{rack['id']}/retire", headers=viewer_headers)
    assert retire_attempt.status_code == 403


async def test_create_rack_with_initial_placement(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id, x_mm=100, y_mm=200, rotation_deg=90)
    assert rack["placement"]["room_id"] == room_id
    assert rack["placement"]["x_mm"] == 100
    assert rack["placement"]["rotation_deg"] == 90


async def test_create_rack_rejects_out_of_range_coordinate(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    revision_id = await create_rack_model_revision(client, headers)
    resp = await client.post(
        "/api/v1/racks",
        json={
            "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "name": "Bad Rack",
            "room_id": room_id, "x_mm": 999_999_999,
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_create_rack_rejects_invalid_rotation(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    revision_id = await create_rack_model_revision(client, headers)
    resp = await client.post(
        "/api/v1/racks",
        json={
            "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision_id, "name": "Bad Rack",
            "room_id": room_id, "rotation_deg": 360,
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_create_rack_with_nonexistent_model_revision_is_404(client, auth_headers):
    headers = await auth_headers("Engineer")
    resp = await client.post(
        "/api/v1/racks",
        json={"asset_tag": "RACK-X", "model_revision_id": str(uuid.uuid4()), "name": "Ghost Rack"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_viewer_cannot_create_rack(client, auth_headers):
    headers = await auth_headers("Viewer")
    revision_id_headers = await auth_headers("Engineer")
    revision_id = await create_rack_model_revision(client, revision_id_headers)
    resp = await client.post(
        "/api/v1/racks", json={"asset_tag": "RACK-VIEW", "model_revision_id": revision_id, "name": "X"}, headers=headers
    )
    assert resp.status_code == 403


async def test_operator_can_move_rack_but_not_create(client, auth_headers):
    engineer_headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, engineer_headers)

    operator_headers = await auth_headers("Operator")
    create_attempt = await client.post(
        "/api/v1/racks",
        json={"asset_tag": "RACK-OP", "model_revision_id": rack["model_revision_id"], "name": "Op Rack"},
        headers=operator_headers,
    )
    assert create_attempt.status_code == 403

    move_attempt = await client.post(
        f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id, "x_mm": 0, "y_mm": 0}, headers=operator_headers
    )
    assert move_attempt.status_code == 200


async def test_rack_move_then_move_again_creates_new_current_placement(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_a = await create_room(client, auth_headers)
    room_b = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_a)

    resp = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_b, "x_mm": 5, "y_mm": 5}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["placement"]["room_id"] == room_b


async def test_rack_move_with_wrong_if_match_version_is_409(client, auth_headers):
    """§7c's locked close-then-open transaction checks If-Match against the CURRENT
    placement row's own version — which resets to 1 on every new placement, so a stale
    value from an earlier placement can't be reproduced by simply replaying "1" twice.
    What must genuinely be rejected is an If-Match that does not match the placement
    actually current right now (a client acting on a version it never actually saw)."""
    headers = await auth_headers("Engineer")
    room_a = await create_room(client, auth_headers)
    room_b = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_a)

    resp = await client.post(
        f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_b}, headers={**headers, "If-Match": "2"}
    )
    assert resp.status_code == 409

    unchanged = await client.get(f"/api/v1/racks/{rack['id']}", headers=headers)
    assert unchanged.json()["placement"]["room_id"] == room_a


async def test_rack_move_concurrent_movers_only_one_wins_the_other_gets_409(client, auth_headers):
    """A genuine two-session race for the same current placement (ARCHITECTURE_REVIEW.md
    §7c's "zero rows after unblock" case): both requests target the same current
    placement with no If-Match at all, so exactly one must close it and open the new row,
    and the loser must observe it already gone rather than silently double-writing."""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("Engineer")
    room_a = await create_room(client, auth_headers)
    room_b = await create_room(client, auth_headers)
    room_c = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_a)

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async def _move(room_id: str):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                return await ac.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id}, headers=headers)

        results = await asyncio.gather(_move(room_b), _move(room_c))
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], f"expected exactly one winner and one conflict, got {[r.status_code for r in results]}"


async def test_rack_retire_then_retire_again_is_idempotent_not_409(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)

    first = await client.post(f"/api/v1/racks/{rack['id']}/retire", headers=headers)
    assert first.status_code == 200
    assert first.json()["placement"] is None

    second = await client.post(f"/api/v1/racks/{rack['id']}/retire", headers=headers)
    assert second.status_code == 200
    assert second.json()["placement"] is None


async def test_rack_update_requires_if_match(client, auth_headers):
    headers = await auth_headers("Engineer")
    rack = await create_rack(client, headers)
    resp = await client.patch(f"/api/v1/racks/{rack['id']}", json={"name": "Renamed"}, headers=headers)
    assert resp.status_code == 428


async def test_rack_update_with_stale_version_is_409(client, auth_headers):
    headers = await auth_headers("Engineer")
    rack = await create_rack(client, headers)
    first = await client.patch(f"/api/v1/racks/{rack['id']}", json={"name": "First"}, headers={**headers, "If-Match": "1"})
    assert first.status_code == 200
    second = await client.patch(f"/api/v1/racks/{rack['id']}", json={"name": "Second"}, headers={**headers, "If-Match": "1"})
    assert second.status_code == 409


async def test_rack_elevation_reflects_mounted_equipment_sorted_by_u_position(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)

    eq_top = await create_equipment(client, headers)
    eq_bottom = await create_equipment(client, headers)

    await client.post(
        f"/api/v1/equipment/{eq_top['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 10, "u_end": 12, "side": "front"},
        headers=headers,
    )
    await client.post(
        f"/api/v1/equipment/{eq_bottom['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 3, "side": "front"},
        headers=headers,
    )

    resp = await client.get(f"/api/v1/racks/{rack['id']}/elevation", headers=headers)
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert len(slots) == 2
    assert slots[0]["u_start"] == 1
    assert slots[1]["u_start"] == 10


async def test_rack_elevation_excludes_retired_equipment(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq = await create_equipment(client, headers)

    await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 3, "side": "front"},
        headers=headers,
    )
    await client.post(f"/api/v1/equipment/{eq['id']}/retire", headers=headers)

    resp = await client.get(f"/api/v1/racks/{rack['id']}/elevation", headers=headers)
    assert resp.json()["slots"] == []


async def test_equipment_move_rejects_u_range_exceeding_rack_capacity(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, room_id=room_id)
    eq = await create_equipment(client, headers)

    resp = await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 40, "u_end": 45, "side": "front"},
        headers=headers,
    )
    assert resp.status_code == 422
