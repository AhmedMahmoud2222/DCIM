"""Synchronous engine for Celery tasks — Celery's worker model is sync-first, so tasks
use this rather than threading the async engine through a sync execution context."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg", "postgresql+psycopg")


settings = get_settings()
sync_engine = create_engine(_sync_url(settings.database_url), pool_pre_ping=True, future=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False, autoflush=False)


def get_sync_db() -> Session:
    return SyncSessionLocal()
