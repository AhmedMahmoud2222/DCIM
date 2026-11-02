"""Session-scoped advisory lock that serializes migration runs which opt in (currently 0057).

Alembic records a revision in `alembic_version` only AFTER the migration function returns, and commits that update after
that. A migration therefore cannot release the lock itself without opening a window in which a second runner still sees
the old revision and starts the same work. So a migration only ACQUIRES the lock (`acquire_migration_lock`); `env.py`
RELEASES it (`release_migration_locks`) after `context.run_migrations()` has committed, or on failure. If the process or
connection dies first, PostgreSQL drops a session-level advisory lock when the session ends.

A session-level lock survives `autocommit_block()` commits, which a transaction-level lock would not.
"""

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Connection

log = logging.getLogger("alembic.runtime.migration")

MIGRATION_LOCK_CLASS = 1940057
MIGRATION_LOCK_ID = 1

_PARAMS = {"lock_class": MIGRATION_LOCK_CLASS, "lock_id": MIGRATION_LOCK_ID}


def acquire_migration_lock(connection: Connection) -> None:
    """Take the lock or fail fast. Re-entrant within one session (acquisitions stack)."""
    acquired = connection.scalar(sa.text("SELECT pg_try_advisory_lock(:lock_class, :lock_id)"), _PARAMS)
    if not acquired:
        raise RuntimeError("0057: another migration invocation is active; wait for it to finish, then re-run.")


def release_migration_locks(connection: Connection) -> None:
    """Release every acquisition this session holds. Never raises: a broken connection releases on close anyway."""
    try:
        for _ in range(100):
            if not connection.scalar(sa.text("SELECT pg_advisory_unlock(:lock_class, :lock_id)"), _PARAMS):
                break
    except Exception:  # noqa: BLE001 - must not mask the migration's own exception; closing the session releases the lock
        log.warning("0057: could not explicitly release the migration advisory lock; it ends with the session")
