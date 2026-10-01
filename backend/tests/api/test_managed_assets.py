import uuid
from datetime import UTC, datetime

from app.domain.identity.models import ManagedAsset


async def test_create_managed_asset(client, auth_headers):
    headers = await auth_headers("Engineer")
    resp = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["lifecycle_status"] == "planned"


async def test_invalid_asset_type_is_rejected(client, auth_headers):
    headers = await auth_headers("Engineer")
    resp = await client.post("/api/v1/managed-assets", json={"asset_type": "spaceship", "asset_tag": "X-1"}, headers=headers)
    assert resp.status_code == 422


async def test_oversized_asset_tag_is_a_clean_validation_error_not_a_500(client, auth_headers):
    """Finding M1 regression (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
    PHASE1_CORRECTION_REPORT.md): an asset_tag longer than the column's 64 characters
    previously reached the database and surfaced as an unhandled 500."""
    headers = await auth_headers("Engineer")
    resp = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": "X" * 100_000}, headers=headers
    )
    assert resp.status_code == 422


async def test_duplicate_idempotency_key_returns_original_result_not_a_second_asset(client, auth_headers):
    headers = await auth_headers("Engineer")
    key = str(uuid.uuid4())
    tag = f"RACK-{uuid.uuid4().hex[:8]}"

    first = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "rack", "asset_tag": tag},
        headers={**headers, "Idempotency-Key": key},
    )
    second = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "rack", "asset_tag": tag},
        headers={**headers, "Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listing = await client.get("/api/v1/managed-assets", params={"asset_type": "rack"}, headers=headers)
    matching = [a for a in listing.json()["items"] if a["asset_tag"] == tag]
    assert len(matching) == 1


async def test_reused_idempotency_key_with_different_body_is_rejected(client, auth_headers):
    headers = await auth_headers("Engineer")
    key = str(uuid.uuid4())
    await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "rack", "asset_tag": "RACK-A"},
        headers={**headers, "Idempotency-Key": key},
    )
    resp = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "rack", "asset_tag": "RACK-B"},
        headers={**headers, "Idempotency-Key": key},
    )
    assert resp.status_code == 422


async def test_lifecycle_transition_valid_path(client, auth_headers):
    headers = await auth_headers("Engineer")
    created = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    asset_id = created.json()["id"]

    resp = await client.post(
        f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": "installed"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["lifecycle_status"] == "installed"


async def test_lifecycle_transition_to_decommissioned_persists_utc_timestamp(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    created = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert created.status_code == 201
    asset_id = created.json()["id"]

    for status in ("installed", "active"):
        response = await client.post(
            f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": status}, headers=headers
        )
        assert response.status_code == 200, response.text

    before = datetime.now(UTC).replace(tzinfo=None)
    response = await client.post(
        f"/api/v1/managed-assets/{asset_id}/lifecycle-transition",
        json={"to_status": "decommissioned"},
        headers=headers,
    )
    after = datetime.now(UTC).replace(tzinfo=None)
    assert response.status_code == 200, response.text
    assert response.json()["lifecycle_status"] == "decommissioned"

    asset = await db_session.get(ManagedAsset, uuid.UUID(asset_id))
    assert asset is not None
    await db_session.refresh(asset)
    assert asset.decommissioned_at is not None
    assert asset.decommissioned_at.tzinfo is None
    assert before <= asset.decommissioned_at <= after


async def test_lifecycle_transition_invalid_path_is_rejected(client, auth_headers):
    headers = await auth_headers("Engineer")
    created = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    asset_id = created.json()["id"]

    resp = await client.post(
        f"/api/v1/managed-assets/{asset_id}/lifecycle-transition",
        json={"to_status": "decommissioned"},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_operator_can_transition_lifecycle_but_not_create(client, auth_headers):
    engineer_headers = await auth_headers("Engineer")
    created = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"RACK-{uuid.uuid4().hex[:8]}"}, headers=engineer_headers
    )
    asset_id = created.json()["id"]

    operator_headers = await auth_headers("Operator")
    create_attempt = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": "SHOULD-FAIL"}, headers=operator_headers
    )
    assert create_attempt.status_code == 403

    transition_attempt = await client.post(
        f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": "installed"}, headers=operator_headers
    )
    assert transition_attempt.status_code == 200
