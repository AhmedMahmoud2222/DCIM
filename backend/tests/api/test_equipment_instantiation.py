"""Phase 10B: POST /equipment/instantiate, GET /equipment/{id}/ports, and
POST /equipment/{id}/ports/connect, end to end over HTTP — matching this repo's
tests/api convention (test_catalog_designer_lifecycle.py, test_equipment.py)."""

import uuid

from tests.api._phase2_helpers import create_room


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_manufacturer(client, headers) -> str:
    resp = await client.post("/api/v1/catalog/manufacturers", json={"name": f"Acme-{uuid.uuid4().hex[:8]}"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_published_equipment_revision(
    client, headers, *, rack_unit_height: int = 2, port_count: int = 2, psu_quantity: int = 1
) -> dict:
    """A published, category='equipment' revision with `port_count` network ports and one
    power-supply template of `psu_quantity`, ready to instantiate."""
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "equipment", "model_name": f"Server-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    model_id = model_resp.json()["id"]

    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    assert draft_resp.status_code == 201, draft_resp.text
    revision = draft_resp.json()

    fields_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={
            "dimension_unit": "mm", "width_value": 440, "height_value": rack_unit_height * 44.45, "depth_value": 600,
            "weight_unit": "kg", "weight_value": 10, "rack_unit_height": rack_unit_height,
            "supported_placement_types": ["rack_mounted"],
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert fields_resp.status_code == 200, fields_resp.text
    revision = fields_resp.json()

    for i in range(port_count):
        port_resp = await client.post(
            f"/api/v1/catalog/revisions/{revision['id']}/network-ports",
            json={
                "stable_key": f"eth{i}", "display_name": f"eth{i}", "media_type": "copper",
                "supported_speeds_mbps": [1000], "connector_type": "rj45", "side": "rear",
            },
            headers={**headers, "If-Match": str(revision["version"])},
        )
        assert port_resp.status_code == 201, port_resp.text
        revision["version"] = port_resp.json()["revision_version"]

    if psu_quantity:
        psu_resp = await client.post(
            f"/api/v1/catalog/revisions/{revision['id']}/power-supplies",
            json={"stable_key": "psu1", "label": "PSU", "quantity": psu_quantity, "connector_type": "C14"},
            headers={**headers, "If-Match": str(revision["version"])},
        )
        assert psu_resp.status_code == 201, psu_resp.text
        revision["version"] = psu_resp.json()["revision_version"]

    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text
    return publish_resp.json()


async def test_instantiate_creates_ports_and_power_inlets_for_a_2u_server(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, rack_unit_height=2, port_count=2, psu_quantity=2)

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"], "hostname": "srv-1"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["catalog_model_revision_id"] == revision["id"]
    assert len(body["ports"]) == 2
    assert {p["stable_key"] for p in body["ports"]} == {"eth0", "eth1"}
    assert len(body["power_inlets"]) == 2

    ports_resp = await client.get(f"/api/v1/equipment/{body['id']}/ports", headers=headers)
    assert ports_resp.status_code == 200, ports_resp.text
    assert len(ports_resp.json()["ports"]) == 2
    assert all(p["connection"] is None for p in ports_resp.json()["ports"])


async def test_instantiate_into_a_rack_slot_in_one_request_and_elevation_reflects_it(client, auth_headers):
    headers = await _admin(auth_headers)
    engineer_headers = await auth_headers("Engineer")
    revision = await _make_published_equipment_revision(client, headers, rack_unit_height=2, port_count=1, psu_quantity=0)
    room_id = await create_room(client, auth_headers)

    from tests.api._phase2_helpers import create_rack_model_revision

    rack_revision_id = await create_rack_model_revision(client, auth_headers, height_u=10)
    rack_resp = await client.post(
        "/api/v1/racks",
        json={"asset_tag": f"RACK-{uuid.uuid4().hex[:8]}", "model_revision_id": rack_revision_id, "name": "R1", "room_id": room_id},
        headers=engineer_headers,
    )
    assert rack_resp.status_code == 201, rack_resp.text
    rack = rack_resp.json()

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={
            "asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"],
            "placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 3,
            "side": "front",
        },
        headers=engineer_headers,
    )
    assert resp.status_code == 201, resp.text
    equipment_id = resp.json()["id"]
    assert resp.json()["placement"]["rack_id"] == rack["id"]

    elevation_resp = await client.get(f"/api/v1/racks/{rack['id']}/elevation", headers=engineer_headers)
    assert elevation_resp.status_code == 200, elevation_resp.text
    [slot] = elevation_resp.json()["slots"]
    assert slot["equipment_id"] == equipment_id
    assert slot["catalog_model_revision_id"] == revision["id"]


async def test_instantiate_rejects_a_draft_revision(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "equipment", "model_name": f"Draft-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    model_id = model_resp.json()["id"]
    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    revision = draft_resp.json()

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-DRAFT-REJECT", "catalog_model_revision_id": revision["id"]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "draft" in resp.json()["detail"]


async def test_instantiate_rejects_a_rack_category_model(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "rack", "model_name": f"Rack-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    model_id = model_resp.json()["id"]
    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    revision = draft_resp.json()
    fields_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={"dimension_unit": "in", "width_value": 19, "height_value": 73.5, "depth_value": 39.4, "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220},
        headers={**headers, "If-Match": str(revision["version"])},
    )
    revision = fields_resp.json()
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-RACK-REJECT", "catalog_model_revision_id": publish_resp.json()["id"]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "category" in resp.json()["detail"]


async def test_operator_cannot_instantiate_or_connect_ports_but_engineer_can(client, auth_headers):
    admin_headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, admin_headers, port_count=1, psu_quantity=0)

    operator_headers = await auth_headers("Operator")
    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-OPERATOR-DENY", "catalog_model_revision_id": revision["id"]},
        headers=operator_headers,
    )
    assert resp.status_code == 403, resp.text

    engineer_headers = await auth_headers("Engineer")
    server_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=engineer_headers,
    )
    assert server_resp.status_code == 201, server_resp.text
    panel_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"PP-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=engineer_headers,
    )
    assert panel_resp.status_code == 201, panel_resp.text

    server_port_id = server_resp.json()["ports"][0]["id"]
    panel_port_id = panel_resp.json()["ports"][0]["id"]

    deny_connect = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect",
        json={"port_id": server_port_id, "target_port_id": panel_port_id},
        headers=operator_headers,
    )
    assert deny_connect.status_code == 403, deny_connect.text

    allow_connect = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect",
        json={"port_id": server_port_id, "target_port_id": panel_port_id, "cable_id": "CBL-100"},
        headers=engineer_headers,
    )
    assert allow_connect.status_code == 201, allow_connect.text
    assert allow_connect.json()["target_port_id"] == panel_port_id
    assert allow_connect.json()["cable_id"] == "CBL-100"

    ports_resp = await client.get(f"/api/v1/equipment/{server_resp.json()['id']}/ports", headers=operator_headers)
    assert ports_resp.status_code == 200, ports_resp.text
    [connected_port] = ports_resp.json()["ports"]
    assert connected_port["connection"]["target_port_id"] == panel_port_id
    assert connected_port["connection"]["status"] == "active"


async def test_connect_port_requires_exactly_one_target(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1, psu_quantity=0)
    server_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=headers,
    )
    port_id = server_resp.json()["ports"][0]["id"]

    resp = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect", json={"port_id": port_id}, headers=headers
    )
    assert resp.status_code == 422, resp.text
