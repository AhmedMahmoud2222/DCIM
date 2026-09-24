"""Phase 10A PR-2: RBAC closure of the four legacy catalog-authoring endpoints (docs/
superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md §9.1/§9.10,
aligned plan §3.2). Every test hits the real HTTP boundary via `client`, matching this
repo's `test_security.py` convention — not a service-layer unit test.

Endpoints under test: POST /rack-models, POST /rack-models/{id}/revisions,
POST /equipment-models, POST /equipment-models/{id}/revisions — the only four PR-2
touches. No catalog designer API exists yet (PR-3+); this file tests nothing else.

Covers the exact regression matrix spec §9.10 calls for: a role holding only Engineer's
permission set is rejected at every one of these four endpoints; a synthetic custom role
holding only catalog:manage (never named "Administrator") is rejected, proving the
permission code alone is insufficient; an Administrator with catalog:manage's grant
revoked is rejected, proving there is no name-based bypass; an Administrator holding
catalog:manage succeeds; and the two read (list) endpoints per model remain reachable by
every role already holding rack:read/equipment:read, with no Administrator requirement."""

import uuid

from sqlalchemy import text

from app.core.security import hash_password
from app.domain.auth.models import Role, RoleAssignment, User
from app.domain.catalog.models import EquipmentModel, RackModel


async def _make_rack_model(db_session) -> str:
    model = RackModel(manufacturer="RBAC Test Rack Co", model_name=f"Model-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.commit()
    return str(model.id)


async def _make_equipment_model(db_session) -> str:
    model = EquipmentModel(manufacturer="RBAC Test Equip Co", model_name=f"Model-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.commit()
    return str(model.id)


async def _mutating_endpoints(db_session) -> list[tuple[str, str, dict]]:
    """The four (method, path, json_body) mutating requests under test. Built fresh per
    call — each call creates its own fresh parent model row for the two revision
    endpoints, since two different test cases must never share a parent whose creation
    might itself have been gated differently."""
    rack_model_id = await _make_rack_model(db_session)
    equipment_model_id = await _make_equipment_model(db_session)
    return [
        ("POST", "/api/v1/rack-models", {"manufacturer": "X", "model_name": f"M-{uuid.uuid4().hex[:8]}"}),
        ("POST", f"/api/v1/rack-models/{rack_model_id}/revisions", {"height_u": 42, "width_mm": 600, "depth_mm": 1000}),
        ("POST", "/api/v1/equipment-models", {"manufacturer": "X", "model_name": f"M-{uuid.uuid4().hex[:8]}"}),
        ("POST", f"/api/v1/equipment-models/{equipment_model_id}/revisions", {}),
    ]


async def _make_role_with_permissions(db_session, role_name: str, codes: list[str]) -> uuid.UUID:
    """A synthetic, non-seeded role holding exactly the given permission codes and
    deliberately not named 'Administrator' — proves `require_catalog_administrator()`'s
    role-membership check is genuinely enforced, not merely documented: a custom role can
    hold `catalog:manage` (created by any principal holding `role:manage`) without ever
    being the Administrator role."""
    role = Role(id=uuid.uuid4(), name=role_name, description="PR-2 RBAC test role", is_system=False)
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
    email = f"rbac-{uuid.uuid4().hex[:8]}@example.com"
    user = User(id=uuid.uuid4(), email=email, full_name="RBAC Test User", password_hash=hash_password(password))
    db_session.add(user)
    await db_session.flush()
    db_session.add(RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role_id, scope_type="global"))
    await db_session.commit()
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ------------------------------------------------------- §9.10 dual-check regression matrix


async def test_engineer_role_gets_403_on_every_mutating_endpoint(client, db_session, auth_headers):
    """Engineer holds rack:manage/equipment:manage (installing/managing rack and equipment
    *instances* stays untouched by this PR) but never catalog:manage — the concrete
    regression test for §9.1's behavior change: minting a new catalog model/revision is no
    longer reachable through the permission set that used to gate it."""
    headers = await auth_headers("Engineer")
    for method, path, body in await _mutating_endpoints(db_session):
        resp = await client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403, f"{method} {path}: expected 403, got {resp.status_code}: {resp.text}"


async def test_custom_role_with_only_catalog_manage_gets_403(client, db_session):
    role_id = await _make_role_with_permissions(db_session, f"Catalog Custom {uuid.uuid4().hex[:8]}", ["catalog:manage"])
    headers = await _headers_for_role(client, db_session, role_id)
    for method, path, body in await _mutating_endpoints(db_session):
        resp = await client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403, f"{method} {path}: expected 403, got {resp.status_code}: {resp.text}"


async def test_administrator_without_catalog_manage_gets_403(client, db_session, auth_headers):
    """Revoking catalog:manage from the Administrator role's own grant — not the role
    assignment — still blocks every mutation, proving there is no special-case bypass for
    the Administrator role name itself. The revoked row is restored afterward: role_permission
    is shared, session-scoped seed data (not reset by the per-test _clean_tables fixture),
    so leaving it deleted would corrupt every later test's Administrator fixture."""
    headers = await auth_headers("Administrator")
    role_id = (await db_session.execute(text("SELECT id FROM role WHERE name = 'Administrator'"))).scalar_one()
    permission_id = (
        await db_session.execute(text("SELECT id FROM permission WHERE resource = 'catalog' AND action = 'manage'"))
    ).scalar_one()
    await db_session.execute(
        text("DELETE FROM role_permission WHERE role_id = :role_id AND permission_id = :permission_id"),
        {"role_id": role_id, "permission_id": permission_id},
    )
    await db_session.commit()
    try:
        for method, path, body in await _mutating_endpoints(db_session):
            resp = await client.request(method, path, json=body, headers=headers)
            assert resp.status_code == 403, f"{method} {path}: expected 403, got {resp.status_code}: {resp.text}"
    finally:
        await db_session.execute(
            text("INSERT INTO role_permission (role_id, permission_id) VALUES (:role_id, :permission_id)"),
            {"role_id": role_id, "permission_id": permission_id},
        )
        await db_session.commit()


async def test_administrator_with_catalog_manage_succeeds(client, db_session, auth_headers):
    headers = await auth_headers("Administrator")
    for method, path, body in await _mutating_endpoints(db_session):
        resp = await client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 201, f"{method} {path}: expected 201, got {resp.status_code}: {resp.text}"


async def test_reads_remain_unaffected_by_administrator_requirement(client, db_session, auth_headers):
    """The negative-space test: catalog:read/rack:read/equipment:read were not
    accidentally tightened alongside catalog:manage. A Viewer — no catalog:manage, no
    Administrator role — can still list rack/equipment models and revisions."""
    rack_model_id = await _make_rack_model(db_session)
    equipment_model_id = await _make_equipment_model(db_session)
    headers = await auth_headers("Viewer")
    for path in (
        "/api/v1/rack-models",
        f"/api/v1/rack-models/{rack_model_id}/revisions",
        "/api/v1/equipment-models",
        f"/api/v1/equipment-models/{equipment_model_id}/revisions",
    ):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200, f"GET {path}: expected 200, got {resp.status_code}: {resp.text}"
