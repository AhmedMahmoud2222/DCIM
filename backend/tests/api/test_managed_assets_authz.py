# Regression tests for Managed Asset authorization hardening
# Uses repository fixtures: client, auth_headers, make_user, db_session
# Covers:
#   - List endpoint visibility and pagination
#   - Lifecycle transition guard (fail‑closed for out‑of‑scope)
#   - No audit/outbox side‑effects on unauthorized actions
#   - Unrestricted admin behavior
#   - Missing permission (403) handling

import uuid
import pytest
from httpx import AsyncClient

# Helper to create a managed asset via the API
async def create_asset(client: AsyncClient, headers: dict, asset_type: str, **extra):
    payload = {"asset_type": asset_type, "asset_tag": f"tag-{uuid.uuid4().hex[:8]}"}
    payload.update(extra)
    resp = await client.post("/managed-assets", json=payload, headers=headers)
    assert resp.status_code == 201, f"create failed: {resp.text}"
    return resp.json()["id"]

@pytest.mark.asyncio
async def test_list_visibility_and_pagination(client: AsyncClient, auth_headers, db_session):
    # Create a site that will be visible to the unrestricted admin
    admin_hdr = await auth_headers("Administrator")
    # Create equipment and rack within the default site (admin can see all)
    eq_id = await create_asset(client, admin_hdr, "equipment", site_id="site-allowed")
    rack_id = await create_asset(client, admin_hdr, "rack", site_id="site-allowed")
    # Create an unsupported type (pdu) – should be filtered out for restricted callers
    pdu_id = await create_asset(client, admin_hdr, "pdu", site_id="site-allowed")

    # Create a restricted user scoped to a different site (site‑blocked)
    restricted_hdr = await auth_headers("Viewer")  # Viewer role has site‑scoped permissions

    # List equipment – restricted user should see none (site blocked)
    resp_eq = await client.get("/managed-assets", params={"asset_type": "equipment"}, headers=restricted_hdr)
    assert resp_eq.status_code == 200
    assert resp_eq.json()["items"] == []

    # List rack – also empty
    resp_rack = await client.get("/managed-assets", params={"asset_type": "rack"}, headers=restricted_hdr)
    assert resp_rack.status_code == 200
    assert resp_rack.json()["items"] == []

    # List pdu – unsupported type, must be empty for any scoped user
    resp_pdu = await client.get("/managed-assets", params={"asset_type": "pdu"}, headers=restricted_hdr)
    assert resp_pdu.status_code == 200
    assert resp_pdu.json()["items"] == []

    # Unrestricted admin should see all three assets with correct total
    resp_all = await client.get("/managed-assets", headers=admin_hdr)
    assert resp_all.status_code == 200
    items = resp_all.json()["items"]
    ids = {item["id"] for item in items}
    assert eq_id in ids and rack_id in ids and pdu_id in ids
    assert resp_all.json()["total"] == len(items)

    # Pagination sanity check – request limit=1
    resp_page = await client.get("/managed-assets", params={"limit": 1}, headers=admin_hdr)
    assert resp_page.status_code == 200
    page = resp_page.json()
    assert page["limit"] == 1
    assert page["total"] >= 1
    assert len(page["items"]) == 1

@pytest.mark.asyncio
async def test_lifecycle_transition_guard(client: AsyncClient, auth_headers, db_session):
    # Admin creates equipment in a site the restricted viewer cannot access
    admin_hdr = await auth_headers("Administrator")
    asset_id = await create_asset(client, admin_hdr, "equipment", site_id="site-allowed")

    # Viewer (restricted) attempts lifecycle transition – should get 404
    viewer_hdr = await auth_headers("Viewer")
    resp = await client.post(f"/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": "active"}, headers=viewer_hdr)
    assert resp.status_code == 404

    # Verify asset state unchanged
    from app.domain.identity.models import ManagedAsset
    asset = await db_session.get(ManagedAsset, uuid.UUID(asset_id))
    assert asset.lifecycle_status != "active"

    # Ensure no audit or outbox records were created for the failed attempt
    from app.domain.audit.models import AuditLog
    from app.domain.outbox.models import OutboxEvent
    audit = await db_session.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type='managed_asset' AND entity_id=:eid",
        {"eid": asset_id},
    )
    outbox = await db_session.execute(
        "SELECT COUNT(*) FROM outbox_event WHERE aggregate_type='managed_asset' AND aggregate_id=:eid",
        {"eid": asset_id},
    )
    assert audit.scalar_one() == 0
    assert outbox.scalar_one() == 0

@pytest.mark.asyncio
async def test_missing_permission_returns_403(client: AsyncClient, make_user, auth_headers):
    # Create a user with no managed_asset permissions (role "Guest")
    email = f"guest-{uuid.uuid4().hex[:8]}@example.com"
    await make_user(email, "pwd12345", "Guest")
    # Login to obtain token
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "pwd12345"})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}

    # Attempt to list assets – should be 403
    resp_list = await client.get("/managed-assets", headers=hdr)
    assert resp_list.status_code == 403

    # Attempt lifecycle transition – also 403 (endpoint permission missing)
    fake_id = str(uuid.uuid4())
    resp_tr = await client.post(f"/managed-assets/{fake_id}/lifecycle-transition", json={"to_status": "active"}, headers=hdr)
    assert resp_tr.status_code == 403
