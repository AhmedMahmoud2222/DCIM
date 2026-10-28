"""Issue #150 acceptance-evidence: PDU-specific concurrency against real PostgreSQL, with every concurrent request on its
own AsyncSession/connection (the pattern of test_idempotency_concurrency.py - the shared-session `client` fixture is not
safe for concurrent use).

Covers: two simultaneous publishes of one PDU revision, a publish racing a draft outlet replacement, concurrent
instantiation with the same Idempotency-Key, and concurrent instantiation with the same asset tag under different keys,
including proof that the losing transaction left no partial PDU graph behind. Tests only - no runtime change."""

import asyncio
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.main import app
from tests.conftest import TEST_DATABASE_URL

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def per_request_client(db_session):
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=25, max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def _request(method: str, url: str, headers: dict | None = None, **kwargs):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.request(method, url, headers=headers, **kwargs)


async def _admin_headers(make_user) -> dict:
    user = await make_user(f"pdu-conc-{uuid.uuid4().hex[:8]}@example.com", PASSWORD, "Administrator")
    resp = await _request("POST", "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _outlet(number: int) -> dict:
    return {
        "stable_key": f"outlet-{number}", "outlet_number": number, "outlet_type": "iec_c13", "outlet_count": 1,
        "phase": "single", "rated_voltage_v": 230.0, "rated_current_a": 10.0, "side": "front", "sort_order": number,
    }


async def _make_pdu_revision(headers: dict, *, publish: bool, outlets: int = 2) -> dict:
    manufacturer = await _request(
        "POST", "/api/v1/catalog/manufacturers", headers=headers, json={"name": f"PduConc-{uuid.uuid4().hex[:8]}"}
    )
    assert manufacturer.status_code == 201, manufacturer.text
    model = await _request(
        "POST", "/api/v1/catalog/models", headers=headers,
        json={"manufacturer_id": manufacturer.json()["id"], "category": "pdu", "model_name": f"PduConc-{uuid.uuid4().hex[:8]}"},
    )
    assert model.status_code == 201, model.text
    revision = await _request("POST", f"/api/v1/catalog/models/{model.json()['id']}/revisions", headers=headers)
    assert revision.status_code == 201, revision.text
    revision = revision.json()
    patched = await _request(
        "PATCH", f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        headers={**headers, "If-Match": str(revision["version"])},
        json={
            "pdu_input_voltage_v": 400.0, "pdu_input_current_a": 32.0, "pdu_phase": "three",
            "mounting_orientation": "vertical", "outlets": [_outlet(n) for n in range(1, outlets + 1)],
        },
    )
    assert patched.status_code == 200, patched.text
    revision = patched.json()
    if publish:
        published = await _request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
        assert published.status_code == 200, published.text
        revision = published.json()
    return revision


async def _instantiate(headers: dict, revision_id: str, *, tag: str, key: str | None = None):
    h = {**headers, **({"Idempotency-Key": key} if key else {})}
    return await _request(
        "POST", "/api/v1/power/pdus/instantiate", headers=h,
        json={"asset_tag": tag, "name": "conc-pdu", "catalog_model_revision_id": revision_id},
    )


async def _count(db_session, sql: str, **params) -> int:
    await db_session.rollback()  # end any stale snapshot; the test session only reads
    params = {k: uuid.UUID(v) if isinstance(v, str) and len(v) == 36 else v for k, v in params.items()}
    return (await db_session.execute(text(sql), params)).scalar_one()


async def test_concurrent_publish_of_one_pdu_revision_has_exactly_one_winner(make_user, db_session, per_request_client):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=False)

    results = await asyncio.gather(
        *[_request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers) for _ in range(5)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1, f"expected exactly one successful publish, got {statuses}"
    assert all(s == 409 for s in statuses if s != 200), f"losers must be a deterministic 409, got {statuses}"

    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'catalog.revision.publish' AND entity_id = :r",
        r=revision["id"],
    ) == 1, "exactly one publish audit row"
    assert await _count(
        db_session, "SELECT count(*) FROM pdu_outlet_template WHERE catalog_model_revision_id = :r", r=revision["id"]
    ) == 2, "outlet templates untouched by the losing publishes"
    # a PDU never gets a legacy bridge, even under a publish race
    assert await _count(
        db_session,
        "SELECT (SELECT count(*) FROM rack_model_revision WHERE bridged_from_catalog_revision_id = :r) "
        "+ (SELECT count(*) FROM equipment_model_revision WHERE bridged_from_catalog_revision_id = :r)",
        r=uuid.UUID(revision["id"]),
    ) == 0


async def test_publish_racing_a_draft_outlet_replacement_never_yields_a_mixed_published_revision(
    make_user, db_session, per_request_client
):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=False, outlets=2)

    patch = _request(
        "PATCH", f"/api/v1/catalog/revisions/{revision['id']}/pdu",
        headers={**headers, "If-Match": str(revision["version"])},
        json={"outlets": [_outlet(n) for n in range(1, 5)]},
    )
    publish = _request("POST", f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    patch_resp, publish_resp = await asyncio.gather(patch, publish)

    assert publish_resp.status_code in (200, 409), publish_resp.text
    assert patch_resp.status_code in (200, 409), patch_resp.text
    # The only forbidden outcome is a published revision whose outlets were changed after (or during) publication.
    # Publish first -> the patch must be rejected and 2 outlets remain; patch first -> publish sees the 4.
    outlets = await _count(
        db_session, "SELECT count(*) FROM pdu_outlet_template WHERE catalog_model_revision_id = :r", r=revision["id"]
    )
    status = (await db_session.execute(
        text("SELECT lifecycle_status FROM catalog_model_revision WHERE id = :r"), {"r": revision["id"]}
    )).scalar_one()
    if patch_resp.status_code == 200:
        assert outlets == 4
    else:
        assert outlets == 2 and patch_resp.status_code == 409 and status == "published"
    assert status in ("draft", "published")
    assert (status == "published") == (publish_resp.status_code == 200)


async def test_concurrent_instantiate_with_one_idempotency_key_creates_one_pdu_graph(
    make_user, db_session, per_request_client
):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=True, outlets=3)
    tag, key = f"PDU-IDEM-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())

    results = await asyncio.gather(*[_instantiate(headers, revision["id"], tag=tag, key=key) for _ in range(8)])
    statuses = [r.status_code for r in results]
    # Every request either wins (201), replays the cached 201, or is told to retry (503 still-processing); never a 409/500.
    assert all(s in (201, 503) for s in statuses), f"unexpected statuses {statuses}"
    assert statuses.count(201) >= 1
    assert len({r.json()["id"] for r in results if r.status_code == 201}) == 1, "one distinct PDU node id"

    retry = await _instantiate(headers, revision["id"], tag=tag, key=key)
    assert retry.status_code == 201 and retry.json()["id"] == results[statuses.index(201)].json()["id"]

    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 1
    asset_id = (await db_session.execute(text("SELECT id FROM managed_asset WHERE asset_tag = :t"), {"t": tag})).scalar_one()
    assert await _count(db_session, "SELECT count(*) FROM pdu WHERE id = :a", a=asset_id) == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet WHERE pdu_asset_id = :a", a=asset_id) == 3
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE owning_asset_id = :a", a=asset_id) == 3
    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate' AND entity_id = :a", a=asset_id
    ) == 1


async def test_concurrent_instantiate_with_same_tag_and_different_keys_leaves_no_partial_graph(
    make_user, db_session, per_request_client
):
    headers = await _admin_headers(make_user)
    revision = await _make_pdu_revision(headers, publish=True, outlets=2)
    tag = f"PDU-TAG-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        *[_instantiate(headers, revision["id"], tag=tag, key=str(uuid.uuid4())) for _ in range(6)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1, f"exactly one instantiation may win the asset tag, got {statuses}"
    assert all(s == 409 for s in statuses if s != 201), f"losers must be a deterministic 409 (no 500), got {statuses}"

    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_tag = :t", t=tag) == 1
    # Whole-database invariants: the losers rolled back completely - every PDU asset has its subtype row and exactly the
    # templated outlets, and no outlet or outlet node is orphaned.
    assert await _count(db_session, "SELECT count(*) FROM managed_asset WHERE asset_type = 'pdu'") == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu") == 1
    assert await _count(db_session, "SELECT count(*) FROM pdu_outlet") == 2
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE node_type = 'pdu'") == 1
    assert await _count(db_session, "SELECT count(*) FROM power_node WHERE node_type = 'pdu_outlet'") == 2
    assert await _count(
        db_session, "SELECT count(*) FROM audit_log WHERE action = 'power.pdu.instantiate'"
    ) == 1
    assert await _count(
        db_session, "SELECT count(*) FROM outbox_event WHERE event_type = 'PDUInstantiated'"
    ) == 1
