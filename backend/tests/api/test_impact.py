"""Phase 10C: POST /impact/simulate end to end over HTTP — matching this repo's
tests/api convention (test_equipment_instantiation.py, test_power.py)."""

import uuid


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_published_equipment_revision(client, headers, *, port_count: int = 0, psu_quantity: int = 0) -> dict:
    manufacturer_resp = await client.post(
        "/api/v1/catalog/manufacturers", json={"name": f"Acme-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert manufacturer_resp.status_code == 201, manufacturer_resp.text
    manufacturer_id = manufacturer_resp.json()["id"]

    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "equipment", "model_name": f"Model-{uuid.uuid4().hex[:8]}"},
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
            "dimension_unit": "mm", "width_value": 440, "height_value": 44.45, "depth_value": 600,
            "weight_unit": "kg", "weight_value": 10, "rack_unit_height": 1, "supported_placement_types": ["rack_mounted"],
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


async def _instantiate(client, headers, revision_id: str) -> dict:
    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision_id, "hostname": f"host-{uuid.uuid4().hex[:6]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_simulate_power_node_failure_single_corded_full_outage(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=0, psu_quantity=1)
    server = await _instantiate(client, headers, revision["id"])
    inlet = server["power_inlets"][0]

    pdu_resp = await client.post(
        "/api/v1/power/pdus", json={"asset_tag": f"PDU-{uuid.uuid4().hex[:8]}", "name": "PDU-1"}, headers=headers
    )
    assert pdu_resp.status_code == 201, pdu_resp.text
    pdu_asset_id = pdu_resp.json()["managed_asset_id"]

    outlet_resp = await client.post(
        "/api/v1/power/pdu-outlets", json={"pdu_asset_id": pdu_asset_id, "outlet_number": 1}, headers=headers
    )
    assert outlet_resp.status_code == 201, outlet_resp.text
    outlet_id = outlet_resp.json()["id"]

    conn_resp = await client.post(
        "/api/v1/power/connections",
        json={"source_node_id": outlet_id, "target_node_id": inlet["power_node_id"], "feed_label": "single"},
        headers=headers,
    )
    assert conn_resp.status_code == 201, conn_resp.text

    resp = await client.post(
        "/api/v1/impact/simulate", json={"target_type": "power_node", "target_id": outlet_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_type"] == "power_node"
    assert len(body["directly_impacted"]) == 1
    assert body["directly_impacted"][0]["equipment_id"] == server["id"]
    assert body["directly_impacted"][0]["impact_type"] == "power_loss"
    assert body["lost_redundancy_paths"] == []


async def test_simulate_network_port_failure_isolates_downstream_equipment(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1, psu_quantity=0)
    switch = await _instantiate(client, headers, revision["id"])
    server = await _instantiate(client, headers, revision["id"])
    switch_port_id = switch["ports"][0]["id"]
    server_port_id = server["ports"][0]["id"]

    connect_resp = await client.post(
        f"/api/v1/equipment/{switch['id']}/ports/connect",
        json={"port_id": switch_port_id, "target_port_id": server_port_id, "status": "active"},
        headers=headers,
    )
    assert connect_resp.status_code == 201, connect_resp.text

    resp = await client.post(
        "/api/v1/impact/simulate", json={"target_type": "network_port", "target_id": switch_port_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["directly_impacted"]) == 1
    assert body["directly_impacted"][0]["equipment_id"] == server["id"]
    assert body["directly_impacted"][0]["impact_type"] == "network_isolated"


async def test_simulate_missing_target_is_404(client, auth_headers):
    headers = await _admin(auth_headers)
    resp = await client.post(
        "/api/v1/impact/simulate", json={"target_type": "power_node", "target_id": str(uuid.uuid4())}, headers=headers
    )
    assert resp.status_code == 404


async def test_simulate_invalid_target_type_is_422(client, auth_headers):
    headers = await _admin(auth_headers)
    resp = await client.post(
        "/api/v1/impact/simulate", json={"target_type": "power_supply", "target_id": str(uuid.uuid4())}, headers=headers
    )
    assert resp.status_code == 422


async def test_simulate_requires_authentication(client):
    resp = await client.post(
        "/api/v1/impact/simulate", json={"target_type": "power_node", "target_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 401
