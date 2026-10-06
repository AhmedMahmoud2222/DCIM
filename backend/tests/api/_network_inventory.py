"""Inventory + discovery fixtures for neighbor reconciliation and cable tests."""

import uuid

from sqlalchemy import text

from tests.api._network_helpers import collector_with_integration, device_record, ingest_records


async def publish_revision(client, headers, port_names: list[str]) -> dict:
    manufacturer = await client.post("/api/v1/catalog/manufacturers", json={"name": f"Acme-{uuid.uuid4().hex[:8]}"}, headers=headers)
    assert manufacturer.status_code == 201, manufacturer.text
    model = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer.json()["id"], "category": "equipment", "model_name": f"M-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model.status_code == 201, model.text
    revision = (await client.post(f"/api/v1/catalog/models/{model.json()['id']}/revisions", headers=headers)).json()
    revision = (await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={"dimension_unit": "mm", "width_value": 440, "height_value": 44.45, "depth_value": 600, "weight_unit": "kg",
              "weight_value": 10, "rack_unit_height": 1, "supported_placement_types": ["rack_mounted"]},
        headers={**headers, "If-Match": str(revision["version"])},
    )).json()
    for name in port_names:
        port = await client.post(
            f"/api/v1/catalog/revisions/{revision['id']}/network-ports",
            json={"stable_key": name.lower().replace("/", "-"), "display_name": name, "media_type": "copper",
                  "supported_speeds_mbps": [1000], "connector_type": "rj45", "side": "rear"},
            headers={**headers, "If-Match": str(revision["version"])},
        )
        assert port.status_code == 201, port.text
        revision["version"] = port.json()["revision_version"]
    published = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    return published.json()


async def instantiate(client, headers, revision_id: str, *, hostname: str | None = None, ip_address: str | None = None) -> dict:
    body = {"asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision_id}
    if hostname:
        body["hostname"] = hostname
    if ip_address:
        body["ip_address"] = ip_address
    response = await client.post("/api/v1/equipment/instantiate", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def make_device(client, headers, port_names: list[str], **kwargs) -> dict:
    """Equipment with the named ports; returns the instantiate response plus `port_by_name`."""
    revision = await publish_revision(client, headers, port_names)
    equipment = await instantiate(client, headers, revision["id"], **kwargs)
    equipment["port_by_name"] = {p["display_name"]: p["id"] for p in equipment["ports"]}
    return equipment


async def set_mac(db_session, equipment_id: str, mac: str) -> None:
    await db_session.execute(text("UPDATE equipment SET mac_address = :mac WHERE id = :id"), {"mac": mac, "id": equipment_id})
    await db_session.commit()


async def observing_device(client, headers, equipment: dict, *, protocol_caps=("snmp",)) -> tuple[dict, dict]:
    """A collector + integration whose polled device is reconciled to `equipment` by a human."""
    collector, integration = await collector_with_integration(client, headers, integration_type="snmp", target_host="192.0.2.50")
    await ingest_records(client, collector, [device_record(integration["id"], "192.0.2.50")])
    discovered = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in discovered if d["integration_id"] == integration["id"])
    diffs = (await client.get("/api/v1/discovery/reconciliation", headers=headers)).json()
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])
    accepted = await client.post(
        f"/api/v1/discovery/reconciliation/{diff['id']}/accept", json={"matched_managed_asset_id": equipment["id"]}, headers=headers
    )
    assert accepted.status_code == 200, accepted.text
    return collector, integration


def neighbor_record(
    integration_id: str, *, local: str | None = "Eth1/1", chassis: str = "core-sw-1", port: str = "Eth1/24",
    protocol: str = "lldp", scan_id: str = "scan-1", occurred_at: str = "2026-01-01T00:00:00Z", **remote_extra,
) -> dict:
    remote = {
        "chassis_id": chassis, "chassis_id_subtype": "local", "port_id": port, "port_id_subtype": "interface_name",
        "system_name": chassis, "management_address": None, "port_description": None, "system_description": None,
        "platform": None,
    } | remote_extra
    neighbor = {
        "protocol": protocol, "local_port": {"name": local, "ref": "1"}, "remote": remote, "capabilities": ["bridge"],
        "native_vlan": None, "ttl_seconds": None, "raw": {"sys_name": chassis},
    }
    return {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration_id, "external_identifier": "192.0.2.50",
        "occurred_at": occurred_at, "record_type": "neighbor", "raw_attributes": {"scan_id": scan_id, "neighbor": neighbor},
    }


def scan_marker(integration_id: str, *, protocol: str = "lldp", started: str, complete: bool = True, finished: str | None = None) -> dict:
    return {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration_id, "external_identifier": "192.0.2.50",
        "occurred_at": finished or started, "record_type": "neighbor_scan",
        "raw_attributes": {"scan_id": "s", "protocol": protocol, "scan_started_at": started, "complete": complete,
                           "observed_count": 0, "malformed_rows": 0},
    }


async def list_neighbors(client, headers, **params) -> list[dict]:
    response = await client.get("/api/v1/discovery/neighbors", params={"limit": 200, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]
