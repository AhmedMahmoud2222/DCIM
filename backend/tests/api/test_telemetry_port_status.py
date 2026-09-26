"""Phase 10C: POST/GET /telemetry/bindings and /telemetry/port-status/* end to end over
HTTP — matching this repo's tests/api convention."""

import uuid
from datetime import UTC, datetime


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_published_equipment_revision(client, headers, *, port_count: int = 1, psu_quantity: int = 0) -> dict:
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


async def test_create_binding_and_ingest_and_read_latest(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1)
    equipment = await _instantiate(client, headers, revision["id"])
    port = equipment["ports"][0]

    binding_resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={
            "equipment_id": equipment["id"], "target_type": "network_port", "equipment_port_id": port["id"],
            "protocol": "snmp", "external_ref": "1.3.6.1.2.1.2.2.1.8.1", "label": "eth0 link",
        },
        headers=headers,
    )
    assert binding_resp.status_code == 201, binding_resp.text
    binding = binding_resp.json()

    list_resp = await client.get("/api/v1/telemetry/bindings", params={"equipment_id": equipment["id"]}, headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    assert len(list_resp.json()) == 1

    ingest_resp = await client.post(
        "/api/v1/telemetry/port-status/ingest",
        json={
            "binding_id": binding["id"], "sampled_at": datetime.now(UTC).isoformat(),
            "payload": {"link_state": "UP", "bandwidth_util_pct": 42.0, "error_rate_pct": 0.0},
        },
        headers=headers,
    )
    assert ingest_resp.status_code == 200, ingest_resp.text
    assert ingest_resp.json()["status_level"] == "UP"

    latest_resp = await client.get(
        "/api/v1/telemetry/port-status/latest", params={"equipment_id": equipment["id"]}, headers=headers
    )
    assert latest_resp.status_code == 200, latest_resp.text
    [item] = latest_resp.json()
    assert item["binding_id"] == binding["id"]
    assert item["status_level"] == "UP"
    assert item["payload"]["bandwidth_util_pct"] == 42.0


async def test_ingest_down_link_state(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1)
    equipment = await _instantiate(client, headers, revision["id"])
    port = equipment["ports"][0]
    binding_resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={
            "equipment_id": equipment["id"], "target_type": "network_port", "equipment_port_id": port["id"],
            "protocol": "snmp", "external_ref": "oid", "label": None,
        },
        headers=headers,
    )
    assert binding_resp.status_code == 201, binding_resp.text
    binding_id = binding_resp.json()["id"]

    ingest_resp = await client.post(
        "/api/v1/telemetry/port-status/ingest",
        json={
            "binding_id": binding_id, "sampled_at": datetime.now(UTC).isoformat(),
            "payload": {"link_state": "DOWN", "bandwidth_util_pct": 0.0, "error_rate_pct": 0.0},
        },
        headers=headers,
    )
    assert ingest_resp.status_code == 200, ingest_resp.text
    assert ingest_resp.json()["status_level"] == "DOWN"


async def test_binding_target_mismatch_is_422(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1)
    equipment = await _instantiate(client, headers, revision["id"])
    port = equipment["ports"][0]

    resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={
            "equipment_id": equipment["id"], "target_type": "power_inlet", "equipment_port_id": port["id"],
            "protocol": "snmp", "external_ref": "oid", "label": None,
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_latest_requires_exactly_one_filter(client, auth_headers):
    headers = await _admin(auth_headers)
    resp = await client.get("/api/v1/telemetry/port-status/latest", headers=headers)
    assert resp.status_code == 422

    resp = await client.get(
        "/api/v1/telemetry/port-status/latest",
        params={"equipment_id": str(uuid.uuid4()), "rack_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_operator_role_cannot_manage_bindings(client, auth_headers):
    headers = await auth_headers("Operator")
    resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={
            "equipment_id": str(uuid.uuid4()), "target_type": "network_port", "equipment_port_id": str(uuid.uuid4()),
            "protocol": "snmp", "external_ref": "oid", "label": None,
        },
        headers=headers,
    )
    assert resp.status_code == 403


async def test_stale_ingest_keeps_the_http_contract_and_returns_the_retained_status(client, auth_headers):
    """The ordering guard changes which row wins, never the endpoint's shape: a stale
    sample is still a 200 with the same `PortStatusOut` body, reporting the status that
    is actually current (the retained newer one) rather than the one just submitted.
    It is deliberately not an error — a poller retrying a delayed batch has done nothing
    wrong and must not be driven into a retry loop by a 409."""
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1)
    equipment = await _instantiate(client, headers, revision["id"])
    port = equipment["ports"][0]
    binding_resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={
            "equipment_id": equipment["id"], "target_type": "network_port", "equipment_port_id": port["id"],
            "protocol": "snmp", "external_ref": "oid", "label": None,
        },
        headers=headers,
    )
    assert binding_resp.status_code == 201, binding_resp.text
    binding_id = binding_resp.json()["id"]

    newer = datetime(2026, 9, 25, 12, 5, 0, tzinfo=UTC)
    older = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    newer_resp = await client.post(
        "/api/v1/telemetry/port-status/ingest",
        json={
            "binding_id": binding_id, "sampled_at": newer.isoformat(),
            "payload": {"link_state": "UP", "bandwidth_util_pct": 30.0, "error_rate_pct": 0.0},
        },
        headers=headers,
    )
    assert newer_resp.status_code == 200, newer_resp.text
    assert newer_resp.json()["status_level"] == "UP"

    stale_resp = await client.post(
        "/api/v1/telemetry/port-status/ingest",
        json={
            "binding_id": binding_id, "sampled_at": older.isoformat(),
            "payload": {"link_state": "DOWN", "bandwidth_util_pct": 0.0, "error_rate_pct": 0.0},
        },
        headers=headers,
    )
    assert stale_resp.status_code == 200, stale_resp.text
    body = stale_resp.json()
    assert set(body) == set(newer_resp.json()), "response shape must be unchanged"
    assert body["status_level"] == "UP"
    assert body["payload"]["bandwidth_util_pct"] == 30.0

    latest_resp = await client.get(
        "/api/v1/telemetry/port-status/latest", params={"equipment_id": equipment["id"]}, headers=headers
    )
    assert latest_resp.status_code == 200, latest_resp.text
    [item] = latest_resp.json()
    assert item["status_level"] == "UP"
