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
import io
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application.bulk_import import service as bulk_import_service
from app.application.bulk_import.limits import BULK_IMPORT_COMMIT_MAX_ATTEMPTS
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.tasks import bulk_import as bulk_import_tasks
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


# ------------------------------------------------------------------------ ROUND 2 finding #1


async def test_two_genuinely_overlapping_run_commit_deliveries_only_one_processes_rows(
    client, auth_headers, db_session, monkeypatch,
):
    """SEC (Codex PR #50 review, ROUND 2, finding #1): unlike
    test_run_commit_ignores_a_second_delivery_of_the_same_task above (two *sequential*
    calls, the second only starting after the first has already finished), this proves two
    *genuinely overlapping* deliveries are safe -- two independent AsyncSessions (simulating
    two separate Celery worker processes racing each other) both calling
    service.run_commit for the identical job via asyncio.gather. Before the lease fix,
    run_commit's initial `SELECT ... FOR UPDATE` + status-check + immediate `db.commit()`
    released the row lock right after checking status='committing' (not a one-shot-consumed
    state), so a second, overlapping delivery could pass that same check and both deliveries
    would then process the same batch of valid rows concurrently. The atomic lease claim
    (a single `UPDATE ... WHERE status='committing' AND (no unexpired lease)`) is what
    Postgres itself serializes: whichever delivery's claim UPDATE gets there first wins the
    row lock; the other's claim UPDATE blocks, then re-evaluates its WHERE clause against
    the now-committed (lease-held) row and matches zero rows -- a clean, immediate no-op,
    never a second pass over the rows."""
    monkeypatch.setattr(bulk_import_service, "BULK_IMPORT_COMMIT_BATCH_SIZE", 2)

    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    tags = [f"RACK-{uuid.uuid4().hex[:8]}" for _ in range(5)]
    rows = [
        [tag, f"Row {i} Rack", model["manufacturer"], model["model_name"], "", room["site_code"],
         room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]
        for i, tag in enumerate(tags)
    ]
    content = build_workbook(RACK_HEADERS, rows)
    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    job = upload.json()
    parse_and_validate_bulk_import_job.run(job["id"])
    job_id = uuid.UUID(job["id"])

    # Claim the job into 'committing' exactly like the real API endpoint's atomic claim —
    # this test exercises the two *overlapping run_commit deliveries* race directly, not
    # the "two racing HTTP requests" race (already covered above).
    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing"
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    # Capture the emitted structured JSON from service.py's own logger, not only mock call
    # args, so we can directly assert the losing delivery's log event.
    log_output = io.StringIO()
    captured_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output), processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(bulk_import_service, "logger", captured_logger)

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with session_factory() as session_a, session_factory() as session_b:
            await asyncio.gather(
                bulk_import_service.run_commit(session_a, job_id),
                bulk_import_service.run_commit(session_b, job_id),
            )
    finally:
        await engine.dispose()

    log_lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line.strip()]
    not_acquired = [line for line in log_lines if line.get("event") == "bulk_import_commit_lease_not_acquired"]
    assert len(not_acquired) == 1, (
        f"expected exactly one delivery to lose the lease claim and log immediately, got {not_acquired}"
    )

    final = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert final["status"] == "committed", final
    assert final["committed_row_count"] == 5, "every row must be committed exactly once, by exactly one delivery"

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    for tag in tags:
        matching = [r for r in racks["items"] if r["asset_tag"] == tag]
        assert len(matching) == 1, f"{tag} must have been committed exactly once, never twice by two overlapping deliveries"


async def test_run_commit_reclaims_an_expired_lease_after_a_crashed_delivery(client, auth_headers, db_session):
    """SEC (Codex PR #50 review, ROUND 2, finding #1): a delivery that claimed the commit
    lease and then crashed (worker killed, container OOM'd) mid-run leaves the job stuck at
    status='committing' with a lease that will never be renewed again. A later call to
    run_commit (redelivery, or an operator-triggered retry) must be able to reclaim that
    lease once it has expired -- rather than being permanently stuck because the stale
    lease looks "held" forever."""
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

    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing"
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    # Simulate a delivery that claimed the lease and then crashed without ever renewing or
    # finishing: an already-expired lease left behind on the job row.
    stale_lease_id = uuid.uuid4()
    await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id).values(
            commit_lease_id=stale_lease_id, commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    )
    await db_session.commit()

    await bulk_import_service.run_commit(db_session, job_id)

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "committed", job_status
    assert job_status["committed_row_count"] == 1, "the recovering delivery must successfully finish the job"

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
    assert len(matching) == 1


# ------------------------------------------------------------------------ ROUND 3 finding #1


async def test_sweeper_requeues_and_the_redelivered_task_actually_finishes_a_crashed_job(
    client, auth_headers, db_session, monkeypatch,
):
    """SEC (Codex PR #50 review, ROUND 3, finding #1): the earlier crash-recovery test
    (test_run_commit_reclaims_an_expired_lease_after_a_crashed_delivery) only proves
    run_commit *can* reclaim an expired lease if something calls it again — it doesn't
    prove anything ever would, operationally, given Celery's default early acknowledgment
    (a worker that dies mid-task is never redelivered by the broker on its own). This test
    exercises the actual recovery path end to end: a job is left stuck in 'committing'
    with an expired lease (simulating a crashed delivery), the bounded sweeper
    (requeue_stuck_bulk_import_commits) is invoked exactly as Celery-beat would invoke it,
    and the job must actually reach a terminal 'committed' status as a result — not via a
    second direct call to run_commit made by the test itself.

    `commit_bulk_import_job.delay` is monkeypatched to run the task inline instead of
    publishing to Redis (there's no worker consuming the broker in this test environment,
    same reasoning tests/api/test_bulk_import_racks.py's `_run_import`-style helpers use
    for `.run()` throughout this suite) — this still proves the sweeper calls `.delay()`
    with the right job id and that the redelivered task genuinely finishes the job, which
    is exactly the gap the prior test left open."""
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

    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing"
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    # A crashed delivery: an already-expired lease left behind, and — unlike the earlier
    # crash-recovery test — nothing in this test calls run_commit directly afterward.
    await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id).values(
            commit_lease_id=uuid.uuid4(), commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    )
    await db_session.commit()

    dispatched_job_ids: list[str] = []

    def _fake_delay(job_id_str: str) -> None:
        dispatched_job_ids.append(job_id_str)
        commit_bulk_import_job.run(job_id_str)

    monkeypatch.setattr(commit_bulk_import_job, "delay", _fake_delay)

    bulk_import_tasks.requeue_stuck_bulk_import_commits()

    assert dispatched_job_ids == [str(job_id)], "the sweeper must dispatch exactly the one stuck job, by its real id"

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "committed", job_status
    assert job_status["committed_row_count"] == 1

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
    assert len(matching) == 1


async def test_sweeper_does_not_touch_a_job_whose_lease_is_still_live(client, auth_headers, db_session, monkeypatch):
    """The sweeper's own query (status='committing' AND lease expired) must never select a
    job whose owning delivery is still healthy and within its lease window — re-dispatching
    a live delivery's job would be wasteful at best; this proves it simply doesn't happen."""
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

    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing", commit_lease_id=uuid.uuid4(),
            commit_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(commit_bulk_import_job, "delay", lambda job_id_str: dispatched.append(job_id_str))

    bulk_import_tasks.requeue_stuck_bulk_import_commits()

    assert dispatched == [], "a job with a live, unexpired lease must never be requeued by the sweeper"


# ------------------------------------------------------------------------ ROUND 3 finding #2


async def test_a_stale_deliverys_fallback_never_corrupts_the_replacement_deliverys_job(
    client, auth_headers, db_session,
):
    """SEC (Codex PR #50 review, ROUND 3, finding #2): the exact interleaving the review
    described. Delivery A claims the commit lease (lease_id_A). A's lease then expires
    while A is merely stalled, not dead. Delivery B reclaims the lease and runs the job to
    completion (status='committed'). A finally wakes up and, unaware it was ever
    superseded, hits its own unexpected error and tries to run its fallback with its own
    original lease_id_A. Before this fix, that fallback wrote unconditionally — marking
    B's already-committed row 'failed' and flipping the finished job back to
    'committed_with_errors'. This proves the fenced fallback
    (service.py::_mark_commit_failed_if_still_owner) is a clean no-op instead: B's
    finished job/row/audit state must be completely untouched by A's late fallback."""
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

    # Delivery A claims the lease (simulating the real atomic claim run_commit itself would
    # perform, but capturing lease_id_A so this test can later call the fenced fallback
    # exactly as A itself would, with A's own — by then stale — token).
    lease_id_a = uuid.uuid4()
    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing", commit_lease_id=lease_id_a,
            commit_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    # A's lease expires (A is stalled, not dead) -- then delivery B reclaims and finishes
    # the job for real via the normal run_commit path.
    await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id).values(
            commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    await db_session.commit()
    await bulk_import_service.run_commit(db_session, job_id)

    finished = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert finished["status"] == "committed", finished
    assert finished["committed_row_count"] == 1

    audit_count_before = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
            {"id": str(job_id)},
        )
    ).scalar_one()
    assert audit_count_before == 1

    # A finally wakes up and tries its own fallback with its own, by-now-stale lease_id_a.
    await bulk_import_service._mark_commit_failed_if_still_owner(db_session, job_id, lease_id_a)

    # B's finished job must be completely untouched by A's late, unauthorized fallback.
    unchanged = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert unchanged["status"] == "committed", "a stale delivery's fallback must never flip a finished job's status"
    assert unchanged["failed_row_count"] == 0, "a stale delivery's fallback must never mark the replacement's rows failed"

    committed_row = (
        await db_session.execute(select(BulkImportRow).where(BulkImportRow.job_id == job_id))
    ).scalar_one()
    assert committed_row.status == "committed", "the replacement delivery's committed row must not be reverted to failed"

    audit_count_after = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
            {"id": str(job_id)},
        )
    ).scalar_one()
    assert audit_count_after == audit_count_before, "a skipped fallback must never write its own duplicate audit row"


# ------------------------------------------------------------------------ ROUND 4 blocker 1


async def test_run_commit_bounds_retries_for_a_job_that_crashes_on_every_delivery(
    client, auth_headers, db_session, monkeypatch,
):
    """SEC (Codex PR #50 review, ROUND 4, blocker 1): the crash-recovery sweeper
    (requeue_stuck_bulk_import_commits) re-dispatches any job stuck 'committing' past its
    lease expiry, unconditionally, every sweep. Without a per-job cap, a job whose commit
    genuinely crashes the worker process on *every* delivery (a poison-pill row that OOMs
    or segfaults, not an ordinary Python exception — those are already caught and finalized
    on the very first attempt by run_commit's own except block / fenced fallback) would be
    reclaimed and re-crashed forever at the sweeper's fixed interval, never reaching a
    terminal status.

    Simulates a real crash — as opposed to an ordinary exception — by making
    `_run_commit_owned` raise `asyncio.CancelledError`, a `BaseException` subclass that
    `run_commit`'s own `except Exception` does NOT catch, so it propagates out exactly like
    a killed process would (no fallback ever runs; the claim's own writes, already
    committed, are all that persists). Each iteration then force-expires the lease exactly
    as a real elapsed lease-duration would, standing in for the sweeper's next sweep.

    After `BULK_IMPORT_COMMIT_MAX_ATTEMPTS` such crashes, the next claim must stop
    retrying and finalize the job as `committed_with_errors` instead of crashing again —
    proving the retry loop is bounded, not just that a single crash can be recovered from
    (already covered by the two crash-recovery tests above)."""
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

    claim = await db_session.execute(
        update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
            status="committing"
        )
    )
    assert claim.rowcount == 1
    await db_session.commit()

    async def _simulated_crash(*args, **kwargs):
        raise asyncio.CancelledError("simulated worker crash: no exception handler ever runs")

    monkeypatch.setattr(bulk_import_service, "_run_commit_owned", _simulated_crash)

    for _ in range(BULK_IMPORT_COMMIT_MAX_ATTEMPTS):
        with pytest.raises(asyncio.CancelledError):
            await bulk_import_service.run_commit(db_session, job_id)
        # Stand in for real time passing until the lease naturally expires, exactly as the
        # sweeper's own query would see it on its next scheduled sweep.
        await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id).values(
                commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db_session.commit()

    attempts_before_final = (
        await db_session.execute(select(BulkImportJob.commit_attempt_count).where(BulkImportJob.id == job_id))
    ).scalar_one()
    assert attempts_before_final == BULK_IMPORT_COMMIT_MAX_ATTEMPTS

    # The (MAX_ATTEMPTS + 1)-th claim must NOT crash again -- it must finalize the job.
    await bulk_import_service.run_commit(db_session, job_id)

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "committed_with_errors", job_status
    assert job_status["failed_row_count"] == 1, job_status
    assert job_status["committed_row_count"] == 0, "the perpetually-crashing row must never be reported as committed"

    row = (await db_session.execute(select(BulkImportRow).where(BulkImportRow.job_id == job_id))).scalar_one()
    assert row.status == "failed"
    assert any("repeated attempts" in e["message"] for e in row.errors)

    # A job that has already been finalized must never be reselected by the sweeper again
    # -- it is no longer 'committing', regardless of how its lease looks.
    dispatched: list[str] = []
    monkeypatch.setattr(commit_bulk_import_job, "delay", lambda job_id_str: dispatched.append(job_id_str))
    bulk_import_tasks.requeue_stuck_bulk_import_commits()
    assert dispatched == [], "a finalized (attempts-exhausted) job must never be re-swept"
