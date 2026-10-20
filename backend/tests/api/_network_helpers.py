"""Shared builders for Issue #101 API tests."""

import copy
import uuid

from app.application.network.profile_templates import CDP_SPEC, LLDP_SPEC, STANDARD_DISCOVERY_OIDS

BASE = "/api/v1/network-profiles"


def vendor_doc(code=None, *, prefixes=("1.3.6.1.4.1.9",), neighbor=True, **extra) -> dict:
    doc = {
        "code": code or f"v-{uuid.uuid4().hex[:8]}",
        "name": "Test vendor",
        "sys_object_id_prefixes": list(prefixes),
        "supported_protocols": ["snmp"],
        "discovery_oids": dict(STANDARD_DISCOVERY_OIDS),
        "neighbor_discovery": {"lldp": copy.deepcopy(LLDP_SPEC), "cdp": copy.deepcopy(CDP_SPEC)} if neighbor else {},
    }
    doc.update(extra)
    return doc


def device_doc(code=None, **extra) -> dict:
    doc = {
        "code": code or f"d-{uuid.uuid4().hex[:8]}",
        "name": "Test device profile",
        "device_class": "switch",
        "match_criteria": [],
        "capabilities": {"metrics": True, "interfaces": True, "lldp": True, "cdp": True, "snmp_versions": ["v2c", "v3"]},
        "interface_discovery": {"strategy": "if_xtable", "name_source": "if_name"},
        "neighbor_behavior": {"lldp": {"enabled": True}, "cdp": {"enabled": True}},
    }
    doc.update(extra)
    return doc


async def create_vendor(client, headers, **kwargs) -> dict:
    resp = await client.post(f"{BASE}/vendors", json=vendor_doc(**kwargs), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_device(client, headers, vendor_id, **kwargs) -> dict:
    resp = await client.post(f"{BASE}/vendors/{vendor_id}/devices", json=device_doc(**kwargs), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def collector_with_integration(client, headers, *, integration_type="snmp", **integration_extra) -> tuple[dict, dict]:
    """An active central collector declaring `integration_type`, with a fresh integration assigned to it."""
    from tests.api._phase8_helpers import create_integration, register_collector

    collector = await register_collector(client, headers)
    caps = await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": [integration_type]}, headers=headers
    )
    assert caps.status_code == 204, caps.text
    integration = await create_integration(client, headers, integration_type=integration_type, **integration_extra)
    assigned = await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers
    )
    assert assigned.status_code == 201, assigned.text
    return collector, integration


async def signed_post(client, collector: dict, suffix: str, payload: dict):
    import json as _json

    from tests.api._phase8_helpers import sign_request

    raw = _json.dumps(payload).encode()
    sig = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    return await client.post(
        f"/api/v1/collectors/{collector['id']}/{suffix}", content=raw, headers={**sig, "Content-Type": "application/json"}
    )


async def ingest_records(client, collector: dict, records: list[dict]) -> dict:
    resp = await signed_post(client, collector, "ingest", {"batch_id": uuid.uuid4().hex, "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def device_record(integration_id: str, external_identifier: str, **raw) -> dict:
    return {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration_id, "external_identifier": external_identifier,
        "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": raw,
    }


async def signed_get(client, collector: dict, suffix: str):
    from tests.api._phase8_helpers import sign_request

    sig = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=b"")
    return await client.get(f"/api/v1/collectors/{collector['id']}/{suffix}", headers=sig)
