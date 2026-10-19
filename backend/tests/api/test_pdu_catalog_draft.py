"""Issue #150 M2: the narrow `/catalog/revisions/{id}/pdu` draft API, end to end against
real PostgreSQL.

Every test hits the HTTP boundary through `client` with real `make_user`/`auth_headers`
principals (this repo's `test_catalog_designer_lifecycle.py` convention) - no mocked
persistence, no mocked authorization. PDU-category models are seeded through the ORM
because `POST /catalog/models` still rejects `category='pdu'` by design (the Phase 1 scope
limitation Issue #150 documents - model creation/publish integration is a later milestone);
rack models go through the real designer endpoints so existing catalog behavior is
exercised alongside the new `/pdu` sub-resource."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditLog
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer
from app.domain.outbox.models import OutboxEvent

PASSWORD = "correct horse battery staple"


async def _seed_pdu_revision(
    db: AsyncSession,
    *,
    creator_id: uuid.UUID,
    lifecycle_status: str = "draft",
    version: int = 1,
) -> CatalogModelRevision:
    """Insert a PDU-category model + revision directly (see module docstring)."""
    manufacturer = Manufacturer(name=f"PduMfr-{uuid.uuid4().hex[:8]}")
    db.add(manufacturer)
    await db.flush()
    model = CatalogModel(
        manufacturer_id=manufacturer.id,
        category="pdu",
        model_name=f"PduModel-{uuid.uuid4().hex[:8]}",
    )
    db.add(model)
    await db.flush()
    revision = CatalogModelRevision(
        catalog_model_id=model.id,
        revision_number=1,
        created_by_user_id=creator_id,
        lifecycle_status=lifecycle_status,
        version=version,
        pdu_input_voltage_v=230.0,
        pdu_input_current_a=16.0,
        pdu_phase="single",
    )
    db.add(revision)
    await db.flush()
    await db.commit()
    return revision


def _outlet(outlet_number: int, *, stable_key: str | None = None, **overrides) -> dict:
    payload = {
        "stable_key": stable_key or f"outlet-{outlet_number}",
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


async def _creator(db: AsyncSession, make_user) -> uuid.UUID:
    user = await make_user(f"pdu-creator-{uuid.uuid4().hex[:8]}@example.com", PASSWORD, "Administrator")
    return user.id


def _url(revision_id: uuid.UUID) -> str:
    return f"/api/v1/catalog/revisions/{revision_id}/pdu"


# ------------------------------------------------------------------------ Authentication / authz


async def test_get_pdu_draft_requires_authentication(client: AsyncClient, db_session, make_user):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    resp = await client.get(_url(revision.id))
    assert resp.status_code == 401, resp.text


async def test_viewer_can_read_a_draft(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    headers = await auth_headers("Viewer")
    resp = await client.get(_url(revision.id), headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision_id"] == str(revision.id)
    assert body["lifecycle_status"] == "draft"
    assert body["version"] == 1
    assert float(body["pdu_input_voltage_v"]) == 230.0
    assert float(body["pdu_input_current_a"]) == 16.0
    assert body["pdu_phase"] == "single"
    assert body["outlets"] == []


async def test_viewer_cannot_update_a_draft(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    headers = await auth_headers("Viewer")
    resp = await client.patch(_url(revision.id), json={"pdu_phase": "three"}, headers={**headers, "If-Match": "1"})
    assert resp.status_code == 403, resp.text


# ------------------------------------------------------------------------ Draft persistence


async def test_admin_update_persists_electrical_spec_and_outlets(
    client: AsyncClient, db_session, make_user, auth_headers
):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={
            "pdu_input_voltage_v": 400.0,
            "pdu_input_current_a": 32.0,
            "pdu_phase": "three",
            "mounting_orientation": "vertical",
            "outlets": [
                _outlet(1),
                _outlet(2, outlet_type="iec_c19", phase="three", sort_order=2),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lifecycle_status"] == "draft"
    assert body["version"] == 2
    assert float(body["pdu_input_voltage_v"]) == 400.0
    assert float(body["pdu_input_current_a"]) == 32.0
    assert body["pdu_phase"] == "three"
    assert body["mounting_orientation"] == "vertical"
    assert [(o["outlet_number"], o["stable_key"]) for o in body["outlets"]] == [
        (1, "outlet-1"),
        (2, "outlet-2"),
    ]
    assert body["outlets"][1]["outlet_type"] == "iec_c19"
    assert body["outlets"][1]["phase"] == "three"

    # Persistence is real: a *new* authenticated read sees the stored rows, and the
    # mutation produced both an audit entry and an outbox event.
    fresh = await client.get(_url(revision.id), headers=await auth_headers("Viewer"))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["version"] == 2
    assert len(fresh.json()["outlets"]) == 2

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "catalog.pdu_draft.update")
        )
    ).scalars().first()
    assert audit is not None
    assert str(audit.entity_id) == str(revision.id)

    outbox = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.event_type == "PDUDraftUpdated"))
    ).scalars().first()
    assert outbox is not None
    assert str(outbox.aggregate_id) == str(revision.id)


async def test_duplicate_outlet_number_rejected_and_rolled_back(
    client: AsyncClient, db_session, make_user, auth_headers
):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    # Captured pre-rollback: rollback expires the ORM instance, and touching an expired
    # attribute from async test code is a lazy (sync) load.
    revision_id = revision.id
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision_id),
        headers={**admin, "If-Match": "1"},
        json={
            "pdu_input_voltage_v": 220.0,
            "outlets": [_outlet(1, stable_key="dup-a"), _outlet(1, stable_key="dup-b")],
        },
    )
    assert resp.status_code == 422, resp.text

    # The request-scoped session discards the failed transaction exactly like
    # `app.db.session.get_db` does on an exception path - nothing was committed.
    await db_session.rollback()
    fresh = await client.get(_url(revision_id), headers=await auth_headers("Viewer"))
    assert fresh.status_code == 200, fresh.text
    body = fresh.json()
    assert body["version"] == 1
    assert float(body["pdu_input_voltage_v"]) == 230.0
    assert body["outlets"] == []


# ------------------------------------------------------------------------ Lifecycle immutability


async def test_published_revision_cannot_be_updated(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(
        db_session, creator_id=await _creator(db_session, make_user), lifecycle_status="published"
    )
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"pdu_phase": "three", "outlets": [_outlet(1)]},
    )
    assert resp.status_code == 409, resp.text


async def test_retired_revision_cannot_be_updated(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(
        db_session, creator_id=await _creator(db_session, make_user), lifecycle_status="retired"
    )
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"pdu_phase": "three"},
    )
    assert resp.status_code == 409, resp.text


# ------------------------------------------------------------------------ Optimistic concurrency


async def test_missing_if_match_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(_url(revision.id), json={"pdu_phase": "three"}, headers=admin)
    assert resp.status_code == 428, resp.text


async def test_stale_if_match_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")

    stale = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "99"}, json={"pdu_phase": "three"}
    )
    assert stale.status_code == 409, stale.text

    ok = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "1"}, json={"pdu_phase": "three"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 2

    replay = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "1"}, json={"pdu_phase": "single"}
    )
    assert replay.status_code == 409, replay.text


# ------------------------------------------------------------------------ Cross-category isolation


async def test_rack_revision_is_not_exposed_and_designer_path_still_works(
    client: AsyncClient, auth_headers
):
    headers = await auth_headers("Administrator")

    mfr = await client.post(
        "/api/v1/catalog/manufacturers",
        json={"name": f"RackMfr-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert mfr.status_code == 201, mfr.text
    model = await client.post(
        "/api/v1/catalog/models",
        json={
            "manufacturer_id": mfr.json()["id"],
            "category": "rack",
            "model_name": f"Rack-{uuid.uuid4().hex[:8]}",
        },
        headers=headers,
    )
    assert model.status_code == 201, model.text
    draft = await client.post(
        f"/api/v1/catalog/models/{model.json()['id']}/revisions", headers=headers
    )
    assert draft.status_code == 201, draft.text
    rack_revision_id = draft.json()["id"]
    assert draft.json()["version"] == 1

    read = await client.get(f"/api/v1/catalog/revisions/{rack_revision_id}/pdu", headers=headers)
    assert read.status_code == 404, read.text

    write = await client.patch(
        f"/api/v1/catalog/revisions/{rack_revision_id}/pdu",
        headers={**headers, "If-Match": "1"},
        json={"pdu_phase": "three", "outlets": [_outlet(1)]},
    )
    assert write.status_code == 404, write.text

    # The existing rack designer path is untouched by the /pdu sub-resource: the same
    # revision still reads back and still accepts a normal designer PATCH.
    detail = await client.get(f"/api/v1/catalog/revisions/{rack_revision_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["lifecycle_status"] == "draft"
    assert detail.json()["version"] == 1

    patched = await client.patch(
        f"/api/v1/catalog/revisions/{rack_revision_id}",
        headers={**headers, "If-Match": "1"},
        json={
            "dimension_unit": "in",
            "width_value": 19.0,
            "height_value": 73.5,
            "depth_value": 39.4,
            "rack_unit_height": 42,
            "weight_unit": "lb",
            "weight_value": 220.0,
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2
