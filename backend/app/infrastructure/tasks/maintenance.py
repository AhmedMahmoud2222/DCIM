"""SEC-05: bounded, retry-safe collector nonce and heartbeat retention.

Only the maintenance worker deletes rows. Each batch locks at most 500 eligible rows
per table, commits both deletes together, and uses SKIP LOCKED for overlapping runs.
The per-run cap bounds the work during a backlog; the next hourly run continues.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.schema import Table

from app.application.collector_auth import REQUEST_TIMESTAMP_WINDOW_SECONDS
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.sync_session import sync_engine
from app.domain.integration.models import CollectorHeartbeat, CollectorRequestNonce
from app.domain.telemetry.models import MonitoringPolicy, TelemetryConsumedKey, TelemetryReplayLedgerState
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

BATCH_SIZE = 500
MAX_BATCHES_PER_RUN = 100
_NONCE_TABLE = cast(Table, CollectorRequestNonce.__table__)
_HEARTBEAT_TABLE = cast(Table, CollectorHeartbeat.__table__)
_LEDGER_TABLE = cast(Table, TelemetryConsumedKey.__table__)
_LEDGER_STATE_TABLE = cast(Table, TelemetryReplayLedgerState.__table__)
_POLICY_TABLE = cast(Table, MonitoringPolicy.__table__)
LEDGER_BATCH_SIZE = 5000
LEDGER_MAX_BATCHES_PER_RUN = 100


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


def prune_consumed_keys(
    *, now: datetime | None = None, engine: Engine | None = None
) -> dict[str, int | bool | str]:
    """Bounded pruning of consumed-key tombstones (Issue #128 / G4-R). A STRICT no-op while the horizon is NULL.

    With a finite ``late_replay_horizon_days`` H, a tombstone is needed only until ``consumed_at + H`` (a record is accepted
    only if its age is at most H, and a key cannot be consumed before it occurred), so rows whose ``consumed_day`` is earlier
    than ``utc_day(now - H) - 1 day`` are deleted (one extra day covers UTC rounding). Before deleting anything the run
    advances ``ledger_complete_since`` to that cutoff in its own committed transaction; the ``UPDATE`` waits for in-flight late
    merges (which read the state ``FOR SHARE``), and every later merge applies ``min(H, now - ledger_complete_since)``, so a
    horizon raised afterwards never relies on a tombstone that has been or is about to be deleted. Batches commit separately
    and use SKIP LOCKED; a failed run leaves earlier batches committed and the next run resumes. Logs carry counts only.
    """
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Retention clock must be timezone-aware.")
    database = engine if engine is not None else sync_engine
    with database.begin() as conn:
        # Lock the state row BEFORE reading the horizon. Clearing the horizon takes the same lock first
        # (`guard_horizon_clear`), so a clear and this run are serialised: if the clear commits first this run reads NULL and
        # skips; if this run gets the lock first it records `pruning_begun_at`, which makes the clear refuse.
        conn.execute(
            pg_insert(_LEDGER_STATE_TABLE).values(id=1, ledger_complete_since=instant).on_conflict_do_nothing(
                index_elements=[_LEDGER_STATE_TABLE.c.id])
        )
        conn.execute(select(_LEDGER_STATE_TABLE.c.id).where(_LEDGER_STATE_TABLE.c.id == 1).with_for_update())
        horizon = conn.execute(select(_POLICY_TABLE.c.late_replay_horizon_days).where(_POLICY_TABLE.c.id == 1)).scalar()
        if horizon is None:
            result: dict[str, int | bool | str] = {"skipped": True, "reason": "horizon_unset", "deleted": 0}
            logger.info("telemetry_ledger_prune_skipped", reason="horizon_unset")
            return result
        cutoff_day = (instant - timedelta(days=horizon)).astimezone(UTC).date() - timedelta(days=1)
        cutoff_start = datetime(cutoff_day.year, cutoff_day.month, cutoff_day.day, tzinfo=UTC)
        conn.execute(
            update(_LEDGER_STATE_TABLE).where(_LEDGER_STATE_TABLE.c.id == 1).values(
                ledger_complete_since=func.greatest(_LEDGER_STATE_TABLE.c.ledger_complete_since, cutoff_start),
                pruning_begun_at=func.coalesce(_LEDGER_STATE_TABLE.c.pruning_begun_at, instant))
        )
    deleted_total = 0
    limit_reached = True
    for batch_index in range(LEDGER_MAX_BATCHES_PER_RUN):
        try:
            with database.begin() as conn:
                keys = (
                    select(_LEDGER_TABLE.c.key_hash)
                    .where(_LEDGER_TABLE.c.consumed_day < cutoff_day)
                    .order_by(_LEDGER_TABLE.c.consumed_day)
                    .limit(LEDGER_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
                deleted = len(conn.execute(
                    delete(_LEDGER_TABLE).where(_LEDGER_TABLE.c.key_hash.in_(keys)).returning(_LEDGER_TABLE.c.key_hash)
                ).all())
        except Exception:  # noqa: BLE001 -- log only fixed fields; Celery must see a failure
            logger.error("telemetry_ledger_prune_failed", error_code="LEDGER_PRUNE_BATCH_FAILED",
                         batch_index=batch_index, deleted=deleted_total)
            raise RuntimeError("Telemetry ledger prune batch failed.") from None
        deleted_total += deleted
        if deleted == 0:
            limit_reached = False
            break
    with database.begin() as conn:
        conn.execute(update(_LEDGER_STATE_TABLE).where(_LEDGER_STATE_TABLE.c.id == 1).values(
            last_pruned_at=instant, last_pruned_count=deleted_total))
    outcome: dict[str, int | bool | str] = {"skipped": False, "deleted": deleted_total, "batch_limit_reached": limit_reached}
    logger.info("telemetry_ledger_pruned", deleted=deleted_total, batch_limit_reached=limit_reached)
    return outcome


@celery_app.task(name="app.infrastructure.tasks.maintenance.prune_consumed_telemetry_keys")
def prune_consumed_telemetry_keys() -> dict[str, int | bool | str]:
    return prune_consumed_keys()


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
