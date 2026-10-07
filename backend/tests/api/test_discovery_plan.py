"""GET /collectors/{id}/discovery-plan: the secret-free, profile-resolved work list for a collector."""

import json

from tests.api._network_helpers import (
    collector_with_integration,
    create_device,
    create_vendor,
    signed_get,
)

V3 = {
    "username": "dcim-poller", "auth_protocol": "sha256", "auth_secret": "plan-auth-secret-SEC04",
    "priv_protocol": "aes128", "priv_secret": "plan-priv-secret-SEC04",
}


async def test_plan_lists_assigned_snmp_integrations_with_resolved_profile_and_no_secrets(client, auth_headers):
    headers = await auth_headers("Administrator")
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"])
    collector, integration = await collector_with_integration(
        client, headers, integration_type="snmp", target_host="192.0.2.77", snmpv3=V3, device_profile_id=device["id"])
    _other_collector, other_integration = await collector_with_integration(client, headers, integration_type="snmp")

    response = await signed_get(client, collector, "discovery-plan")
    assert response.status_code == 200, response.text
    (item,) = response.json()
    assert item["integration_id"] == integration["id"] and item["target_host"] == "192.0.2.77"
    assert item["snmp_version"] == "v3" and item["snmpv3"]["username"] == "dcim-poller"
    assert item["plan"]["device_profile"]["id"] == device["id"]
    assert item["plan"]["neighbor_discovery"]["lldp"]["table_oid"] == "1.0.8802.1.1.2.1.4.1.1"
    assert item["plan"]["neighbor_behavior"]["cdp"]["enabled"] is True
    blob = json.dumps(response.json())
    assert "plan-auth-secret-SEC04" not in blob and "plan-priv-secret-SEC04" not in blob
    assert other_integration["id"] not in blob  # another collector's work is never exposed


async def test_integration_without_profile_has_a_null_plan_and_disabled_ones_are_omitted(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers, integration_type="snmp")
    assert (await signed_get(client, collector, "discovery-plan")).json()[0]["plan"] is None
    patched = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"enabled": False}, headers=headers | {"If-Match": "1"})
    assert patched.status_code == 200
    assert (await signed_get(client, collector, "discovery-plan")).json() == []


async def test_plan_requires_the_collectors_own_signature(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, _integration = await collector_with_integration(client, headers, integration_type="snmp")
    other, _ = await collector_with_integration(client, headers, integration_type="snmp")
    forged = dict(collector, secret="wrong-secret-wrong-secret-wrong-secret")
    assert (await signed_get(client, forged, "discovery-plan")).status_code == 401
    # a valid collector cannot read another collector's plan by swapping the path id
    import uuid

    from tests.api._phase8_helpers import sign_request

    sig = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=b"")
    cross = await client.get(f"/api/v1/collectors/{other['id']}/discovery-plan", headers=sig)
    assert cross.status_code == 401
    assert (await client.get(f"/api/v1/collectors/{collector['id']}/discovery-plan", headers=headers)).status_code == 422  # users have no access
