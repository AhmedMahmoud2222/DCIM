"""SEC (Codex PR #50 review, finding #1): the two independent halves of the
duplicate/concurrent-commit fix.

1. `commit_bulk_import_job_endpoint` (app/api/v1/bulk_import.py) claims a job into
   'committing' with a single atomic `UPDATE ... WHERE status='validated'` — two
   concurrent POST .../commit requests against the same validated job must see exactly
   one 202 and one 409, never two 202s. This is a true concurrency test against a real
   PostgreSQL instance with genuinely separate sessions per request (mirroring
   tests/integration/test_idempotency_concurrency.py's own `per_request_client` pattern
   and its stated reasoning for not using the shared `client`/`auth_headers` fixtures,
   which bind every request to one shared, not-concurrency-safe `db_session`).

2. `run_commit` (app/application/bulk_import/service.py) itself must be safe against a
   second Celery delivery of the *same* dispatched task landing after the job already
   reached a terminal status — verified directly at the service layer, simulating
   redelivery without needing two real concurrent callers."""

import asyncio
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.bulk_import import service as bulk_import_service
from app.domain.bulk_import.models import BulkImportJob
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job
from app.main import app
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model
from tests.conftest import TEST_DATABASE_URL


@pytest_asyncio.fixture
async def per_request_client(db_session):
    """See tests/integration/test_idempotency_concurrency.py's identical fixture for the
    full reasoning — a fresh AsyncSession per request (its own connection from its own
    pool), not the shared single `db_session` the `client` fixture binds every request
    to, so concurrent requests genuinely don't share a session."""
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


async def _login(ac: AsyncClient, email: str, password: str) -> dict:
    resp = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _auth_headers_factory(ac: AsyncClient, make_user):
    """A drop-in for the `auth_headers` fixture (tests/conftest.py) that works against
    `ac`/`per_request_client` instead of the shared `client` fixture, so the existing
    helper functions (create_room_with_codes, _create_rack_model) — which only call
    `client.post(...)`/`await auth_headers(role)` — work unmodified here."""

    async def _auth_headers(role_name: str = "Administrator") -> dict:
        email = f"user-{uuid.uuid4().hex[:8]}@example.com"
        await make_user(email, "correct horse battery staple", role_name)
        return await _login(ac, email, "correct horse battery staple")

    return _auth_headers


async def test_two_concurrent_commit_requests_exactly_one_succeeds(make_user, db_session, per_request_client):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        auth_headers = _auth_headers_factory(ac, make_user)
        headers = await auth_headers("Engineer")
        room = await create_room_with_codes(ac, auth_headers)
        model = await _create_rack_model(ac, auth_headers)
        asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

        content = build_workbook(
            RACK_HEADERS,
            [[asset_tag, "Row A Rack 1", model["manufacturer"], model["model_name"], "", room["site_code"],
              room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]],
        )
        upload = await ac.post(
            "/api/v1/racks/import-jobs?mode=create_only",
            files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
        )
        assert upload.status_code == 202, upload.text
        job = upload.json()

        parse_and_validate_bulk_import_job.run(job["id"])

        job_status = (await ac.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
        assert job_status["status"] == "validated", job_status

        results = await asyncio.gather(
            ac.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers),
            ac.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers),
        )
        status_codes = sorted(r.status_code for r in results)
        assert status_codes == [202, 409], f"expected exactly one 202 and one 409, got {[r.status_code for r in results]}"

        # No worker consumes the broker queue in this test environment — drain the one
        # dispatched commit task synchronously, exactly like the sequential tests in
        # tests/api/test_bulk_import_racks.py do.
        commit_bulk_import_job.run(job["id"])

        final = (await ac.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
        assert final["status"] == "committed", final
        assert final["committed_row_count"] == 1

        racks = (await ac.get("/api/v1/racks", headers=headers)).json()
        matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
        assert len(matching) == 1, "the row must be committed exactly once, not once per concurrent request"


async def test_run_commit_ignores_a_second_delivery_of_the_same_task(client, auth_headers, db_session):
    """Simulates a second Celery delivery of the identical `commit_bulk_import_job` task
    arriving after the first delivery already ran `run_commit` to completion — must be a
    clean no-op (service.py::run_commit's own `job.status != 'committing'` check), never
    a second pass over the same rows."""
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

    content = build_workbook(
        RACK_HEADERS,
        [[asset_tag, "Row A Rack 1", model["manufacturer"], model["model_name"], "", room["site_code"],
          room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]],
    )
    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    job = upload.json()
    parse_and_validate_bulk_import_job.run(job["id"])

    job_id = uuid.UUID(job["id"])
    # Simulate exactly what the API layer's atomic claim does (app/api/v1/bulk_import.py)
    # without going through HTTP, since this test only needs one claim followed by two
    # direct `run_commit` calls (simulated redelivery), not two racing HTTP requests —
    # that race is covered separately above.
    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing"
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    await bulk_import_service.run_commit(db_session, job_id)

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "committed", job_status
    assert job_status["committed_row_count"] == 1

    # Second "delivery" of the same task — must no-op, not reprocess the row.
    await bulk_import_service.run_commit(db_session, job_id)

    job_status_after = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status_after["status"] == "committed"
    assert job_status_after["committed_row_count"] == 1, "a second delivery must never double-count committed rows"

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
    assert len(matching) == 1, "a second delivery must never create a duplicate rack"

    audit_count = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
            {"id": str(job_id)},
        )
    ).scalar_one()
    assert audit_count == 1, "a second delivery must never write a duplicate job-level audit row"
