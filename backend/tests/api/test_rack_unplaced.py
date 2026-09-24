"""GET /racks/unplaced — the authoritative, paginated, organization-wide source for
racks with no active placement anywhere. Exists because deriving "unplaced" by loading
GET /racks?limit=200 and diffing client-side silently truncates once inventory exceeds
that page size."""

import uuid

from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Rack
from tests.api._phase2_helpers import create_rack, create_rack_model_revision, create_room


async def test_unplaced_racks_returns_genuinely_unplaced_only(client, auth_headers):
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)

    unplaced = await create_rack(client, headers, auth_headers)
    placed = await create_rack(client, headers, auth_headers, room_id=room_id, x_mm=10, y_mm=20)
    retired = await create_rack(client, headers, auth_headers, room_id=room_id, x_mm=5, y_mm=5)
    retire_resp = await client.post(f"/api/v1/racks/{retired['id']}/retire", headers=headers)
    assert retire_resp.status_code == 200
    assert retire_resp.json()["placement"] is None

    resp = await client.get("/api/v1/racks/unplaced", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body["items"]}

    assert unplaced["id"] in ids
    assert retired["id"] in ids  # retired/historical-only placement is unplaced now
    assert placed["id"] not in ids  # has an active placement — never unplaced
    for item in body["items"]:
        assert item["placement"] is None


async def test_unplaced_racks_excludes_coordinate_incomplete_active_placement(client, auth_headers):
    """A rack placed in a room but missing x_mm/y_mm still has a valid, active
    RackPlacement row — it is placed, not unplaced. Conflating "no coordinates" with
    "no placement" is exactly the bug this endpoint must not reproduce."""
    headers = await auth_headers("Engineer")
    room_id = await create_room(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers)

    move = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id}, headers=headers)
    assert move.status_code == 200
    assert move.json()["placement"]["x_mm"] is None

    resp = await client.get("/api/v1/racks/unplaced", headers=headers)
    ids = {item["id"] for item in resp.json()["items"]}
    assert rack["id"] not in ids


async def test_unplaced_racks_deterministic_ordering(client, auth_headers):
    headers = await auth_headers("Engineer")
    revision_id = await create_rack_model_revision(client, auth_headers)
    names = [f"Z-Unplaced-{uuid.uuid4().hex[:6]}", f"A-Unplaced-{uuid.uuid4().hex[:6]}", f"M-Unplaced-{uuid.uuid4().hex[:6]}"]
    created_ids = set()
    for name in names:
        rack = await create_rack(client, headers, auth_headers, model_revision_id=revision_id, name=name)
        created_ids.add(rack["id"])

    first = await client.get("/api/v1/racks/unplaced?limit=200", headers=headers)
    second = await client.get("/api/v1/racks/unplaced?limit=200", headers=headers)
    first_order = [item["id"] for item in first.json()["items"] if item["id"] in created_ids]
    second_order = [item["id"] for item in second.json()["items"] if item["id"] in created_ids]
    assert first_order == second_order
    # Ordered by name — the three names sort A, M, Z regardless of creation order.
    our_names_in_order = [item["name"] for item in first.json()["items"] if item["id"] in created_ids]
    assert our_names_in_order == sorted(our_names_in_order)


async def test_unplaced_racks_pagination_beyond_200_reports_total_and_no_duplicates(client, auth_headers, db_session):
    """Directly seeds 205 unplaced racks (bypassing the HTTP create-per-rack round trip
    for test speed) to prove the endpoint's pagination and total count hold beyond the
    old GET /racks?limit=200 ceiling that the client-side approach silently truncated at."""
    headers = await auth_headers("Engineer")
    revision_id = await create_rack_model_revision(client, auth_headers)

    seeded_ids = []
    for index in range(205):
        asset = ManagedAsset(asset_type="rack", asset_tag=f"BULK-UNPLACED-{uuid.uuid4().hex[:10]}", lifecycle_status="planned")
        db_session.add(asset)
        await db_session.flush()
        db_session.add(Rack(id=asset.id, model_revision_id=uuid.UUID(revision_id), name=f"Bulk Rack {index:04d}-{uuid.uuid4().hex[:6]}"))
        seeded_ids.append(str(asset.id))
    await db_session.commit()

    baseline_total = (await client.get("/api/v1/racks/unplaced?limit=1", headers=headers)).json()["total"]
    assert baseline_total >= 205

    page1 = await client.get("/api/v1/racks/unplaced?limit=200&offset=0", headers=headers)
    page2 = await client.get("/api/v1/racks/unplaced?limit=200&offset=200", headers=headers)
    assert page1.status_code == 200 and page2.status_code == 200
    assert page1.json()["total"] == baseline_total
    assert page2.json()["total"] == baseline_total
    assert len(page1.json()["items"]) == 200
    assert len(page2.json()["items"]) >= 5

    page1_ids = [item["id"] for item in page1.json()["items"]]
    page2_ids = [item["id"] for item in page2.json()["items"]]
    assert len(set(page1_ids) & set(page2_ids)) == 0  # no overlap across pages
    seeded_returned = (set(page1_ids) | set(page2_ids)) & set(seeded_ids)
    assert seeded_returned == set(seeded_ids)  # every seeded rack is reachable via pagination


async def test_unplaced_racks_requires_authentication(client):
    resp = await client.get("/api/v1/racks/unplaced")
    assert resp.status_code == 401


async def test_viewer_can_read_unplaced_racks(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.get("/api/v1/racks/unplaced", headers=headers)
    assert resp.status_code == 200


async def test_unplaced_racks_route_is_not_swallowed_by_the_rack_id_path_parameter(client, auth_headers):
    """Regression guard: /racks/unplaced must be registered before /racks/{rack_id}, or
    "unplaced" would be parsed as an invalid UUID path parameter instead of matching this
    route."""
    headers = await auth_headers("Viewer")
    resp = await client.get("/api/v1/racks/unplaced", headers=headers)
    assert resp.status_code == 200
    assert "items" in resp.json() and "total" in resp.json()


async def test_unplaced_racks_response_matches_rack_schema(client, auth_headers):
    headers = await auth_headers("Engineer")
    rack = await create_rack(client, headers, auth_headers)
    resp = await client.get("/api/v1/racks/unplaced", headers=headers)
    match = next(item for item in resp.json()["items"] if item["id"] == rack["id"])
    assert set(match.keys()) == {
        "id", "asset_tag", "lifecycle_status", "model_revision_id", "name", "owner", "notes", "version",
        "created_at", "placement",
    }
