from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    best-effort `invalidate()`, independent of which branch got here."""
    session = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
            logger.error("db_session_cleanup_rollback_failed")
            try:
                await session.invalidate()
            except Exception:  # noqa: BLE001 -- best-effort; must never mask the original exception.
                logger.error("db_session_cleanup_invalidate_failed")
        raise
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
            logger.error("db_session_cleanup_close_failed")
            try:
                await session.invalidate()
            except Exception:  # noqa: BLE001 -- best-effort; must never mask a pending exception.
                logger.error("db_session_cleanup_close_invalidate_failed")
