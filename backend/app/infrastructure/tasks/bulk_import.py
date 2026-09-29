"""Bulk-import job execution: two Celery tasks, `parse_and_validate_bulk_import_job` and
`commit_bulk_import_job`, modeled on app/infrastructure/tasks/floorplan_import.py's own
structure (status transitions committed immediately so polling clients see progress,
try/except with rollback + a terminal-status fallback for the common failure mode) — with
one structural difference floorplan_import.py's fully-sync pipeline doesn't need: this
pipeline's validate/commit functions are async (so they can call straight into the
existing async application services — move_rack/move_equipment/catalog_designer_service —
without re-deriving their logic in a sync form), so each task's body runs via `_run_async`
on a dedicated thread with its own event loop and its own short-lived async engine, rather
than app/db/sync_session.py's shared sync engine.

The terminal-status fallback below is deliberately qualified, not an unconditional
guarantee: it catches any exception raised *after* this task body starts running and the
database is reachable (a bad row, a domain-service bug, an unexpected constraint) and
marks the job failed/committed_with_errors rather than leaving it silently stuck. It does
NOT cover the job never being picked up at all -- a Celery dispatch (`.delay(...)`) that
never reaches the broker in the first place (Redis unreachable, serialization failure) is
caught and surfaced synchronously as a 502 at the API layer instead (see the `.delay()`
call sites in app/api/v1/bulk_import.py / racks.py / equipment.py / catalog_designer.py,
Codex PR #50 review finding #7) precisely so this fallback is never depended on for that
case. Nor does it cover the database itself being unreachable while this fallback's own
write is attempted -- that failure simply propagates (there is nothing else to write it
to), and the job is left in whatever status it last durably reached."""

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.bulk_import import service
from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)


class BulkImportTaskFailed(Exception):
    """SEC (Codex PR #50 review, ROUND 2, finding #2A): raised in place of a bare `raise`
    of the original caught exception, in both `_parse_and_validate_async`'s and
    `_commit_async`'s inner `_body`. A bare `raise` there re-raises the ORIGINAL exception
    object -- message, traceback, and any chained cause -- into Celery's own task-failure
    machinery (result backend, worker logs), which can surface raw workbook cell values or
    SQL parameters via that exception's own `str()`, even though the log line right above it
    is already sanitized (fixed event name + exception class only). This wrapper carries
    only the job id and the exception's *class* (a safe, bounded taxonomy) -- never the
    original message -- and is always raised with `from None` so exception chaining never
    lets Celery's own traceback capture walk back into the original exception's message or
    `__cause__`/`__context__`."""


def _run_async(coro_factory) -> None:
    """Runs `coro_factory()` to completion, blocking the calling (sync) thread. Always
    spawns a brand-new event loop on a dedicated thread — never a bare `asyncio.run()`
    on the calling thread — because the `.run(...)` pattern this pipeline's own tests
    use (tests/api/test_bulk_import_*.py, mirroring test_floor_plans.py's `_run_import`)
    invokes a task's body synchronously from *inside* an already-running pytest-asyncio
    event loop, where `asyncio.run()` would raise "cannot be called from a running event
    loop". A real Celery worker process has no running loop on its task-execution thread
    either way, so this is correct there too, and the per-task fresh engine/thread avoids
    asyncpg's event-loop affinity (a connection pool created on one loop cannot be reused
    from another)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, coro_factory()).result()


async def _with_fresh_session(body) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with session_factory() as db:
            await body(db)
    finally:
        await engine.dispose()


async def _mark_parse_failed_on_unexpected_error(db: AsyncSession, job_id: uuid.UUID) -> None:
    """See floorplan_import.py's own `_mark_job_failed_on_unexpected_error` docstring for
    the full reasoning — identical mechanism, applied to this pipeline's job model. `job`
    is re-fetched fresh (not reused) since the caller's `db.rollback()` has already
    expired it."""
    job = await db.get(BulkImportJob, job_id)
    if job is None:
        return
    job.status = "failed_parse"
    job.rejection_reason = "The uploaded file could not be processed due to an internal error."
    await db.commit()


async def _mark_commit_failed_on_unexpected_error(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Any row left at status='valid' (neither 'committed' nor 'failed' by the normal
    per-row handling in app/application/bulk_import/service.py::run_commit) could not be
    resolved one way or the other by the failure that interrupted this task — mark each
    one failed with a generic reason so no row is left silently unresolved and the job
    still reaches an allowed terminal status (JOB_STATUSES has no separate "commit failed
    unexpectedly" state; treating it as committed_with_errors is accurate: some rows did
    not commit)."""
    job = await db.get(BulkImportJob, job_id)
    if job is None:
        return
    stuck_rows = list(
        (
            await db.execute(select(BulkImportRow).where(BulkImportRow.job_id == job.id, BulkImportRow.status == "valid"))
        ).scalars()
    )
    for row in stuck_rows:
        row.status = "failed"
        row.errors = [*row.errors, {"field": None, "message": "This row could not be committed due to an internal error."}]
        job.failed_row_count += 1
    job.status = "committed_with_errors"
    job.committed_at = datetime.now(UTC)
    await db.commit()


async def _parse_and_validate_async(job_id: str) -> None:
    async def _body(db: AsyncSession) -> None:
        try:
            await service.run_parse_and_validate(db, uuid.UUID(job_id))
        except Exception as exc:
            await db.rollback()
            # SEC (Codex PR #50 review, finding #3): `logger.exception`/`exc_info` would
            # emit a full traceback, which can embed raw workbook cell values (e.g. a
            # ValueError message quoting the bad cell) -- fixed event name + exception
            # *class* only, matching app/api/v1/collectors.py's SEC-07 `logger.error`
            # pattern for this exact class of bug.
            logger.error("bulk_import_parse_unexpected_failure", job_id=job_id, error_code=type(exc).__name__)
            await _mark_parse_failed_on_unexpected_error(db, uuid.UUID(job_id))
            raise BulkImportTaskFailed(f"job {job_id} failed: {type(exc).__name__}") from None

    await _with_fresh_session(_body)


async def _commit_async(job_id: str) -> None:
    async def _body(db: AsyncSession) -> None:
        try:
            await service.run_commit(db, uuid.UUID(job_id))
        except Exception as exc:
            await db.rollback()
            logger.error("bulk_import_commit_unexpected_failure", job_id=job_id, error_code=type(exc).__name__)
            await _mark_commit_failed_on_unexpected_error(db, uuid.UUID(job_id))
            raise BulkImportTaskFailed(f"job {job_id} failed: {type(exc).__name__}") from None

    await _with_fresh_session(_body)


@celery_app.task(name="app.infrastructure.tasks.bulk_import.parse_and_validate_bulk_import_job")
def parse_and_validate_bulk_import_job(job_id: str) -> None:
    _run_async(lambda: _parse_and_validate_async(job_id))


@celery_app.task(name="app.infrastructure.tasks.bulk_import.commit_bulk_import_job")
def commit_bulk_import_job(job_id: str) -> None:
    _run_async(lambda: _commit_async(job_id))
