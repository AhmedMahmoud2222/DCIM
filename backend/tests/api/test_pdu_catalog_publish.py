"""Issue #150 M3: PDU publication lifecycle integration, end to end against real
PostgreSQL.

The full designer flow through the real HTTP boundary (this repo's
`test_catalog_designer_lifecycle.py` convention - real `client`, real `make_user`/
`auth_headers` RBAC, no mocks, no fake administrators): `POST /catalog/models` with
`category='pdu'` (the Phase-1 scope gate this milestone lifts), a draft revision, the M2
`/pdu` sub-resource for electrical authoring, the pre-publish `/validate` route, and
publication through the existing transactional `publish_revision` - revision row lock ->
publish validation (M1 `validate_pdu_revision` re-run server-side) -> single draft ->
published UPDATE -> audit + outbox, one transaction. PDU publication deliberately skips
the legacy rack/equipment bridge (§4.1 / Issue #150 architecture decision), which these
tests assert stays untouched."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditLog
from app.domain.catalog.designer_models import CatalogModelRevision, PDUOutletTemplate
from app.domain.catalog.models import EquipmentModelRevision, RackModelRevision
from app.domain.outbox.models import OutboxEvent


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_manufacturer(client, headers) -> str:
    resp = await client.post(
        "/api/v1/catalog/manufacturers", json={"name": f"PduPubMfr-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_pdu_draft(client, headers) -> tuple[str, dict]:
    """Real creation path: manufacturer -> model (category='pdu') -> draft revision."""
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "pdu", "model_name": f"PduPub-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    rev_resp = await client.post(f"/api/v1/catalog/models/{model_resp.json()['id']}/revisions", headers=headers)
    assert rev_resp.status_code == 201, rev_resp.text
    revision = rev_resp.json()
    assert revision["lifecycle_status"] == "draft"
    assert revision["revision_number"] == 1
    return model_resp.json()["id"], revision


def _outlet(outlet_number: int, **overrides) -> dict:
    payload = {
        "stable_key": f"outlet-{outlet_number}",
        "outlet_number": outlet_number,
        "outlet_type": "iec_c13",
        "outlet_count": 1,
        "phase": "single",
        "rated_voltage_v": 230.0,
        "rated_current_a": 10.0,
        "side": "front",
        "sort_order": outlet_number,
    }
    payload.update(overrides)
    return payload


async def _author_electrical(client, headers, revision: dict, *, outlets: bool = True) -> dict:
    body = {
        "pdu_input_voltage_v": 400.0,
        "pdu_input_current_a": 32.0,
        "pdu_phase": "three",
        "mounting_orientation": "vertical",
        "outlets": [_outlet(1), _outlet(2)] if outlets else [],
    }
    resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        json=body,
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------------ Creation gate


async def test_pdu_model_creation_and_draft_revision_are_supported(client: AsyncClient, auth_headers):
    headers = await _admin(auth_headers)
    model_id, revision = await _make_pdu_draft(client, headers)
    assert model_id
    assert revision["version"] == 1

    # Categories outside the designer/lifecycle workflow stay explicitly rejected -
    # lifting the PDU gate must not silently open every category.
    manufacturer_id = await _make_manufacturer(client, headers)
    rejected = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "ups", "model_name": "NotYet-UPS"},
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text
    assert "not yet supported" in rejected.json()["detail"]


# ------------------------------------------------------------------------ Pre-publish validation


async def test_pdu_validate_requires_electrical_spec_and_at_least_one_outlet(
    client: AsyncClient, auth_headers
):
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)

    resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["valid"] is False
    error_fields = {e["field"] for e in summary["errors"]}
    assert {
        "pdu_input_voltage_v",
        "pdu_input_current_a",
        "pdu_phase",
        "mounting_orientation",
        "outlets",
    } <= error_fields
    # Rack/equipment physical dimensions are deliberately NOT required for PDUs.
    assert "width_value" not in error_fields
    assert "rack_unit_height" not in error_fields

    # Author everything except outlets: only the outlets rule may fail (precision check).
    await _author_electrical(client, headers, revision, outlets=False)
    resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["valid"] is False
    assert {e["field"] for e in summary["errors"]} == {"outlets"}


# ------------------------------------------------------------------------ Publish happy path


async def test_pdu_publish_is_transactional_and_creates_no_legacy_bridge(
    client: AsyncClient, auth_headers, db_session: AsyncSession
):
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(client, headers, revision)

    valid = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert valid.status_code == 200, valid.text
    summary = valid.json()
    assert summary["valid"] is True
    assert summary["errors"] == []
    # Graphics warnings are advisory for every category (PR-5 directive), PDU included.
    assert {(w["field"], w["code"]) for w in summary["warnings"]} == {
        ("graphics.front", "graphic_missing"),
        ("graphics.rear", "graphic_missing"),
    }

    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text
    published = publish_resp.json()
    assert published["lifecycle_status"] == "published"
    assert published["published_at"] is not None
    assert "legacy_rack_model_revision_id" not in published
    assert "legacy_equipment_model_revision_id" not in published

    revision_id = uuid.UUID(revision["id"])
    stored = (
        await db_session.execute(select(CatalogModelRevision).where(CatalogModelRevision.id == revision_id))
    ).scalar_one()
    assert stored.lifecycle_status == "published"
    assert stored.published_by_user_id is not None
    # Issue #150 architecture decision: PDU publication is designer-side only - no legacy
    # rack/equipment bridge row may exist for a PDU revision, forward or reverse.
    assert stored.legacy_rack_model_revision_id is None
    assert stored.legacy_equipment_model_revision_id is None
    legacy_count = (
        await db_session.execute(
            select(func.count())
            .select_from(RackModelRevision)
            .where(RackModelRevision.bridged_from_catalog_revision_id == revision_id)
        )
    ).scalar_one()
    legacy_eq_count = (
        await db_session.execute(
            select(func.count())
            .select_from(EquipmentModelRevision)
            .where(EquipmentModelRevision.bridged_from_catalog_revision_id == revision_id)
        )
    ).scalar_one()
    assert (legacy_count, legacy_eq_count) == (0, 0)

    # Audit + outbox committed atomically with the publish (spec §5.3).
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == revision_id, AuditLog.action == "catalog.revision.publish"
            )
        )
    ).scalar_one()
    assert audit.result == "success"
    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == revision_id,
                OutboxEvent.event_type == "CatalogModelRevisionPublished",
            )
        )
    ).scalar_one()
    assert outbox.status == "pending"


# ------------------------------------------------------------------------ Publish-time re-validation


async def test_pdu_publish_reruns_m1_validation_over_stored_data(
    client: AsyncClient, auth_headers, db_session: AsyncSession
):
    """Data that reached the table without passing the `/pdu` PATCH validation (e.g.
    written before this validator existed, or directly by an admin tool) must still be
    rejected at publish - the M1 rules run server-side inside the publish transaction."""
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(client, headers, revision)

    await db_session.execute(
        update(CatalogModelRevision)
        .where(CatalogModelRevision.id == uuid.UUID(revision["id"]))
        .values(pdu_phase="bogus")
    )
    await db_session.commit()

    resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any(e["field"] == "pdu_phase" and e["code"] == "invalid_phase" for e in body["errors"])
    await db_session.rollback()

    # The failed publish wrote nothing: the revision is still an editable draft.
    stored = (
        await db_session.execute(
            select(CatalogModelRevision).where(CatalogModelRevision.id == uuid.UUID(revision["id"]))
        )
    ).scalar_one()
    assert stored.lifecycle_status == "draft"


# ------------------------------------------------------------------------ RBAC


async def test_viewer_cannot_publish_a_pdu_revision(client: AsyncClient, auth_headers):
    admin = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, admin)
    await _author_electrical(client, admin, revision)

    viewer = await auth_headers("Viewer")
    resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=viewer)
    assert resp.status_code == 403, resp.text


# ------------------------------------------------------------------------ Immutability / lifecycle


async def test_published_pdu_revision_is_immutable_and_cannot_be_republished(
    client: AsyncClient, auth_headers
):
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(client, headers, revision)

    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text
    published = publish_resp.json()

    edit = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        json={"pdu_input_voltage_v": 230.0, "outlets": [_outlet(1)]},
        headers={**headers, "If-Match": str(published["version"])},
    )
    assert edit.status_code == 409, edit.text

    republish = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert republish.status_code == 409, republish.text
    assert "not a draft" in republish.json()["detail"]


async def test_published_pdu_outlet_templates_reject_direct_db_writes(
    client: AsyncClient, auth_headers, db_session: AsyncSession
):
    """`trg_pdu_outlet_template_immutable` (migration 0055): published outlet rows reject
    direct INSERT/UPDATE/DELETE at the database level, so the draft-only contract holds
    even against tools that bypass the API - the exact guard `network_port_template`,
    `power_supply_template` and `monitoring_metric_template` already carry (0018)."""
    headers = await _admin(auth_headers)
    _, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(client, headers, revision)
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text

    revision_id = uuid.UUID(revision["id"])
    with pytest.raises(DBAPIError, match="child rows are immutable"):
        await db_session.execute(
            text("UPDATE pdu_outlet_template SET label = 'tampered' WHERE catalog_model_revision_id = :rid"),
            {"rid": revision_id},
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError, match="child rows are immutable"):
        await db_session.execute(
            text(
                "INSERT INTO pdu_outlet_template (catalog_model_revision_id, stable_key, outlet_number, label) "
                "VALUES (:rid, 'sneaky', 99, 'sneaky')"
            ),
            {"rid": revision_id},
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError, match="child rows are immutable"):
        await db_session.execute(
            text("DELETE FROM pdu_outlet_template WHERE catalog_model_revision_id = :rid"),
            {"rid": revision_id},
        )
    await db_session.rollback()

    # The tamper attempts changed nothing.
    intact = (
        await db_session.execute(
            select(func.count())
            .select_from(PDUOutletTemplate)
            .where(PDUOutletTemplate.catalog_model_revision_id == revision_id)
        )
    ).scalar_one()
    assert intact == 2


async def test_pdu_lifecycle_publish_retire_reclone(
    client: AsyncClient, auth_headers, db_session: AsyncSession
):
    headers = await _admin(auth_headers)
    model_id, revision = await _make_pdu_draft(client, headers)
    await _author_electrical(client, headers, revision)
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text

    # Retire (admin, catalog:retire) - generic lifecycle transition applies to PDUs.
    retire = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/retire",
        json={"reason": "superseded by revision 2"},
        headers=headers,
    )
    assert retire.status_code == 200, retire.text
    assert retire.json()["lifecycle_status"] == "retired"
    await db_session.rollback()

    # Clone preserves the PDU electrical spec and the outlet map (spec §5.5 / M3), so the
    # clone can be edited and republished - a clone that silently dropped them could not.
    clone_resp = await client.post(
        f"/api/v1/catalog/models/{model_id}/revisions/clone?from_revision_id={revision['id']}",
        headers=headers,
    )
    assert clone_resp.status_code == 201, clone_resp.text
    clone = clone_resp.json()
    assert clone["lifecycle_status"] == "draft"
    assert clone["revision_number"] == 2

    clone_draft = await client.get(f"/api/v1/catalog/revisions/{clone['id']}/pdu", headers=headers)
    assert clone_draft.status_code == 200, clone_draft.text
    body = clone_draft.json()
    assert float(body["pdu_input_voltage_v"]) == 400.0
    assert float(body["pdu_input_current_a"]) == 32.0
    assert body["pdu_phase"] == "three"
    assert body["mounting_orientation"] == "vertical"
    assert sorted(o["outlet_number"] for o in body["outlets"]) == [1, 2]

    clone_publish = await client.post(f"/api/v1/catalog/revisions/{clone['id']}/publish", headers=headers)
    assert clone_publish.status_code == 200, clone_publish.text
    assert clone_publish.json()["lifecycle_status"] == "published"
