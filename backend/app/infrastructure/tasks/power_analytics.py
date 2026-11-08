"""Celery tasks for Issue #102: hourly utilization snapshots and queued operational reports.

Both bodies are idempotent. A snapshot run only inserts missing (bucket, scope) rows. A report run claims
its job with an atomic UPDATE, so a duplicate delivery or a redelivery after completion does nothing."""

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import power_history, power_reports
from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

REPORTS_QUEUE = "reports"
SNAPSHOT_LOOKBACK_HOURS = 3


def _run_async(coro_factory) -> None:
    """Run the coroutine on a dedicated thread with its own loop (same reasoning as the bulk-import tasks:
    asyncpg connections are bound to the loop that created them, and tests call tasks from a running loop)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, coro_factory()).result()


async def _with_fresh_session(body) -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as db:
            await body(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.infrastructure.tasks.power_analytics.snapshot_power_utilization")
def snapshot_power_utilization(lookback_hours: int = SNAPSHOT_LOOKBACK_HOURS) -> dict:
    summary: dict = {}

    async def _body(db) -> None:
        summary.update(await power_history.snapshot_all_sites(db, datetime.now(UTC), lookback_hours))

    _run_async(lambda: _with_fresh_session(_body))
    logger.info("power_snapshots_written", **summary)
    return summary


@celery_app.task(name="app.infrastructure.tasks.power_analytics.generate_power_report")
def generate_power_report(job_id: str) -> str:
    outcome = {"value": "skipped"}

    async def _body(db) -> None:
        outcome["value"] = await power_reports.run_report_job(db, uuid.UUID(job_id), datetime.now(UTC))

    _run_async(lambda: _with_fresh_session(_body))
    logger.info("power_report_job_finished", job_id=job_id, outcome=outcome["value"])
    return outcome["value"]


@celery_app.task(name="app.infrastructure.tasks.power_analytics.requeue_stuck_power_reports")
def requeue_stuck_power_reports() -> int:
    """Re-dispatch queued jobs whose first delivery never ran and running jobs whose lease expired.
    Safe to repeat: `run_report_job` claims atomically."""
    from sqlalchemy import or_, select

    from app.domain.power.analytics_models import PowerReportJob

    found: list[str] = []

    async def _body(db) -> None:
        now = datetime.now(UTC)
        rows = await db.execute(
            select(PowerReportJob.id)
            .where(
                or_(
                    PowerReportJob.status == "queued",
                    (PowerReportJob.status == "running") & (PowerReportJob.lease_expires_at < now),
                )
            )
            .where(PowerReportJob.created_at < now - timedelta(seconds=60))
            .order_by(PowerReportJob.created_at)
            .limit(50)
        )
        found.extend(str(r) for r in rows.scalars())

    _run_async(lambda: _with_fresh_session(_body))
    for job_id in found:
        generate_power_report.apply_async(args=[job_id], queue=REPORTS_QUEUE)
    return len(found)
