"""Run an async body from a sync Celery task on its own thread, event loop and short-lived engine.

asyncpg connections belong to the loop that created them, and tests invoke task bodies from inside a
running loop, so a bare `asyncio.run` would fail there. Same reasoning as the bulk-import tasks."""

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


async def _with_fresh_session(body: Callable[[AsyncSession], Awaitable[None]]) -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as db:
            await body(db)
    finally:
        await engine.dispose()


def run_with_session(body: Callable[[AsyncSession], Awaitable[None]]) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _with_fresh_session(body)).result()
