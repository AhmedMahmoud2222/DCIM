"""SEC-05: bounded, retry-safe collector nonce and heartbeat retention.

Only the maintenance worker deletes rows. Each batch locks at most 500 eligible rows
per table, commits both deletes together, and uses SKIP LOCKED for overlapping runs.
The per-run cap bounds the work during a backlog; the next hourly run continues.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.schema import Table

from app.application.collector_auth import REQUEST_TIMESTAMP_WINDOW_SECONDS
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.sync_session import sync_engine
from app.domain.integration.models import CollectorHeartbeat, CollectorRequestNonce
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

BATCH_SIZE = 500
MAX_BATCHES_PER_RUN = 100
_NONCE_TABLE = cast(Table, CollectorRequestNonce.__table__)
_HEARTBEAT_TABLE = cast(Table, CollectorHeartbeat.__table__)


def _delete_batch(conn: Connection, table: Table, timestamp_column: str, cutoff: datetime) -> int:
    timestamp = table.c[timestamp_column]
    ids = (
        select(table.c.id)
        .where(timestamp < cutoff)  # exactly at the retention boundary remains
        .order_by(timestamp, table.c.id)
        .limit(BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    result = conn.execute(delete(table).where(table.c.id.in_(ids)).returning(table.c.id))
    return len(result.scalars().all())  # bounded by BATCH_SIZE, and counts real deletions


def prune_expired_collector_rows(
    *, now: datetime | None = None, engine: Engine | None = None, settings: Settings | None = None,
) -> dict[str, int | bool]:
    """Prune against a fixed UTC cutoff. Optional inputs support deterministic DB tests.

    A failed batch rolls back both tables; earlier committed batches stay committed and
    a retry safely resumes. Counts include committed deletes only. Never log database
    exceptions, SQL parameters, collector IDs, nonces or heartbeat payloads.
    """
    policy = settings or get_settings()
    if policy.nonce_retention_seconds < 3600 or policy.nonce_retention_seconds <= REQUEST_TIMESTAMP_WINDOW_SECONDS:
        raise ValueError("Nonce retention must remain at least one hour and exceed the request acceptance window.")
    if policy.heartbeat_retention_days < 30:
        raise ValueError("Heartbeat retention must remain at least 30 days.")
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Retention clock must be timezone-aware.")
    nonce_cutoff = instant - timedelta(seconds=policy.nonce_retention_seconds)
    heartbeat_cutoff = instant - timedelta(days=policy.heartbeat_retention_days)
    database = engine if engine is not None else sync_engine
    nonce_count = heartbeat_count = 0
    limit_reached = True

    for batch_index in range(MAX_BATCHES_PER_RUN):
        try:
            with database.begin() as conn:
                nonces = _delete_batch(conn, _NONCE_TABLE, "seen_at", nonce_cutoff)
                heartbeats = _delete_batch(conn, _HEARTBEAT_TABLE, "ts", heartbeat_cutoff)
        except Exception:  # noqa: BLE001 -- log only safe fixed fields; Celery must see a failure
            logger.error(
                "collector_retention_failed", error_code="RETENTION_BATCH_FAILED",
                batch_index=batch_index, deleted_nonces=nonce_count, deleted_heartbeats=heartbeat_count,
            )
            raise RuntimeError("Collector retention batch failed.") from None
        nonce_count += nonces
        heartbeat_count += heartbeats
        if not nonces and not heartbeats:
            limit_reached = False
            break

    result: dict[str, int | bool] = {
        "deleted_nonces": nonce_count,
        "deleted_heartbeats": heartbeat_count,
        "batch_limit_reached": limit_reached,
    }
    logger.info("collector_retention_completed", **result)
    return result


@celery_app.task(name="app.infrastructure.tasks.maintenance.prune_collector_nonces_and_heartbeats")
def prune_collector_nonces_and_heartbeats() -> dict[str, int | bool]:
    return prune_expired_collector_rows()


@celery_app.task(name="app.infrastructure.tasks.maintenance.purge_expired_staged_catalog_documents")
def purge_expired_staged_catalog_documents() -> int:
    """DCIM01 PDF datasheet import: removes datasheet uploads nobody attached within
    `catalog_document_staging_retention_days` (default 14), plus their stored objects when
    unshared. Attached and superseded-predecessor documents are never touched."""
    from app.application.catalog_documents.service import purge_expired_staged_documents
    from app.db.sync_session import get_sync_db
    from app.infrastructure.storage import get_document_storage_backend

    settings = get_settings()
    with get_sync_db() as db:
        deleted = purge_expired_staged_documents(
            db, storage=get_document_storage_backend(), retention_days=settings.catalog_document_staging_retention_days
        )
    logger.info("catalog_document_staging_purged", deleted=deleted)
    return deleted
