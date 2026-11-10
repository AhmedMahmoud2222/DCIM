import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import engine, settings


async def check_database(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_redis() -> bool:
    settings = get_settings()
    client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


def database_pool_snapshot() -> dict[str, int]:
    """Return fixed database-pool gauges without querying PostgreSQL."""
    pool = engine.pool
    return {
        "size": int(pool.size()),
        "checked_out": int(pool.checkedout()),
        "overflow_in_use": max(0, int(pool.overflow())),
        "max_overflow": settings.database_max_overflow,
    }
