"""SEC (Codex PR #50 review, ROUND 3, finding #2 / ROUND 4, blocker 2): the existing
`test_a_stale_deliverys_fallback_never_corrupts_the_replacement_deliverys_job`
(test_bulk_import_commit_concurrency.py) proves the lease-fencing logic itself is correct,
but does so by calling `bulk_import_service.run_commit` and
`bulk_import_service._mark_commit_failed_if_still_owner` directly, sequentially, in this
test process -- never through an actual Celery task dispatch, and never with delivery A and
delivery B genuinely alive and interleaved at the same time (A's fallback is invoked only
after the whole scenario has already played out).

This module reproduces the identical interleaving -- delivery A's lease expires while A is
merely stalled, not dead; delivery B reclaims the lease and finishes the job for real; A
then resumes and raises its own unrelated exception -- through TWO REAL, CONCURRENT Celery
task executions (`commit_bulk_import_job.delay(...)`, consumed by a real embedded worker
running two worker threads), not direct function calls. A's "stall" and "resume with an
exception" are driven by real `threading.Event` synchronization between actual OS threads
(A's dedicated task thread vs. this test's main thread), so the ordering is deterministic
(B is only ever dispatched after A has provably already claimed the lease and entered its
per-row commit, and A is only ever allowed to resume after B has provably already finished)
even though the underlying execution is genuinely concurrent, not simulated.

This also verifies Codex's "preserve sanitized Celery error handling" requirement
end-to-end: A's task must reach Celery's own FAILURE state (not silently succeed, not hang)
and the failure recorded in Celery's result backend must be the sanitized
`BulkImportTaskFailed` wrapper (app/infrastructure/tasks/bulk_import.py) -- never the raw
injected exception's message, which carries a sentinel string this test asserts is absent
from everything Celery persisted about the failure."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta

import celery.contrib.testing.tasks  # noqa: F401  -- registers 'celery.ping' for start_worker's ping check
import pytest
from celery.contrib.testing.worker import start_worker
from sqlalchemy import select, text, update

from app.application.bulk_import import service as bulk_import_service
from app.application.bulk_import.commit import rack as commit_rack
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.celery_app import celery_app
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model

pytestmark = pytest.mark.asyncio

_SENTINEL = "simulated-transient-error-on-A-resume-SEC07-must-not-leak"


async def _poll_until(client, headers, job_id: str, *, terminal_statuses: set[str], timeout_seconds: float = 15.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_status: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        last_status = (await client.get(f"/api/v1/import-jobs/{job_id}", headers=headers)).json()
        if last_status["status"] in terminal_statuses:
            return last_status
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {terminal_statuses} within {timeout_seconds}s; last seen: {last_status}")


async def test_two_real_concurrent_deliveries_a_stale_delivery_raising_never_corrupts_b(
    client, auth_headers, db_session, monkeypatch,
):
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

    # Deterministic cross-thread choreography: A blocks the instant it starts processing its
    # one row (before touching it at all), signaling this test's main thread that it has
    # provably already claimed the lease and begun. This test only ever lets A resume (and
    # then raise) after B has provably already finished -- the ordering is real, not timed.
    a_started = threading.Event()
    let_a_resume = threading.Event()
    original_commit_row = commit_rack.commit_row
    call_count = {"n": 0}
    call_lock = threading.Lock()

    async def _commit_row_with_injected_stall_then_error(db, *, job, row, mode):
        with call_lock:
            call_count["n"] += 1
            is_a = call_count["n"] == 1
        if is_a:
            a_started.set()
            if not let_a_resume.wait(timeout=15):
                raise AssertionError("test timed out waiting to release delivery A")
            raise RuntimeError(_SENTINEL)
        return await original_commit_row(db, job=job, row=row, mode=mode)

    monkeypatch.setitem(bulk_import_service._COMMIT_FUNCS, "rack", _commit_row_with_injected_stall_then_error)

    with start_worker(celery_app, pool="threads", concurrency=2, perform_ping_check=True, loglevel="info"):
        # Started BEFORE the upload, not after: the upload endpoint issues its own real
        # `parse_and_validate_bulk_import_job.delay(...)` call (app/api/v1/racks.py), and if
        # no worker is running yet that message sits queued in the broker -- only to be
        # consumed later, out of order, once a worker finally starts (empirically confirmed
        # during this module's own development: it re-ran parse-and-validate on top of this
        # test's already-'committing' job, silently reverting it back to 'validated' and
        # racing everything below). Starting the worker first means the real dispatch is
        # consumed immediately, in its natural place in the flow -- exactly like production.
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

        validated = await _poll_until(client, headers, job["id"], terminal_statuses={"validated"}, timeout_seconds=10.0)
        assert validated["valid_row_count"] == 1, validated

        # Claim the job into 'committing' exactly like the real API endpoint's atomic claim.
        claim = await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id, BulkImportJob.status == "validated").values(
                status="committing"
            )
        )
        assert claim.rowcount == 1
        await db_session.commit()

        result_a = commit_bulk_import_job.delay(job["id"])

        assert await asyncio.to_thread(a_started.wait, 10.0), "delivery A never reached its first row commit"

        # A is now provably blocked, holding the lease it claimed, mid-commit. Simulate real
        # time passing until that lease naturally expires (exactly as the sibling recovery
        # tests do), then dispatch B for real -- a second, fully independent Celery delivery.
        await db_session.execute(
            update(BulkImportJob).where(BulkImportJob.id == job_id).values(
                commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db_session.commit()

        result_b = commit_bulk_import_job.delay(job["id"])

        finished = await _poll_until(
            client, headers, job["id"], terminal_statuses={"committed", "committed_with_errors"}, timeout_seconds=15.0,
        )
        assert finished["status"] == "committed", finished
        assert finished["committed_row_count"] == 1

        racks = (await client.get("/api/v1/racks", headers=headers)).json()
        matching_before = [r for r in racks["items"] if r["asset_tag"] == asset_tag]
        assert len(matching_before) == 1, "delivery B must have committed the row exactly once"

        audit_count_before = (
            await db_session.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
                {"id": str(job_id)},
            )
        ).scalar_one()
        assert audit_count_before == 1

        await asyncio.to_thread(result_b.get, 10.0)  # propagate any unexpected failure from B

        # Only now -- after B has provably already finished the job -- let stale delivery A
        # resume and raise its own unrelated error.
        let_a_resume.set()
        a_outcome = await asyncio.to_thread(result_a.get, 15.0, propagate=False)

    # --- Celery-level assertions: A's task must fail, and fail *sanitized*. ---
    assert result_a.state == "FAILURE", f"delivery A's task must reach Celery's FAILURE state, got {result_a.state}"
    assert isinstance(a_outcome, Exception)
    assert type(a_outcome).__name__ == "BulkImportTaskFailed", (
        f"Celery must only ever see the sanitized wrapper, not the raw exception; got {type(a_outcome).__name__}"
    )
    assert _SENTINEL not in str(a_outcome), "the raw injected exception message must never reach Celery's result backend"
    assert _SENTINEL not in repr(a_outcome)
    assert _SENTINEL not in str(result_a.traceback or "")

    # --- Database-level assertions: A's late, unauthorized fallback must never have run. ---
    unchanged = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert unchanged["status"] == "committed", "A's stale exception must never flip B's finished job back to an error state"
    assert unchanged["failed_row_count"] == 0, "A's stale exception must never mark B's committed row failed"
    assert unchanged["committed_row_count"] == 1

    committed_row = (
        await db_session.execute(select(BulkImportRow).where(BulkImportRow.job_id == job_id))
    ).scalar_one()
    assert committed_row.status == "committed", "B's committed row must not be reverted by A's late fallback"

    racks_after = (await client.get("/api/v1/racks", headers=headers)).json()
    matching_after = [r for r in racks_after["items"] if r["asset_tag"] == asset_tag]
    assert len(matching_after) == 1, "A must never create a second, duplicate rack after B already committed one"

    audit_count_after = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
            {"id": str(job_id)},
        )
    ).scalar_one()
    assert audit_count_after == audit_count_before, "A's fallback must never write its own duplicate job-level audit row"
