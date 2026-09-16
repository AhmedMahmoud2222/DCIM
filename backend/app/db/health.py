import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings


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
