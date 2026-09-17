"""Shared fixture-building helpers for Phase 3 power API tests — mirrors
tests/api/_phase2_helpers.py's style."""

import uuid


async def create_room_and_site(client, auth_headers) -> tuple[str, str]:
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
            "/api/v1/rooms", json={"floor_id": floor["id"], "code": f"R-{uuid.uuid4().hex[:6]}", "name": "Test Room"}, headers=headers,
        )
    ).json()
    return room["id"], site["id"]


async def create_utility(client, headers, label="Utility") -> dict:
    resp = await client.post("/api/v1/power/utility-intakes", json={"label": label}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_pdu(client, headers, tag=None) -> dict:
    resp = await client.post(
        "/api/v1/power/pdus", json={"asset_tag": tag or f"PDU-{uuid.uuid4().hex[:8]}", "name": "Test PDU"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_ups(client, headers, room_id, tag=None) -> dict:
    resp = await client.post(
        "/api/v1/power/upses",
        json={"asset_tag": tag or f"UPS-{uuid.uuid4().hex[:8]}", "name": "Test UPS", "room_id": room_id, "capacity_kva": 100},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def connection_body(source_id, target_id, feed_label="single", **extra) -> dict:
    body = {
        "source_node_id": source_id, "target_node_id": target_id, "connection_type": "feed", "feed_label": feed_label,
        "status": "active",
    }
    body.update(extra)
    return body
