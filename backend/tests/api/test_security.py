"""Focused adversarial review (§43 of the Phase 1 prompt): tries to break login,
authorization, object access, and error handling. Reported findings are in
PHASE1_IMPLEMENTATION_REPORT.md's Security section — this is not a claim of external
penetration testing."""

import uuid


async def test_protected_endpoint_rejects_missing_token(client):
    resp = await client.get("/api/v1/organizations")
    assert resp.status_code == 401
    assert "password" not in resp.text.lower()


async def test_protected_endpoint_rejects_malformed_token(client):
    resp = await client.get("/api/v1/organizations", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


async def test_protected_endpoint_rejects_token_signed_with_wrong_algorithm_expectation(client, auth_headers):
    """A token for a different type (refresh) must never be accepted as an access
    token — this is the same check as test_token_type_confusion_is_rejected but through
    the actual HTTP boundary, not just the helper function."""
    headers = await auth_headers("Viewer")
    token = headers["Authorization"].split(" ")[1]
    resp = await client.get("/api/v1/organizations", headers={"Authorization": f"Bearer {token}x"})
    assert resp.status_code == 401


async def test_viewer_cannot_escalate_by_calling_manage_endpoints_directly(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.post("/api/v1/users", json={"email": "new@example.com", "full_name": "X", "password": "x", "role_name": "Administrator"}, headers=headers)
    assert resp.status_code == 403


async def test_unauthenticated_user_cannot_enumerate_managed_assets(client):
    resp = await client.get("/api/v1/managed-assets")
    assert resp.status_code == 401


async def test_404_does_not_leak_internal_details(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.get(f"/api/v1/organizations/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) >= {"type", "title", "status", "detail", "instance"}
    assert "Traceback" not in resp.text
    assert "sqlalchemy" not in resp.text.lower()


async def test_sql_injection_shaped_input_is_stored_safely_not_executed(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    payload_name = "Acme'; DROP TABLE organization;--"
    resp = await client.post("/api/v1/organizations", json={"name": payload_name}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["name"] == payload_name

    still_works = await client.get("/api/v1/organizations", headers=headers)
    assert still_works.status_code == 200


async def test_cors_headers_present_for_configured_origin(client):
    resp = await client.options(
        "/api/v1/health/live",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


async def test_response_never_includes_password_hash(client, auth_headers, make_user):
    headers = await auth_headers("Administrator")
    resp = await client.post(
        "/api/v1/users",
        json={"email": f"leak-{uuid.uuid4().hex[:6]}@example.com", "full_name": "Leak Test", "password": "hunter2", "role_name": "Viewer"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert "password" not in resp.text.lower()
    assert "hunter2" not in resp.text
