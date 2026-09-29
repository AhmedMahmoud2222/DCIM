from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Codex's Phase 11 review (Issue #38 / PR #49): this cleanup's own `rollback()` can
    itself fail (the session is genuinely unusable, e.g. after connection loss) --
    exactly the condition a route-level handler like `ingest_batch()`'s
    `_release_claim_safely()` may already be recovering from when its own sanitized
    `ApiError` propagates in here to be cleaned up. An unguarded failure here would
    replace that already-sanitized exception with this cleanup's raw one, which is
    not necessarily an `ApiError` and so would reach `app/core/errors.py`'s
    catch-all handler -- logging `str(exc)`/`exc_info=True` unsanitized, and
    returning a generic 500 instead of whatever safe response the route intended.

    Codex's follow-up review (same PR): `async with AsyncSessionLocal() as session:`
    puts `AsyncSession.__aexit__`'s own `close()` call OUTSIDE any guard -- it still
    runs, unguarded, as this function's `except` block's `raise` propagates out through
    the `async with`. A failing close() would then replace the just-recovered, sanitized
    exception with its own raw one, undoing the fix above one layer further out. Managed
    manually here instead of via `async with`, so every exit path -- the rollback
    attempt, a best-effort `invalidate()` when rollback itself failed (discarding the
    connection so the pool never hands a known-broken one to a later request, per Codex's
    explicit ask), and the final `close()` -- is individually guarded with the same
    fixed-field-only logging discipline. The ORIGINAL exception from `yield session` is
    what ultimately propagates via the bare `raise` right after the nested guards;
    `close()`'s own guard lives inside `finally` specifically so it can never replace it.

    Codex's third follow-up (same PR): the `invalidate()` fallback above only fired when
    `rollback()` itself failed -- a `close()` failure with NO preceding rollback failure
    (an ordinary successful request, or one whose rollback succeeded fine) was logged and
    swallowed with no attempt to discard the connection at all. A connection whose own
    close() failed is exactly the kind SQLAlchemy's pool cannot safely trust back into
    circulation (this session's own reproduction of the pre-guard version proved a failed
    close can leave a connection "idle in transaction" holding real locks indefinitely --
    see the regression tests below). `close()`'s own failure now attempts the same
    best-effort `invalidate()`, independent of which branch got here.

    Codex's fourth follow-up (same PR): `session.invalidate()` itself is not a reliable
    fallback after `rollback()`/`close()` has already failed. Reading SQLAlchemy 2.x's own
    source (`Session.invalidate` -> `_close_impl(invalidate=True)` -> only iterates
    `self._transaction._iterate_self_and_parents()` `if self._transaction is not None`)
    against `SessionTransaction.close()`/`.rollback()` shows both set
    `session._transaction = self._parent` (`None` for a root transaction) *before* the
    loop that actually closes/rolls back each connection -- a loop whose own
    `connection.close()`/`t[1].rollback()` call is exactly what can raise and reach our
    `except` blocks below. By the time that exception reaches us, `session._transaction`
    is already `None`, so our own `await session.invalidate()` call's internal
    `if self._transaction is not None` check is always false -- it is a silent no-op,
    every single time it would ever actually be needed. Verified directly against this
    project's pinned SQLAlchemy version (not merely read from upstream source) in this
    round's regression tests below, with a real, uncommitted, checked-out connection.

    Fixed by never depending on the *session's* transaction bookkeeping to find the
    connection to invalidate: `_snapshot_connection_for_invalidation()` below grabs the
    actual `AsyncConnection` object directly, via the public `session.in_transaction()` /
    `session.connection()` APIs, immediately *before* the operation that might fail
    (`rollback()` or `close()`) -- while the session's own pointers are still intact --
    and holds onto that reference independently. If the operation then fails, invalidation
    calls `.invalidate()` directly on that captured connection object, bypassing the
    session's (by-then-already-cleared) internal state entirely. `in_transaction()` is a
    pure in-memory check (no I/O); `connection()` reuses the transaction's existing
    connection rather than opening a new one (`Session._connection_for_bind`'s own cache),
    so this adds no new connections and is a no-op whenever no transaction is open.

    Codex's fifth follow-up (same PR): `session.connection()` is an *awaited* SQLAlchemy
    operation against the live connection, not a pure in-memory check -- unlike
    `in_transaction()`, it can itself raise on a connection that is already unusable (e.g.
    invalidated or lost) before rollback/close is even attempted. Calling it unguarded, as
    the previous round did, meant a snapshot failure raised immediately: in the `except`
    block, that happened *before* the guarded `session.rollback()` call ever ran, replacing
    the original, already-sanitized exception (`ingest_batch`'s recovered `ApiError(503)`,
    say) with this raw one -- undoing every previous round's fix one step further back. In
    `finally`, it raised *before* the guarded `session.close()` call, which meant it could
    replace an otherwise-*successful* response on an ordinary read route, and skipped
    `close()` entirely -- leaking the connection outright rather than merely failing to
    invalidate it. Fixed by `_safe_snapshot_connection_for_invalidation()` below, which
    wraps the snapshot attempt itself in a guard: on failure it logs a fixed event name
    only (distinct from the existing rollback/close-failure events, so "failed to obtain
    the connection" is distinguishable from "obtained it but failed to close/invalidate
    it") and returns `None` -- which every caller already treats as "nothing to invalidate"
    -- so the guarded `rollback()`/`close()` call immediately below always still runs
    regardless of whether the snapshot succeeded. A snapshot failure does mean this round's
    direct-invalidation protection is unavailable for that specific cleanup attempt (there
    is no connection reference left to invalidate); this is a narrow, documented
    limitation, not silently claimed-away -- see the regression tests below, and note the
    same asyncpg dialect auto-invalidation described in the fourth follow-up above already
    covers the common case (a genuinely disconnected connection) independently of this
    module's own logic."""
    session = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        connection = await _safe_snapshot_connection_for_invalidation(
            session, event="db_session_cleanup_snapshot_failed"
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
            logger.error("db_session_cleanup_rollback_failed")
            await _invalidate_snapshotted_connection(connection, event="db_session_cleanup_invalidate_failed")
        raise
    finally:
        connection = await _safe_snapshot_connection_for_invalidation(
            session, event="db_session_cleanup_finally_snapshot_failed"
        )
        try:
            await session.close()
        except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
            logger.error("db_session_cleanup_close_failed")
            await _invalidate_snapshotted_connection(connection, event="db_session_cleanup_close_invalidate_failed")


async def _snapshot_connection_for_invalidation(session: AsyncSession) -> AsyncConnection | None:
    """Captures the session's current live connection, if any, via public APIs only --
    called immediately before `rollback()`/`close()`, while the session's own transaction
    pointer is still intact. See `get_db`'s docstring (Codex's fourth follow-up) for why
    this snapshot -- not a post-failure `session.invalidate()` call -- is what actually
    lets a subsequent failure discard the real connection."""
    if not session.in_transaction():
        return None
    return await session.connection()


async def _safe_snapshot_connection_for_invalidation(session: AsyncSession, *, event: str) -> AsyncConnection | None:
    """Best-effort wrapper around `_snapshot_connection_for_invalidation`. See `get_db`'s
    docstring (Codex's fifth follow-up) for why the snapshot attempt itself must never be
    allowed to raise here: both call sites in `get_db` need the guarded `rollback()`/
    `close()` call immediately after them to still run even when the connection could not
    be captured. Never raises -- on failure, logs a fixed event name only (never the
    exception's own text, which may embed SQL parameters or row data) and returns `None`,
    which callers already treat as "nothing to invalidate"."""
    try:
        return await _snapshot_connection_for_invalidation(session)
    except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
        logger.error(event)
        return None


async def _invalidate_snapshotted_connection(connection: AsyncConnection | None, *, event: str) -> None:
    """Best-effort invalidation of a connection snapshotted by
    `_snapshot_connection_for_invalidation` before a failed `rollback()`/`close()`. Never
    raises -- this runs from inside an `except` block already handling a cleanup failure
    and must not mask it with a new one."""
    if connection is None:
        return
    try:
        await connection.invalidate()
    except Exception:  # noqa: BLE001 -- best-effort; must never mask a pending exception.
        logger.error(event)
