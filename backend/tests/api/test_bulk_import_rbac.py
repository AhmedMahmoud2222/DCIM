"""Bulk-import RBAC closure — mirrors tests/api/test_catalog_rbac_closure.py's pattern
(synthetic roles with exact permission subsets via direct DB inserts): Viewer/Operator
forbidden from upload and commit on all three resources; a role holding
`catalog:manage`/`catalog:import` but never literally named "Administrator" is still
forbidden from catalog import (proves `require_catalog_administrator`-equivalent
semantics carry over to the generic /import-jobs/{id}/commit route); a role with only the
read permission can view template/status/preview but not upload/commit."""

import uuid

from sqlalchemy import text

from app.core.security import hash_password
from app.domain.auth.models import Role, RoleAssignment, User
from tests.api._bulk_import_helpers import build_workbook

RACK_HEADERS = [
    "asset_tag", "rack_name", "manufacturer", "model_name", "revision_number", "site_code", "building_code",
    "floor_level", "room_code", "x_mm", "y_mm", "rotation_deg", "owner", "notes",
]
EQUIPMENT_HEADERS = [
    "asset_tag", "hostname", "manufacturer", "model_name", "revision_number", "placement_type", "site_code",
    "building_code", "floor_level", "room_code", "rack_asset_tag", "u_start", "u_end", "side", "ip_address",
    "mac_address", "owner", "service", "environment", "notes", "lifecycle_status",
]
CATALOG_HEADERS = [
    "manufacturer_name", "category", "model_name", "model_number", "subtype", "description", "tags",
    "dimension_unit", "width_value", "height_value", "depth_value", "rack_unit_height", "weight_unit", "weight_value",
    "mounting_orientation", "supported_placement_types", "airflow_direction", "rated_power_w", "typical_power_w",
    "max_power_w", "heat_dissipation_btu_hr", "power_redundancy_mode", "revision_number", "clone_from_revision_number",
]

_UPLOAD_ENDPOINTS = (
    ("/api/v1/racks/import-jobs?mode=create_only", RACK_HEADERS, [["", "", "", "", "", "", "", "", "", "", "", "", "", ""]]),
    (
        "/api/v1/equipment/import-jobs?mode=create_only", EQUIPMENT_HEADERS,
        [["", "", "", "", "", "floor_standing", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "planned"]],
    ),
    (
        "/api/v1/catalog/import-jobs?mode=create_only", CATALOG_HEADERS,
        [["", "equipment", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]],
    ),
)
_TEMPLATE_ENDPOINTS = ("/api/v1/racks/import-template", "/api/v1/equipment/import-template", "/api/v1/catalog/import-template")


async def _make_role_with_permissions(db_session, role_name: str, codes: list[str]) -> uuid.UUID:
    role = Role(id=uuid.uuid4(), name=role_name, description="bulk-import RBAC test role", is_system=False)
    db_session.add(role)
    await db_session.flush()
    for code in codes:
        resource, action = code.split(":")
        permission_id = (
            await db_session.execute(
                text("SELECT id FROM permission WHERE resource = :r AND action = :a"), {"r": resource, "a": action}
            )
        ).scalar_one()
        await db_session.execute(
            text("INSERT INTO role_permission (role_id, permission_id) VALUES (:role_id, :permission_id)"),
            {"role_id": str(role.id), "permission_id": str(permission_id)},
        )
    await db_session.commit()
    return role.id


async def _headers_for_role(client, db_session, role_id: uuid.UUID) -> dict:
    password = "correct horse battery staple"
    email = f"bulk-import-rbac-{uuid.uuid4().hex[:8]}@example.com"
    user = User(id=uuid.uuid4(), email=email, full_name="Bulk Import RBAC Test User", password_hash=hash_password(password))
    db_session.add(user)
    await db_session.flush()
    db_session.add(RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role_id, scope_type="global"))
    await db_session.commit()
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_viewer_forbidden_from_upload_on_all_three_resources(client, auth_headers):
    headers = await auth_headers("Viewer")
    for path, cols, rows in _UPLOAD_ENDPOINTS:
        content = build_workbook(cols, rows)
        resp = await client.post(path, files={"file": ("x.xlsx", content, "application/octet-stream")}, headers=headers)
        assert resp.status_code == 403, f"{path}: expected 403, got {resp.status_code}: {resp.text}"


async def test_operator_forbidden_from_upload_on_all_three_resources(client, auth_headers):
    headers = await auth_headers("Operator")
    for path, cols, rows in _UPLOAD_ENDPOINTS:
        content = build_workbook(cols, rows)
        resp = await client.post(path, files={"file": ("x.xlsx", content, "application/octet-stream")}, headers=headers)
        assert resp.status_code == 403, f"{path}: expected 403, got {resp.status_code}: {resp.text}"


async def test_viewer_and_operator_forbidden_from_commit(client, auth_headers, db_session):
    from app.domain.bulk_import.models import BulkImportJob

    for role_name, import_type in (("Viewer", "rack"), ("Operator", "equipment")):
        uploader = User(
            id=uuid.uuid4(), email=f"uploader-{uuid.uuid4().hex[:8]}@example.com", full_name="Uploader",
            password_hash=hash_password("correct horse battery staple"),
        )
        db_session.add(uploader)
        await db_session.flush()
        job = BulkImportJob(
            import_type=import_type, mode="create_only", status="validated", uploaded_by_user_id=uploader.id,
            original_filename="x.xlsx", file_hash="a" * 64, file_size_bytes=10,
        )
        db_session.add(job)
        await db_session.commit()

        headers = await auth_headers(role_name)
        resp = await client.post(f"/api/v1/import-jobs/{job.id}/commit", headers=headers)
        assert resp.status_code == 403, f"{role_name}/{import_type}: expected 403, got {resp.status_code}: {resp.text}"


async def test_custom_role_with_catalog_manage_and_catalog_import_but_not_administrator_forbidden(client, db_session):
    """Proves the generic /import-jobs commit route's catalog check carries over
    `require_catalog_administrator`'s exact semantics: permission code alone (even both
    catalog:manage and catalog:import together) is insufficient without literal
    Administrator role membership."""
    role_id = await _make_role_with_permissions(
        db_session, f"Catalog Custom {uuid.uuid4().hex[:8]}", ["catalog:manage", "catalog:import", "catalog:read"],
    )
    headers = await _headers_for_role(client, db_session, role_id)

    content = build_workbook(
        CATALOG_HEADERS,
        [["Mfr", "equipment", f"Model-{uuid.uuid4().hex[:8]}", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
          "", "", "", "", "", "", ""]],
    )
    resp = await client.post(
        "/api/v1/catalog/import-jobs?mode=create_only", files={"file": ("x.xlsx", content, "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


async def test_read_only_role_can_view_template_status_and_preview_but_not_upload_or_commit(client, auth_headers, db_session):
    from app.domain.bulk_import.models import BulkImportJob

    headers = await auth_headers("Viewer")  # Viewer holds rack:read/equipment:read/catalog:read

    for path in _TEMPLATE_ENDPOINTS:
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200, f"{path}: expected 200, got {resp.status_code}"

    uploader = User(
        id=uuid.uuid4(), email=f"uploader-{uuid.uuid4().hex[:8]}@example.com", full_name="Uploader",
        password_hash=hash_password("correct horse battery staple"),
    )
    db_session.add(uploader)
    await db_session.flush()
    job = BulkImportJob(
        import_type="rack", mode="create_only", status="validated", uploaded_by_user_id=uploader.id,
        original_filename="x.xlsx", file_hash="b" * 64, file_size_bytes=10,
    )
    db_session.add(job)
    await db_session.commit()

    status_resp = await client.get(f"/api/v1/import-jobs/{job.id}", headers=headers)
    assert status_resp.status_code == 200
    rows_resp = await client.get(f"/api/v1/import-jobs/{job.id}/rows", headers=headers)
    assert rows_resp.status_code == 200

    commit_resp = await client.post(f"/api/v1/import-jobs/{job.id}/commit", headers=headers)
    assert commit_resp.status_code == 403

    upload_resp = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("x.xlsx", build_workbook(RACK_HEADERS, []), "application/octet-stream")}, headers=headers,
    )
    assert upload_resp.status_code == 403
