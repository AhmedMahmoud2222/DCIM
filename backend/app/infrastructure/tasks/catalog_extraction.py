"""DCIM01 PDF datasheet import, PR-B: background extraction.

`run_catalog_extraction_job` is idempotent: it claims the job with a lease and fencing token
(`app.application.catalog_documents.extraction.service`), so a duplicate delivery, a redelivery after
a crash or two workers handed the same id cannot both publish a result. The beat task
`requeue_stuck_catalog_extraction_jobs` re-dispatches jobs whose dispatch was lost (queued too long)
or whose worker died (lease expired); the claim, not the sweep, decides who runs.

Failures never include exception text, paths or document content (the service stores fixed codes)."""

import uuid

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

EXTRACTION_QUEUE = "extraction"


@celery_app.task(name="app.infrastructure.tasks.catalog_extraction.run_catalog_extraction_job")
def run_catalog_extraction_job(job_id: str) -> str:
    from app.application.catalog_documents.extraction.service import run_extraction_job
    from app.db.sync_session import get_sync_db
    from app.infrastructure.storage import get_document_storage_backend

    return run_extraction_job(
        uuid.UUID(job_id), settings=get_settings(), storage=get_document_storage_backend(), session_factory=get_sync_db
    )


@celery_app.task(name="app.infrastructure.tasks.catalog_extraction.requeue_stuck_catalog_extraction_jobs")
def requeue_stuck_catalog_extraction_jobs() -> int:
    from app.application.catalog_documents.extraction.service import find_jobs_to_dispatch
    from app.db.sync_session import get_sync_db

    with get_sync_db() as db:
        job_ids = find_jobs_to_dispatch(db)
    for job_id in job_ids:
        run_catalog_extraction_job.apply_async(args=[str(job_id)], queue=EXTRACTION_QUEUE)
    if job_ids:
        logger.info("catalog_extraction_jobs_requeued", count=len(job_ids))
    return len(job_ids)


def dispatch_extraction_job(job_id: uuid.UUID) -> bool:
    """Best effort. A failed dispatch leaves the job `queued`, which the sweep picks up; the API
    still answers 202 because the request itself was recorded."""
    try:
        run_catalog_extraction_job.apply_async(args=[str(job_id)], queue=EXTRACTION_QUEUE)
    except Exception as exc:  # noqa: BLE001 - broker errors can carry connection strings
        logger.warning("catalog_extraction_dispatch_failed", job_id=str(job_id), error_type=type(exc).__name__)
        return False
    return True
