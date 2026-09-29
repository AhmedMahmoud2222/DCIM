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
    Guarded here with the same fixed-field-only discipline, then the ORIGINAL
    exception is re-raised (Python 3 restores the enclosing except's exception state
    once the nested `except` below exits) so callers see exactly what they raised."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
                logger.error("db_session_cleanup_rollback_failed")
            raise
