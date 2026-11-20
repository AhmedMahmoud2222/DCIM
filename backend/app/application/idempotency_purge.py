"""DB-03 (#357): purge of expired idempotency ledger rows. DISABLED unless explicitly enabled.

`idempotency_purge_enabled` defaults to False and the purge is never activated by this change. While it is
disabled `purge_expired_idempotency_keys` returns 0 *before* any engine, session or statement exists, so a
disabled deployment issues no SQL at all (test T37).

When an operator later enables it (a separate, written authorization with the capacity/index gates in the
DB-03 design), each batch is ONE statement:

    WITH doomed AS (SELECT id FROM idempotency_key WHERE expires_at <= :cutoff LIMIT :batch
                    FOR UPDATE SKIP LOCKED)
    DELETE FROM idempotency_key d USING doomed WHERE d.id = doomed.id

run in its own short transaction with a transaction-local `statement_timeout` (at most 2000 ms), so row locks
live for one bounded statement only and a locked row (a live owner, a concurrent replacement) is skipped, not
waited on. A row is only eligible once it is `grace` seconds past `expires_at`; renewed and replaced rows have
a fresh `expires_at` and are therefore never selected. Runs are time-budgeted and throttled. Failures are
logged with fixed codes only: driver exceptions can carry SQL parameters (keys, hashes, responses) and are
never logged (SEC-07).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# One statement per batch. No ORDER BY (it would force a sort of the whole candidate set under lock).
PURGE_BATCH_SQL = text(
    "WITH doomed AS ("
    " SELECT id FROM idempotency_key WHERE expires_at <= :cutoff LIMIT :batch FOR UPDATE SKIP LOCKED"
    ") DELETE FROM idempotency_key AS d USING doomed WHERE d.id = doomed.id"
)
_QUERY_CANCELED_SQLSTATE = "57014"


def _is_statement_timeout(error: DBAPIError) -> bool:
    return getattr(error.orig, "sqlstate", None) == _QUERY_CANCELED_SQLSTATE or (
        getattr(error.orig, "pgcode", None) == _QUERY_CANCELED_SQLSTATE
    )


def purge_expired_idempotency_keys(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Delete expired-beyond-grace ledger rows within a bounded time budget. Returns rows deleted."""
    policy = settings or get_settings()
    if not policy.idempotency_purge_enabled:
        return 0  # strict no-op: no engine access, no session, no SQL.

    if engine is None:
        from app.db.sync_session import sync_engine  # imported lazily so a disabled run never touches it

        engine = sync_engine

    clock = now or (lambda: datetime.now(UTC))
    cutoff = clock() - timedelta(seconds=policy.idempotency_purge_grace_seconds)
    batch_size = policy.idempotency_purge_batch_size
    timeout_ms = policy.idempotency_purge_statement_timeout_ms
    started = monotonic()
    deleted_total = 0

    while True:
        try:
            with engine.begin() as conn:
                # Transaction-local (is_local=true): never leaks onto the pooled connection.
                conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(timeout_ms)})
                deleted = cast(int, conn.execute(PURGE_BATCH_SQL, {"cutoff": cutoff, "batch": batch_size}).rowcount)
        except DBAPIError as error:
            # Nothing was deleted by the failed batch (it rolled back); back off until the next scheduled run.
            code = "idempotency_purge_statement_timeout" if _is_statement_timeout(error) else "idempotency_purge_db_error"
            logger.warning(code, deleted_before_failure=deleted_total)
            break
        except SQLAlchemyError:
            logger.warning("idempotency_purge_db_error", deleted_before_failure=deleted_total)
            break

        deleted_total += deleted
        if deleted < batch_size:
            break  # drained (or the rest is locked: the next run continues)
        # The pause counts against the budget: never sleep into, or past, the deadline and then start another
        # batch. Elapsed time is read once so the check and the pause use the same instant.
        pause_seconds = policy.idempotency_purge_pause_ms / 1000
        if (monotonic() - started) + pause_seconds >= policy.idempotency_purge_max_runtime_seconds:
            break  # time budget exhausted (or would be during the pause); resume next run
        if pause_seconds:
            sleep(pause_seconds)

    if deleted_total:
        logger.info("idempotency_purge_completed", deleted=deleted_total)
    return deleted_total
