"""API-level tests for Phase 8 Integration CRUD -- credential write-only semantics,
optimistic concurrency (If-Match/version), and RBAC (integration:read vs
integration:manage)."""

import uuid

from tests.api._phase8_helpers import create_integration


async def test_create_integration_returns_no_credential_field(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers, credential="super-secret-community-string")
    assert "credential" not in integration
    assert "credential_ciphertext" not in integration
    assert integration["has_credential"] is True


async def test_create_integration_without_credential(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers)
    assert integration["has_credential"] is False


async def test_list_integrations(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    await create_integration(client, headers)
    await create_integration(client, headers)
    resp = await client.get("/api/v1/integrations", headers=headers)
    assert resp.status_code == 200
    names = [i["name"] for i in resp.json()]
    assert len(names) >= 2


async def test_get_integration_by_id(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    created = await create_integration(client, headers)
    resp = await client.get(f"/api/v1/integrations/{created['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert "credential" not in resp.json()


async def test_get_integration_404_for_bogus_id(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    resp = await client.get(f"/api/v1/integrations/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_viewer_can_read_but_not_create(client, auth_headers):
    manager_headers = await auth_headers("DCIM Manager")
    viewer_headers = await auth_headers("Viewer")

    resp = await client.post(
        "/api/v1/integrations",
        json={"name": f"i-{uuid.uuid4().hex[:8]}", "integration_type": "icmp", "target_host": "127.0.0.1"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403

    integration = await create_integration(client, manager_headers)
    resp = await client.get(f"/api/v1/integrations/{integration['id']}", headers=viewer_headers)
    assert resp.status_code == 200


async def test_update_requires_if_match_version(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers)

    # No If-Match header at all -- must be rejected, not silently applied.
    resp = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"enabled": False}, headers=headers,
    )
    assert resp.status_code == 428


async def test_update_with_stale_if_match_is_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers)

    stale_headers = {**headers, "If-Match": str(integration["version"])}
    resp = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"enabled": False}, headers=stale_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == integration["version"] + 1

    # Replaying the same (now stale) If-Match a second time must fail.
    resp2 = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"enabled": True}, headers=stale_headers,
    )
    assert resp2.status_code == 409


async def test_update_credential_then_read_never_exposes_it(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers)
    update_headers = {**headers, "If-Match": str(integration["version"])}
    resp = await client.patch(
        f"/api/v1/integrations/{integration['id']}", json={"credential": "rotated-secret"}, headers=update_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["has_credential"] is True
    assert "credential" not in resp.json()
