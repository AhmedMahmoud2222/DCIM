"""Phase 10A PR-3: the catalog designer lifecycle API, end to end (docs/superpowers/
specs/2026-09-23-phase-10a-asset-catalog-designer-design.md §5/§8/§9.1/§10, aligned plan
§3.3). Every test hits the real HTTP boundary via `client`, matching this repo's
`test_racks.py`/`test_network_connections.py` convention — service-layer concurrency
internals are covered separately in
`tests/integration/test_catalog_designer_concurrency.py`."""

import uuid

from sqlalchemy import select

from app.domain.audit.models import AuditLog
from app.domain.catalog.models import RackModelRevision
from app.domain.outbox.models import OutboxEvent


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_manufacturer(client, headers, name: str = "Acme Rack Co") -> str:
    resp = await client.post("/api/v1/catalog/manufacturers", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_model(client, headers, manufacturer_id: str, *, category: str = "rack", model_name: str | None = None) -> str:
    resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": category, "model_name": model_name or f"Model-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_draft(client, headers, model_id: str) -> dict:
    resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _fill_required_rack_fields(client, headers, revision: dict, *, dimension_unit="in", weight_unit="lb") -> dict:
    body = {
        "dimension_unit": dimension_unit, "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4,
        "rack_unit_height": 42, "weight_unit": weight_unit, "weight_value": 220.0,
    }
    resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json=body,
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------------ End-to-end happy path


async def test_full_rack_lifecycle_create_edit_validate_publish_clone_retire(client, auth_headers, db_session):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id, category="rack", model_name="RackLife-42U")
    revision = await _make_draft(client, headers, model_id)
    assert revision["lifecycle_status"] == "draft"
    assert revision["revision_number"] == 1
    # The legacy bridge id is never exposed to the API (spec §4.7).
    assert "legacy_rack_model_revision_id" not in revision

    # Validate on an incomplete draft: fails, with required_for_category errors.
    invalid = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["valid"] is False
    error_fields = {e["field"] for e in invalid.json()["errors"]}
    assert {"dimension_unit", "width_value", "rack_unit_height"} <= error_fields

    # Fill every category='rack' required field, authored in inches/lb to exercise unit conversion.
    revision = await _fill_required_rack_fields(client, headers, revision, dimension_unit="in", weight_unit="lb")

    valid = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert valid.status_code == 200, valid.text
    assert valid.json() == {"valid": True, "errors": [], "warnings": []}

    # Publish re-runs validation and succeeds; verify audit + outbox committed atomically.
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text
    published = publish_resp.json()
    assert published["lifecycle_status"] == "published"
    assert published["published_at"] is not None
    assert "legacy_rack_model_revision_id" not in published

    revision_id = uuid.UUID(revision["id"])
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == revision_id, AuditLog.action == "catalog.revision.publish"))
    ).scalar_one()
    assert audit.result == "success"
    outbox = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == revision_id, OutboxEvent.event_type == "CatalogModelRevisionPublished"))
    ).scalar_one()
    assert outbox.status == "pending"

    # Legacy bridge row: created correctly, including unit conversion (in -> mm, lb -> kg).
    legacy = (
        await db_session.execute(select(RackModelRevision).where(RackModelRevision.bridged_from_catalog_revision_id == revision_id))
    ).scalar_one()
    assert legacy.height_u == 42
    assert legacy.width_mm == round(19.0 * 25.4)
    assert legacy.depth_mm == round(39.4 * 25.4)
    assert legacy.weight_capacity_kg == round(220.0 * 0.45359237)

    # Immutability: any further child write or scalar edit on the published revision is 409.
    port_attempt = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/network-ports",
        json={"stable_key": "eth0", "display_name": "eth0", "media_type": "copper", "supported_speeds_mbps": [1000], "connector_type": "rj45", "side": "front"},
        headers=headers,
    )
    assert port_attempt.status_code == 409, port_attempt.text
    assert "immutable" in port_attempt.json()["detail"]

    edit_attempt = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json={"weight_value": 999},
        headers={**headers, "If-Match": str(published["version"])},
    )
    assert edit_attempt.status_code == 409, edit_attempt.text

    # Clone the published revision into a new draft.
    clone_resp = await client.post(
        f"/api/v1/catalog/models/{model_id}/revisions/clone?from_revision_id={revision['id']}", headers=headers
    )
    assert clone_resp.status_code == 201, clone_resp.text
    clone = clone_resp.json()
    assert clone["lifecycle_status"] == "draft"
    assert clone["revision_number"] == 2
    assert clone["cloned_from_revision_id"] == revision["id"]
    assert clone["rack_unit_height"] == 42
    assert clone["dimension_unit"] == "in"

    # Retire requires a reason (422 without one — Pydantic body validation).
    no_reason = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/retire", json={}, headers=headers)
    assert no_reason.status_code == 422

    retire_resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/retire", json={"reason": "superseded by revision 2"}, headers=headers
    )
    assert retire_resp.status_code == 200, retire_resp.text
    retired = retire_resp.json()
    assert retired["lifecycle_status"] == "retired"
    assert retired["allow_installation_when_retired"] is False

    # allow_installation_when_retired toggle.
    override_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/retire-override",
        json={"allow_installation_when_retired": True, "reason": "keep selectable during migration window"},
        headers=headers,
    )
    assert override_resp.status_code == 200, override_resp.text
    assert override_resp.json()["allow_installation_when_retired"] is True

    # Compare the retired original against its draft clone.
    compare_resp = await client.get(
        f"/api/v1/catalog/revisions/compare?left={revision['id']}&right={clone['id']}", headers=headers
    )
    assert compare_resp.status_code == 200, compare_resp.text
    compare = compare_resp.json()
    assert compare["left_revision_id"] == revision["id"]
    assert compare["right_revision_id"] == clone["id"]


async def test_equipment_nameplate_power_requires_a_power_supply_and_snmp_metric_requires_oid(client, auth_headers):
    """§5.2's rule list, cases not covered by the rack happy path above."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers, name="Acme Equip Co")
    model_id = await _make_model(client, headers, manufacturer_id, category="equipment", model_name="EquipLife-1U")
    revision = await _make_draft(client, headers, model_id)

    body = {
        "dimension_unit": "mm", "width_value": 440, "height_value": 44, "depth_value": 600,
        "weight_unit": "kg", "weight_value": 8, "rated_power_w": 500,
    }
    resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json=body, headers={**headers, "If-Match": str(revision["version"])}
    )
    assert resp.status_code == 200, resp.text

    metric_resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics",
        json={"stable_key": "temp", "protocol": "snmp", "metric_name": "Inlet Temp", "value_type": "float"},
        headers=headers,
    )
    assert metric_resp.status_code == 201, metric_resp.text

    validate_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert validate_resp.status_code == 200
    summary = validate_resp.json()
    assert summary["valid"] is False
    codes_by_field = {e["field"]: e["code"] for e in summary["errors"]}
    assert codes_by_field.get("power_supplies") == "power_supply_required"
    assert codes_by_field.get("monitoring_metrics[0].oid") == "missing_oid_for_snmp"

    # Fix both: add a PSU, set the OID.
    psu_resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/power-supplies",
        json={"stable_key": "psu1", "label": "PSU 1", "connector_type": "C14"},
        headers=headers,
    )
    assert psu_resp.status_code == 201, psu_resp.text
    metric_id = metric_resp.json()["id"]
    patch_metric = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics/{metric_id}", json={"oid": "1.3.6.1.4.1.9.9.13.1.3.1.3"},
        headers=headers,
    )
    assert patch_metric.status_code == 200, patch_metric.text

    revalidate = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert revalidate.json()["valid"] is True


async def test_delete_draft_revision_cascades_and_is_audited(client, auth_headers, db_session):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    delete_resp = await client.delete(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert get_resp.status_code == 404

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == uuid.UUID(revision["id"]), AuditLog.action == "catalog.revision.delete_draft")
        )
    ).scalar_one()
    assert audit.result == "success"


async def test_cannot_publish_an_already_published_revision(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    revision = await _fill_required_rack_fields(client, headers, revision)
    first = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert first.status_code == 200

    second = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert second.status_code == 409


async def test_cannot_retire_a_draft(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/retire", json={"reason": "x"}, headers=headers)
    assert resp.status_code == 409


async def test_cannot_clone_a_draft_only_published_or_retired(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions/clone?from_revision_id={revision['id']}", headers=headers)
    assert resp.status_code == 409


# ------------------------------------------------------------------------ CatalogModel identity vs. metadata


async def test_patch_model_metadata_is_never_locked_by_publication(client, auth_headers):
    """Spec §4.1/§5.4 resolved decision, PR-2/PR-3: description/tags/status are mutable at
    any time, before or after publication — unlike manufacturer/category/model_name/
    model_number, which PR-1's DB trigger locks once any revision has published."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    await _fill_required_rack_fields(client, headers, revision)
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text

    patch_resp = await client.patch(
        f"/api/v1/catalog/models/{model_id}",
        json={"description": "Now with published history", "tags": ["flagship"], "status": "deprecated"},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    body = patch_resp.json()
    assert body["description"] == "Now with published history"
    assert body["tags"] == ["flagship"]
    assert body["status"] == "deprecated"


async def test_create_model_rejects_unsupported_category(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "sensor", "model_name": "Unsupported"},
        headers=headers,
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------------ Authorization matrix (new-router-specific)


async def test_engineer_cannot_reach_any_new_catalog_designer_mutation(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    engineer_headers = await auth_headers("Engineer")
    attempts = [
        ("POST", "/api/v1/catalog/manufacturers", {"name": "Should Fail"}),
        ("POST", "/api/v1/catalog/models", {"manufacturer_id": manufacturer_id, "category": "rack", "model_name": "Nope"}),
        ("PATCH", f"/api/v1/catalog/models/{model_id}", {"status": "deprecated"}),
        ("POST", f"/api/v1/catalog/models/{model_id}/revisions", None),
        ("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", None),
        ("POST", f"/api/v1/catalog/revisions/{revision['id']}/retire", {"reason": "x"}),
    ]
    for method, path, body in attempts:
        resp = await client.request(method, path, json=body, headers=engineer_headers)
        assert resp.status_code == 403, f"{method} {path}: expected 403, got {resp.status_code}: {resp.text}"


async def test_custom_role_with_only_catalog_manage_cannot_create_a_model(client, auth_headers, db_session):
    from sqlalchemy import text

    from app.core.security import hash_password
    from app.domain.auth.models import Role, RoleAssignment, User

    role = Role(id=uuid.uuid4(), name=f"Catalog Custom {uuid.uuid4().hex[:8]}", description="test", is_system=False)
    db_session.add(role)
    await db_session.flush()
    permission_id = (
        await db_session.execute(text("SELECT id FROM permission WHERE resource = 'catalog' AND action = 'manage'"))
    ).scalar_one()
    await db_session.execute(
        text("INSERT INTO role_permission (role_id, permission_id) VALUES (:r, :p)"), {"r": str(role.id), "p": str(permission_id)}
    )
    password = "correct horse battery staple"
    user = User(id=uuid.uuid4(), email=f"custom-{uuid.uuid4().hex[:8]}@example.com", full_name="Custom", password_hash=hash_password(password))
    db_session.add(user)
    await db_session.flush()
    db_session.add(RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role.id, scope_type="global"))
    await db_session.commit()
    login = await client.post("/api/v1/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post("/api/v1/catalog/manufacturers", json={"name": "Should Still Fail"}, headers=headers)
    assert resp.status_code == 403


async def test_read_draft_permission_gates_draft_detail_but_not_published(client, auth_headers, db_session):
    """The one conditional-permission read (spec §10): a Viewer (catalog:read, no
    catalog:read_draft) is rejected reading a draft's full detail, but can read a
    published revision's detail once it exists."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    viewer_headers = await auth_headers("Viewer")
    draft_read = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=viewer_headers)
    assert draft_read.status_code == 403

    revision = await _fill_required_rack_fields(client, headers, revision)
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200

    published_read = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=viewer_headers)
    assert published_read.status_code == 200


# ------------------------------------------------------------------------ Legacy catalog.py audit/outbox gap (closed in PR-3)


async def test_legacy_catalog_endpoints_now_write_audit_and_outbox(client, auth_headers, db_session):
    """Spec §1.1's gap ("No audit or outbox writes on any existing catalog endpoint"),
    closed in PR-3 (task instruction: add atomic audit/outbox rather than close the
    routes, since _phase2_helpers.py and the PR-2 frontend picker both still depend on
    them until PR-4)."""
    headers = await _admin(auth_headers)
    resp = await client.post("/api/v1/rack-models", json={"manufacturer": "Legacy Audit Co", "model_name": "LA-1"}, headers=headers)
    assert resp.status_code == 201, resp.text
    model_id = resp.json()["id"]

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(model_id), AuditLog.action == "rack_model.create"))
    ).scalar_one()
    assert audit.result == "success"
    outbox = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == uuid.UUID(model_id), OutboxEvent.event_type == "RackModelCreated"))
    ).scalar_one()
    assert outbox.status == "pending"
