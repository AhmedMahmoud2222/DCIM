"""Vendor/device profile administration: CRUD, optimistic concurrency, ambiguity refusal,
safe retirement, secret refusal and RBAC."""

import uuid

from tests.api._network_helpers import BASE, create_device, create_vendor, device_doc, vendor_doc
from tests.api._phase8_helpers import create_integration


def _if_match(version: int) -> dict:
    return {"If-Match": str(version)}


async def test_vendor_create_get_list_and_audit(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers, code="cisco-test")
    assert vendor["version"] == 1 and vendor["status"] == "active"
    assert vendor["neighbor_discovery"]["lldp"]["table_oid"] == "1.0.8802.1.1.2.1.4.1.1"
    got = await client.get(f"{BASE}/vendors/{vendor['id']}", headers=headers)
    assert got.status_code == 200 and got.json()["code"] == "cisco-test"
    listed = await client.get(f"{BASE}/vendors", headers=headers)
    assert [v["code"] for v in listed.json()] == ["cisco-test"]


async def test_duplicate_code_and_duplicate_prefix_conflict(client, auth_headers):
    headers = await auth_headers("Administrator")
    await create_vendor(client, headers, code="dup", prefixes=("1.3.6.1.4.1.9",))
    again = await client.post(f"{BASE}/vendors", json=vendor_doc("dup", prefixes=("1.3.6.1.4.1.10",)), headers=headers)
    assert again.status_code == 409
    clash = await client.post(f"{BASE}/vendors", json=vendor_doc("other", prefixes=("1.3.6.1.4.1.9",)), headers=headers)
    assert clash.status_code == 409 and "already claimed" in clash.json()["detail"]
    nested = await client.post(f"{BASE}/vendors", json=vendor_doc("nested", prefixes=("1.3.6.1.4.1.9.1",)), headers=headers)
    assert nested.status_code == 201


async def test_validation_rejects_unknown_fields_bad_oids_and_secrets(client, auth_headers):
    headers = await auth_headers("Administrator")
    for patch in (
        {"surprise": True},
        {"sys_object_id_prefixes": ["not-an-oid"]},
        {"supported_protocols": ["telnet"]},
        {"code": "UPPER"},
        {"discovery_oids": {"community": "1.3.6.1"}},
    ):
        resp = await client.post(f"{BASE}/vendors", json=vendor_doc() | patch, headers=headers)
        assert resp.status_code == 422, (patch, resp.text)


async def test_update_requires_if_match_and_bumps_version(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    body = {k: v for k, v in vendor_doc(vendor["code"]).items() if k != "code"} | {"name": "Renamed"}
    assert (await client.put(f"{BASE}/vendors/{vendor['id']}", json=body, headers=headers)).status_code == 428
    stale = await client.put(f"{BASE}/vendors/{vendor['id']}", json=body, headers=headers | _if_match(7))
    assert stale.status_code == 409
    ok = await client.put(f"{BASE}/vendors/{vendor['id']}", json=body, headers=headers | _if_match(1))
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["name"] == "Renamed"
    replay = await client.put(f"{BASE}/vendors/{vendor['id']}", json=body, headers=headers | _if_match(1))
    assert replay.status_code == 409


async def test_device_profile_must_be_backed_by_vendor_behaviour(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers, neighbor=False)
    resp = await client.post(f"{BASE}/vendors/{vendor['id']}/devices", json=device_doc(), headers=headers)
    assert resp.status_code == 422 and "does not define" in resp.json()["detail"]


async def test_device_create_update_and_listing(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"], code="c9300", match_criteria=[
        {"field": "model", "op": "prefix", "value": "C9300"}], firmware_min="16.0")
    assert device["vendor_profile_id"] == vendor["id"] and device["firmware_min"] == "16.0"
    body = {k: v for k, v in device_doc("c9300").items() if k != "code"} | {"priority": 9}
    updated = await client.put(f"{BASE}/devices/{device['id']}", json=body, headers=headers | _if_match(1))
    assert updated.status_code == 200 and updated.json()["priority"] == 9 and updated.json()["version"] == 2
    listed = await client.get(f"{BASE}/devices", params={"vendor_profile_id": vendor["id"]}, headers=headers)
    assert [d["code"] for d in listed.json()] == ["c9300"]


async def test_metric_mappings_validate_against_registry_and_override_vendor(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"])
    vendor_maps = {"mappings": [
        {"oid": "1.3.6.1.4.1.9.9.13.1.3.1.3.1", "canonical_metric": "temperature_c", "unit": "degC"},
        {"oid": "1.3.6.1.4.1.9.9.109.1.1.1.1.7.1", "canonical_metric": "load_percent", "unit": "%"},
    ]}
    resp = await client.put(f"{BASE}/vendors/{vendor['id']}/metric-mappings", json=vendor_maps, headers=headers | _if_match(1))
    assert resp.status_code == 200 and len(resp.json()) == 2
    bad_unit = {"mappings": [{"oid": "1.3.6.1.4.1.9.1", "canonical_metric": "temperature_c", "unit": "W"}]}
    assert (await client.put(f"{BASE}/devices/{device['id']}/metric-mappings", json=bad_unit, headers=headers | _if_match(1))).status_code == 422
    unknown = {"mappings": [{"oid": "1.3.6.1.4.1.9.1", "canonical_metric": "bogus", "unit": "W"}]}
    assert (await client.put(f"{BASE}/devices/{device['id']}/metric-mappings", json=unknown, headers=headers | _if_match(1))).status_code == 422
    dup_oid = {"mappings": [
        {"oid": "1.3.6.1.4.1.9.1", "canonical_metric": "temperature_c", "unit": "degC"},
        {"oid": "1.3.6.1.4.1.9.1", "canonical_metric": "load_percent", "unit": "%"}]}
    assert (await client.put(f"{BASE}/devices/{device['id']}/metric-mappings", json=dup_oid, headers=headers | _if_match(1))).status_code == 422
    override = {"mappings": [{"oid": "1.3.6.1.4.1.9.9.13.1.3.1.3.9", "canonical_metric": "temperature_c", "unit": "degC", "scale": 0.1}]}
    ok = await client.put(f"{BASE}/devices/{device['id']}/metric-mappings", json=override, headers=headers | _if_match(1))
    assert ok.status_code == 200
    effective = (await client.get(f"{BASE}/devices/{device['id']}/metric-mappings", params={"effective": "true"}, headers=headers)).json()
    by_metric = {m["canonical_metric"]: m for m in effective}
    assert by_metric["temperature_c"]["oid"].endswith(".9") and by_metric["temperature_c"]["scale"] == 0.1
    assert by_metric["load_percent"]["oid"].endswith(".7.1")  # inherited from vendor
    plan = (await client.get(f"{BASE}/devices/{device['id']}/plan", headers=headers)).json()
    assert plan["neighbor_discovery"]["cdp"]["columns"]["device_id"] == 6
    assert len(plan["metric_mappings"]) == 2
    assert "credential" not in str(plan).lower() and "community" not in str(plan).lower()


async def test_match_endpoint_refuses_ambiguity(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    a = await create_device(client, headers, vendor["id"], code="dev-a", match_criteria=[{"field": "model", "op": "prefix", "value": "C93"}])
    b = await create_device(client, headers, vendor["id"], code="dev-b", match_criteria=[{"field": "model", "op": "contains", "value": "300"}])
    facts = {"sys_object_id": "1.3.6.1.4.1.9.1.1", "model": "C9300"}
    result = (await client.post(f"{BASE}/match", json=facts, headers=headers)).json()
    assert result["state"] == "ambiguous" and result["device_profile_id"] is None
    assert set(result["candidate_device_profile_ids"]) == {a["id"], b["id"]}
    # Raising one profile's priority is the deliberate, auditable tie-break.
    body = {k: v for k, v in device_doc("dev-a", match_criteria=[{"field": "model", "op": "prefix", "value": "C93"}]).items() if k != "code"} | {"priority": 10}
    assert (await client.put(f"{BASE}/devices/{a['id']}", json=body, headers=headers | _if_match(1))).status_code == 200
    resolved = (await client.post(f"{BASE}/match", json=facts, headers=headers)).json()
    assert resolved["state"] == "matched" and resolved["device_profile_id"] == a["id"]


async def test_retire_is_terminal_and_guarded(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"])
    blocked = await client.post(f"{BASE}/vendors/{vendor['id']}/retire", headers=headers | _if_match(1))
    assert blocked.status_code == 409 and "active device profile" in blocked.json()["detail"]
    integration = await create_integration(client, headers, integration_type="snmp", device_profile_id=device["id"])
    assert integration["device_profile_id"] == device["id"]
    bound = await client.post(f"{BASE}/devices/{device['id']}/retire", headers=headers | _if_match(1))
    assert bound.status_code == 409 and "bound" in bound.json()["detail"]
    unbind = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"device_profile_id": None}, headers=headers | _if_match(1)
    )
    assert unbind.status_code == 200 and unbind.json()["device_profile_id"] is None
    assert (await client.post(f"{BASE}/devices/{device['id']}/retire", headers=headers | _if_match(1))).status_code == 200
    again = await client.post(f"{BASE}/devices/{device['id']}/retire", headers=headers | _if_match(2))
    assert again.status_code == 409
    update = {k: v for k, v in device_doc().items() if k != "code"}
    assert (await client.put(f"{BASE}/devices/{device['id']}", json=update, headers=headers | _if_match(2))).status_code == 409
    assert (await client.post(f"{BASE}/vendors/{vendor['id']}/retire", headers=headers | _if_match(1))).status_code == 200
    # Retired profiles are never matched and never bindable.
    assert (await client.post(f"{BASE}/match", json={"sys_object_id": "1.3.6.1.4.1.9.1"}, headers=headers)).json()["state"] == "no_match"
    other = await create_integration(client, headers, integration_type="snmp")
    rebind = await client.patch(
        f"/api/v1/integrations/{other['id']}", json={"device_profile_id": device["id"]}, headers=headers | _if_match(1)
    )
    assert rebind.status_code == 409
    new_under_retired = await client.post(f"{BASE}/vendors/{vendor['id']}/devices", json=device_doc(), headers=headers)
    assert new_under_retired.status_code == 409


async def test_binding_validates_protocol_and_snmp_version(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"], capabilities={
        "metrics": True, "lldp": True, "cdp": True, "snmp_versions": ["v3"]})
    icmp = await create_integration(client, headers, integration_type="icmp")
    resp = await client.patch(f"/api/v1/integrations/{icmp['id']}", json={"device_profile_id": device["id"]}, headers=headers | _if_match(1))
    assert resp.status_code == 422
    v2 = await create_integration(client, headers, integration_type="snmp", config={"version": "v2c"})
    resp = await client.patch(f"/api/v1/integrations/{v2['id']}", json={"device_profile_id": device["id"]}, headers=headers | _if_match(1))
    assert resp.status_code == 422 and "v2c" in resp.json()["detail"]


async def test_rbac_matrix(client, auth_headers):
    admin = await auth_headers("Administrator")
    vendor = await create_vendor(client, admin)
    for role, can_write in (("Administrator", True), ("DCIM Manager", True), ("Engineer", False), ("Operator", False), ("Viewer", False)):
        headers = await auth_headers(role)
        assert (await client.get(f"{BASE}/vendors", headers=headers)).status_code == 200, role
        assert (await client.get(f"{BASE}/vendors/{vendor['id']}", headers=headers)).status_code == 200
        created = await client.post(f"{BASE}/vendors", json=vendor_doc(prefixes=(f"1.3.6.1.4.1.{uuid.uuid4().int % 90000 + 100}",)), headers=headers)
        assert created.status_code == (201 if can_write else 403), (role, created.text)
        retire = await client.post(f"{BASE}/vendors/{vendor['id']}/retire", headers=headers | _if_match(99))
        assert retire.status_code in ((409,) if can_write else (403,)), (role, retire.status_code)
        mapping = await client.put(f"{BASE}/vendors/{vendor['id']}/metric-mappings", json={"mappings": []}, headers=headers | _if_match(99))
        assert mapping.status_code in ((409,) if can_write else (403,)), role


async def test_unauthenticated_and_unknown_ids(client, auth_headers):
    assert (await client.get(f"{BASE}/vendors")).status_code == 401
    headers = await auth_headers("Administrator")
    missing = uuid.uuid4()
    assert (await client.get(f"{BASE}/vendors/{missing}", headers=headers)).status_code == 404
    assert (await client.get(f"{BASE}/devices/{missing}", headers=headers)).status_code == 404
    assert (await client.post(f"{BASE}/vendors/{missing}/devices", json=device_doc(), headers=headers)).status_code == 404


async def test_templates_are_listed_and_creatable(client, auth_headers):
    headers = await auth_headers("Viewer")
    templates = (await client.get(f"{BASE}/templates", headers=headers)).json()
    assert {t["key"] for t in templates} >= {"ieee-lldp-switch", "cisco-cdp-lldp"}
    admin = await auth_headers("Administrator")
    template = (await client.get(f"{BASE}/templates/cisco-cdp-lldp", headers=headers)).json()
    vendor = await client.post(f"{BASE}/vendors", json=template["vendor"], headers=admin)
    assert vendor.status_code == 201
    device = await client.post(f"{BASE}/vendors/{vendor.json()['id']}/devices", json=template["devices"][0], headers=admin)
    assert device.status_code == 201
    assert (await client.get(f"{BASE}/templates/nope", headers=headers)).status_code == 404
