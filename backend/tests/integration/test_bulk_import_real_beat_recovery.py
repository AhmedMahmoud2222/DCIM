"""SEC (Codex PR #50 review, ROUND 4, blocker 1): "verify Celery beat and deployment
wiring" -- test_bulk_import_real_worker_recovery.py proves a real worker correctly executes
`requeue_stuck_bulk_import_commits` and the `commit_bulk_import_job` it dispatches, but it
still triggers the sweeper itself via a test-issued `.delay()` call. Nothing in this repo's
test suite, before this module, ever actually ran `celery beat` and let IT decide when to
fire that task -- so a broken/missing `beat_schedule` entry, a typo in the task name, or a
schedule pointed at the wrong queue would not have been caught by any existing test.

This module runs the real `celery beat` binary as a subprocess -- the *exact* command
docker-compose.yml's/docker-compose.production.yml's own `celery-beat` service uses
(`celery -A app.infrastructure.celery_app beat --loglevel=info --schedule=<path>`) -- against
this pipeline's real, unmodified 30-second schedule (app/infrastructure/celery_app.py's
beat_schedule["requeue-stuck-bulk-import-commits"]). No test-issued `.delay()` call for the
sweeper task appears anywhere in this file; the only thing that ever calls
`requeue_stuck_bulk_import_commits` is the real `celery beat` process itself, on its own
real schedule, exactly as it would in production. A real embedded worker (started the same
way test_bulk_import_real_worker_recovery.py does) consumes what beat dispatches.

This is deliberately a slower test (bounded at a little over one real 30-second beat tick)
in exchange for being the one test in this suite that would actually fail if someone broke
the beat_schedule wiring itself -- as opposed to only the Python function it schedules."""

import asyncio
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import celery.contrib.testing.tasks  # noqa: F401  -- registers 'celery.ping' for start_worker's ping check
import pytest
from celery.contrib.testing.worker import start_worker
from sqlalchemy import update

from app.domain.bulk_import.models import BulkImportJob
from app.infrastructure.celery_app import celery_app
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model

pytestmark = pytest.mark.asyncio

# app/infrastructure/celery_app.py's real schedule for this task is 30.0 seconds. A beat
# process started at an arbitrary point in its cycle fires on its first tick almost
# immediately (beat schedules "now + interval" from its own start, per celery.beat's own
# Scheduler, not from some fixed wall-clock epoch) -- so one interval plus generous slack
# for worker/database round trips is a safe, real bound, not a guess at beat internals.
_BEAT_SCHEDULE_SECONDS = 30.0
_POLL_TIMEOUT_SECONDS = _BEAT_SCHEDULE_SECONDS + 25.0


async def _poll_until(client, headers, job_id: str, *, terminal_statuses: set[str], timeout_seconds: float) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_status: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        last_status = (await client.get(f"/api/v1/import-jobs/{job_id}", headers=headers)).json()
        if last_status["status"] in terminal_statuses:
            return last_status
        await asyncio.sleep(1.0)
    raise AssertionError(f"job {job_id} never reached {terminal_statuses} within {timeout_seconds}s; last seen: {last_status}")


async def test_real_celery_beat_process_dispatches_the_sweeper_on_its_own_schedule(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

    with start_worker(celery_app, pool="solo", perform_ping_check=True, loglevel="info"):
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
        job = upload.json()
        job_id = uuid.UUID(job["id"])

        # The worker started above (consuming the real broker) parses this job via the
        # upload endpoint's own real `.delay()` dispatch.
        await _poll_until(client, headers, job["id"], terminal_statuses={"validated"}, timeout_seconds=10.0)

        # Simulate a crashed delivery, exactly as the sibling real-worker test does.
        claim = await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
                status="committing", commit_lease_id=uuid.uuid4(),
                commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )
        assert claim.rowcount == 1
        await db_session.commit()

        with tempfile.TemporaryDirectory(prefix="dcim-test-celerybeat-") as schedule_dir:
            schedule_path = str(Path(schedule_dir) / "celerybeat-schedule")
            # The exact command docker-compose.yml's/docker-compose.production.yml's own
            # celery-beat service runs -- proving this test exercises the same deployment
            # wiring, not a test-only invocation shape.
            beat_process = subprocess.Popen(
                [
                    sys.executable, "-m", "celery", "-A", "app.infrastructure.celery_app", "beat",
                    "--loglevel=info", f"--schedule={schedule_path}",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                # No `.delay()` call for the sweeper task anywhere in this test -- only the
                # real `celery beat` subprocess above, on its own real 30s schedule, ever
                # triggers `requeue_stuck_bulk_import_commits` from here on.
                final = await _poll_until(
                    client, headers, job["id"], terminal_statuses={"committed", "committed_with_errors"},
                    timeout_seconds=_POLL_TIMEOUT_SECONDS,
                )
            finally:
                beat_process.terminate()
                try:
                    beat_output, _ = beat_process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    beat_process.kill()
                    beat_output, _ = beat_process.communicate(timeout=10)

    assert final["status"] == "committed", (final, "beat log:\n" + beat_output)
    assert final["committed_row_count"] == 1

    racks = (await client.get("/api/v1/racks", headers=headers)).json()
    matching = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
    assert len(matching) == 1, "the crashed job's row must be committed exactly once via real beat + real worker"
