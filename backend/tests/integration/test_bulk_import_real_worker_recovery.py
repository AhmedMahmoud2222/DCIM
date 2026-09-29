"""SEC (Codex PR #50 review, ROUND 4, blocker 1): the crash-recovery sweeper
(requeue_stuck_bulk_import_commits) and its bounded-retry fix
(app/application/bulk_import/service.py::run_commit's commit_attempt_count check) were
previously only proven via direct function calls (test_bulk_import_commit_concurrency.py's
`test_sweeper_requeues_and_the_redelivered_task_actually_finishes_a_crashed_job` invokes
`requeue_stuck_bulk_import_commits()` directly and monkeypatches `commit_bulk_import_job
.delay` to run inline) — which proves the *code path* is correct but not that the
*deployed wiring* (a real Celery worker consuming the real Redis broker this pipeline is
actually configured against, app/infrastructure/celery_app.py) ever invokes it.

This module closes that gap with `celery.contrib.testing.worker.start_worker`: a real
embedded Celery worker thread that consumes tasks from the *actual* configured broker
(REDIS_URL) via the *actual* `.delay()` dispatch mechanism -- no `.run()` (which calls a
task's body directly, bypassing Celery entirely) and no monkeypatched `.delay`. The worker
is started BEFORE the upload, not after, so the upload endpoint's own real
`parse_and_validate_bulk_import_job.delay(...)` call (app/api/v1/racks.py) is what actually
parses the job -- exactly as it would in production -- rather than this test calling `.run()`
itself and leaving that real dispatch to sit unconsumed in the broker (which, empirically,
gets consumed out of order the moment a worker *does* start later in the test and re-runs
parse-and-validate over this test's own subsequent status manipulation -- caught during this
module's own development). The sweeper task is then dispatched for real too; the real worker
executing it calls `commit_bulk_import_job.delay(...)` for real, dispatching a *second* real
task onto the same broker that the same real worker also picks up and executes -- proving the
full two-hop dispatch chain (`requeue_stuck_bulk_import_commits` -> `commit_bulk_import_job`)
that this pipeline depends on operationally, not just the Python functions each task happens
to wrap.

Deliberately not e2e/Playwright-scoped: the browser-e2e CI job (see PR #50's earlier CI
review comment) already proves this at the docker-compose level, but only exercises one
happy-path upload -- it never kills a worker mid-commit. This module owns exactly that gap:
a real worker/broker/database recovery test that pytest can run directly."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import celery.contrib.testing.tasks  # noqa: F401  -- registers 'celery.ping' for start_worker's ping check
import pytest
from celery.contrib.testing.worker import start_worker
from sqlalchemy import update

from app.domain.bulk_import.models import BulkImportJob
from app.infrastructure.celery_app import celery_app
from app.infrastructure.tasks.bulk_import import requeue_stuck_bulk_import_commits
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model

pytestmark = pytest.mark.asyncio


async def _poll_until(client, headers, job_id: str, *, terminal_statuses: set[str], timeout_seconds: float = 20.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_status: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        last_status = (await client.get(f"/api/v1/import-jobs/{job_id}", headers=headers)).json()
        if last_status["status"] in terminal_statuses:
            return last_status
        await asyncio.sleep(0.25)
    raise AssertionError(f"job {job_id} never reached {terminal_statuses} within {timeout_seconds}s; last seen: {last_status}")


async def _upload_single_row_rack(client, headers, room, model) -> tuple[dict, str]:
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
    assert upload.status_code == 202, upload.text
    return upload.json(), asset_tag


async def test_real_worker_and_broker_recover_a_crashed_commit_via_the_sweeper_task(client, auth_headers, db_session):
    """A job is left stuck 'committing' with an expired lease (simulating a worker that
    died mid-commit, exactly as a real SIGKILL/OOM would leave it -- see
    app/infrastructure/tasks/bulk_import.py's own module docstring). The sweeper task is
    dispatched via a genuine `.delay()` call onto the real Redis broker; a real embedded
    Celery worker (not this test process calling functions directly) picks it up, and that
    worker's own execution of `requeue_stuck_bulk_import_commits` dispatches
    `commit_bulk_import_job` for the stuck job via `.delay()` too -- a second real dispatch
    that the same real worker also consumes and executes to completion. Nothing in this test
    calls `run_commit`, `.run()`, or the sweeper function directly; the only things this test
    does are: upload (whose own real dispatch the running worker parses), one real crash
    simulation via direct DB manipulation, one real `.delay()` call, and a bounded poll."""
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)

    with start_worker(celery_app, pool="solo", perform_ping_check=True, loglevel="info"):
        job, asset_tag = await _upload_single_row_rack(client, headers, room, model)
        job_id = uuid.UUID(job["id"])

        # The worker started above is what actually parses/validates this job -- via the
        # upload endpoint's own real `.delay()` call, not a direct `.run()` from this test.
        validated = await _poll_until(client, headers, job["id"], terminal_statuses={"validated"}, timeout_seconds=10.0)
        assert validated["valid_row_count"] == 1, validated

        # Simulate a crashed delivery: claimed the job into 'committing', took out a lease,
        # and then died (SIGKILL/OOM) before ever renewing it or finishing -- exactly the
        # scenario Celery's own default (early) acknowledgment means no broker-level
        # redelivery will ever recover on its own (bulk_import.py's module docstring).
        claim = await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
                status="committing", commit_lease_id=uuid.uuid4(),
                commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )
        assert claim.rowcount == 1
        await db_session.commit()

        # The one and only recovery trigger this test issues: a real dispatch onto the real
        # broker. Everything from here on is the actual deployed wiring: Celery beat would
        # normally be what calls this on a 30s schedule (see celery_app.py's beat_schedule)
        # -- this test proves what happens once it does, without needing to wait out a real
        # 30s beat tick (covered separately, at the process level, by this same directory's
        # celery-beat-subprocess test).
        requeue_stuck_bulk_import_commits.delay()

        final = await _poll_until(
            client, headers, job["id"], terminal_statuses={"committed", "committed_with_errors"}, timeout_seconds=20.0,
        )

    assert final["status"] == "committed", final
    assert final["committed_row_count"] == 1

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
    assert len(matching) == 1, "the crashed job's row must be committed exactly once by the real worker"


async def test_real_worker_leaves_a_healthy_committing_job_alone(client, auth_headers, db_session):
    """The sweeper must never touch a job whose lease is still live, even when driven by a
    real worker/broker rather than a direct function call -- a job mid-processing by a
    healthy delivery must not be redispatched (which would race a second real worker
    execution against the still-running first one)."""
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)

    with start_worker(celery_app, pool="solo", perform_ping_check=True, loglevel="info"):
        job, _asset_tag = await _upload_single_row_rack(client, headers, room, model)
        job_id = uuid.UUID(job["id"])
        await _poll_until(client, headers, job["id"], terminal_statuses={"validated"}, timeout_seconds=10.0)

        claim = await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
                status="committing", commit_lease_id=uuid.uuid4(),
                commit_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        assert claim.rowcount == 1
        await db_session.commit()

        result = requeue_stuck_bulk_import_commits.delay()
        result.get(timeout=10.0)

        # No terminal transition should have happened -- the sweeper's own query must have
        # found nothing to requeue, so the job is exactly where the live delivery left it.
        still_committing = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
        assert still_committing["status"] == "committing", still_committing
