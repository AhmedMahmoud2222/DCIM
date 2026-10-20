"""Issue #150 M2: the narrow `/catalog/revisions/{id}/pdu` draft API, end to end against
real PostgreSQL.

Every test hits the HTTP boundary through `client` with real `make_user`/`auth_headers`
principals (this repo's `test_catalog_designer_lifecycle.py` convention) - no mocked
persistence, no mocked authorization. PDU-category models here are seeded through the ORM
for fixture-scope reasons (the M3 suite `test_pdu_catalog_publish.py` covers the real
`POST /catalog/models` path with `category='pdu'`); rack models go through the real
designer endpoints so existing catalog behavior is exercised alongside the new `/pdu`
sub-resource."""

import asyncio
import json
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_db
from app.domain.audit.models import AuditLog
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer
from app.domain.outbox.models import OutboxEvent
from app.main import app

PASSWORD = "correct horse battery staple"


def _validation_errors(body: dict) -> list[dict]:
    """The problem+json response carries the ValidationSummary as a JSON string in
    `detail`, not as a top-level `errors` key."""
    detail = json.loads(body["detail"])
    return detail["errors"]


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


async def test_viewer_cannot_read_a_draft(client: AsyncClient, db_session, make_user, auth_headers):
    """A Viewer holds catalog:read but not catalog:read_draft, so reading a PDU draft
    through the /pdu sub-resource must be refused - mirroring
    GET /catalog/revisions/{id}'s conditional gate (spec §10)."""
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    headers = await auth_headers("Viewer")
    resp = await client.get(_url(revision.id), headers=headers)
    assert resp.status_code == 403, resp.text


async def test_administrator_can_read_a_draft(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    headers = await auth_headers("Administrator")
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


async def test_viewer_reading_a_published_pdu_revision_returns_read_only_spec(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """Published/retired revisions use plain catalog:read, and the /pdu sub-resource now
    returns their spec read-only (R6) instead of 409."""
    revision = await _seed_pdu_revision(
        db_session, creator_id=await _creator(db_session, make_user), lifecycle_status="published"
    )
    headers = await auth_headers("Viewer")
    resp = await client.get(_url(revision.id), headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lifecycle_status"] == "published"
    assert float(body["pdu_input_voltage_v"]) == 230.0


async def test_nonexistent_revision_read_is_404_not_403(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """Direct-ID access to an unknown revision is a 404 (category gate), never a
    permission-derived 403/200 that could probe for existence."""
    headers = await auth_headers("Viewer")
    resp = await client.get(_url(uuid.uuid4()), headers=headers)
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------------ Validation (R3)


async def test_voltage_zero_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "1"}, json={"pdu_input_voltage_v": 0.0}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert any(e["field"] == "pdu_input_voltage_v" and e["code"] == "invalid_voltage" for e in _validation_errors(body))


async def test_voltage_negative_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "1"}, json={"pdu_input_voltage_v": -1.0}
    )
    assert resp.status_code == 422, resp.text


async def test_overflowing_voltage_is_a_422_not_a_500(client: AsyncClient, db_session, make_user, auth_headers):
    """Overflowing numeric input must produce a controlled validation error, never an
    HTTP 500 from a PostgreSQL numeric_overflow."""
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id), headers={**admin, "If-Match": "1"}, json={"pdu_input_voltage_v": 999999.0}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert any(e["field"] == "pdu_input_voltage_v" for e in _validation_errors(body))


async def test_outlet_number_zero_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"outlets": [_outlet(0, stable_key="zero")]},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert any(
        e["field"] == "outlets[0].outlet_number" and e["code"] == "invalid_outlet_number" for e in _validation_errors(body)
    )


async def test_outlet_number_negative_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"outlets": [_outlet(-3, stable_key="neg")]},
    )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------------ R6: remaining PDU attributes


async def test_draft_update_and_read_includes_rated_capacity_plug_and_monitoring(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """pdu_rated_capacity_kw, pdu_plug_type and pdu_monitoring_* flow through draft
    validation, persistence and the read surface (R6c)."""
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={
            "pdu_rated_capacity_kw": 4.8,
            "pdu_plug_type": "iec_309",
            "pdu_monitoring_protocol": "snmp",
            "pdu_monitoring_endpoint": "https://pdu-01.example.com/metrics",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert float(body["pdu_rated_capacity_kw"]) == 4.8
    assert body["pdu_plug_type"] == "iec_309"
    assert body["pdu_monitoring_protocol"] == "snmp"
    assert body["pdu_monitoring_endpoint"] == "https://pdu-01.example.com/metrics"

    # Read back through a fresh authenticated request.
    fresh = await client.get(_url(revision.id), headers=await auth_headers("Administrator"))
    assert fresh.status_code == 200, fresh.text
    assert float(fresh.json()["pdu_rated_capacity_kw"]) == 4.8


async def test_monitoring_protocol_requires_endpoint_and_vice_versa(
    client: AsyncClient, db_session, make_user, auth_headers
):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")

    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"pdu_monitoring_protocol": "snmp"},
    )
    assert resp.status_code == 422, resp.text

    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"pdu_monitoring_endpoint": "https://example.com/metrics"},
    )
    assert resp.status_code == 422, resp.text


async def test_negative_rated_capacity_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")
    resp = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"pdu_rated_capacity_kw": -1.0},
    )
    assert resp.status_code == 422, resp.text


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
    fresh = await client.get(_url(revision.id), headers=await auth_headers("Administrator"))
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
    fresh = await client.get(_url(revision_id), headers=await auth_headers("Administrator"))
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


async def test_repeated_draft_edits_reuse_outlet_numbers(client: AsyncClient, db_session, make_user, auth_headers):
    """Repeated edits of the same draft must succeed even when a later generation re-uses
    outlet numbers from the previous one - no transient uniqueness violations on
    uq_pdu_outlet_template_cmr_on."""
    revision = await _seed_pdu_revision(db_session, creator_id=await _creator(db_session, make_user))
    admin = await auth_headers("Administrator")

    first = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "1"},
        json={"outlets": [_outlet(1), _outlet(2)]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    # Second generation re-uses outlet_number=1 (and 2) from the first.
    second = await client.patch(
        _url(revision.id),
        headers={**admin, "If-Match": "2"},
        json={"outlets": [_outlet(1, outlet_type="iec_c19"), _outlet(2)]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 3
    assert [(o["outlet_number"], o["outlet_type"]) for o in second.json()["outlets"]] == [
        (1, "iec_c19"),
        (2, "iec_c13"),
    ]


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


# ------------------------------------------------------------------------ Concurrent edits (P1)


async def test_concurrent_draft_edits_one_wins_one_conflicts(
    client: AsyncClient,
    db_session,
    make_user,
    auth_headers,
    db_engine,
):
    """Two concurrent PATCH requests to the same PDU draft: serialized by the per-revision
    row lock (`lock_draft_revision_for_edit` with `populate_existing=True`).

    Reproduces the lost-update scenario:
      1. Session A and Session B both load revision version N into their own sessions.
      2. Session B commits first, advancing the revision to N+1.
      3. Session A requests the lock (blocked until B commits, then refreshed via
         `populate_existing=True`), sees version N+1, and fails its `If-Match: N` check
         with HTTP 409 instead of silently overwriting Session B's changes.
      4. Exactly one audit/outbox event is written (the winner's); the loser writes none.

    Without `populate_existing=True` on the locked `db.get`, SQLAlchemy returns the stale
    identity-map copy (version N) instead of refreshing the locked row, so both requests
    would pass the version check and the last commit would silently win (lost update).
    """
    revision = await _seed_pdu_revision(
        db_session, creator_id=await _creator(db_session, make_user), version=1
    )
    admin_headers = await auth_headers("Administrator")

    # Each HTTP request gets its own DB session, so the two requests below are genuine
    # concurrent sessions (Session A and Session B) rather than two coroutines sharing one.
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def per_request_session():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = per_request_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            first, second = await asyncio.gather(
                c.patch(
                    _url(revision.id),
                    headers={**admin_headers, "If-Match": "1"},
                    json={"pdu_phase": "three"},
                ),
                c.patch(
                    _url(revision.id),
                    headers={**admin_headers, "If-Match": "1"},
                    json={"pdu_phase": "single"},
                ),
            )
        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 409], statuses

        # The winner's phase persisted; exactly one audit + outbox event was written.
        # Read back from a fresh session because db_session holds a pre-concurrency
        # identity-map copy (expire_on_commit=False) and its .get() would return the
        # cached stale object without hitting the database.
        async with async_sessionmaker(bind=db_engine, expire_on_commit=False)() as fresh:
            final = await fresh.get(CatalogModelRevision, revision.id)
            assert final.version == 2
            assert final.pdu_phase in ("three", "single")

            audits = (
                await fresh.execute(
                    select(AuditLog).where(AuditLog.action == "catalog.pdu_draft.update")
                )
            ).scalars().all()
            assert len(audits) == 1

            outboxes = (
                await fresh.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "PDUDraftUpdated")
                )
            ).scalars().all()
            assert len(outboxes) == 1

        audits = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "catalog.pdu_draft.update")
            )
        ).scalars().all()
        assert len(audits) == 1

        outboxes = (
            await db_session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "PDUDraftUpdated")
            )
        ).scalars().all()
        assert len(outboxes) == 1
    finally:
        app.dependency_overrides.clear()
