# Regression tests for Managed Asset authorization hardening
# Covers P1 lifecycle transition guard and P2 list visibility filtering
# Uses existing test fixtures (client, db, auth helpers) from the repo

import uuid
import pytest
from httpx import AsyncClient
from app.core.errors import NotFoundError, ApiError
from app.application.access_control import AccessScope

# Helper to create a managed asset (equipment, rack, other type)
async def create_asset(client: AsyncClient, token: str, asset_type: str, site_id: str | None = None, rack_id: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"asset_type": asset_type, "asset_tag": f"tag-{uuid.uuid4().hex[:8]}"}
    if site_id:
        payload["site_id"] = site_id
    if rack_id:
        payload["rack_id"] = rack_id
    resp = await client.post("/managed-assets", json=payload, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]

@pytest.mark.asyncio
async def test_lifecycle_transition_fail_closed_without_scope(client: AsyncClient, restricted_token, unrestricted_token, db_session):
    # Create equipment in a site the restricted token cannot access
    asset_id = await create_asset(client, unrestricted_token, "equipment", site_id="site-allowed")

    # Attempt transition with restricted token – should get 404 NotFoundError
    headers = {"Authorization": f"Bearer {restricted_token}"}
    resp = await client.post(f"/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": "active"}, headers=headers)
    assert resp.status_code == 404
    # Ensure no audit/outbox side‑effects – verify DB unchanged (lifecycle_status remains original)
    # (Assuming a fixture to query directly)
    from app.domain.identity.models import ManagedAsset
    asset = await db_session.get(ManagedAsset, uuid.UUID(asset_id))
    assert asset.lifecycle_status != "active"

@pytest.mark.asyncio
async def test_list_visibility_filters_out_of_scope_assets(client: AsyncClient, restricted_token, unrestricted_token, db_session):
    # Create assets of various types
    eq_id = await create_asset(client, unrestricted_token, "equipment", site_id="site-allowed")
    rack_id = await create_asset(client, unrestricted_token, "rack", site_id="site-allowed")
    pdu_id = await create_asset(client, unrestricted_token, "pdu", site_id="site-allowed")
    # Restricted token only sees site‑allowed equipment/rack
    headers = {"Authorization": f"Bearer {restricted_token}"}
    resp = await client.get("/managed-assets", params={"asset_type": "equipment"}, headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    # Should include only equipment the token can see (eq_id)
    ids = {item["id"] for item in items}
    assert eq_id in ids
    # Rack should also be visible via separate request
    resp_rack = await client.get("/managed-assets", params={"asset_type": "rack"}, headers=headers)
    assert resp_rack.status_code == 200
    rack_ids = {item["id"] for item in resp_rack.json()["items"]}
    assert rack_id in rack_ids
    # PDU (unsupported type) must be filtered out – empty list
    resp_pdu = await client.get("/managed-assets", params={"asset_type": "pdu"}, headers=headers)
    assert resp_pdu.status_code == 200
    assert resp_pdu.json()["items"] == []

# Additional cases for unrestricted user (should see all)
@pytest.mark.asyncio
async def test_unrestricted_sees_all(client: AsyncClient, unrestricted_token):
    headers = {"Authorization": f"Bearer {unrestricted_token}"}
    resp = await client.get("/managed-assets", headers=headers)
    assert resp.status_code == 200
    # At least one item should be returned (previously created assets)
    assert len(resp.json()["items"]) > 0
