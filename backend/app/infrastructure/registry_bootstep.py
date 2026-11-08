"""Abort worker startup before consumption if registry code/DB disagree."""

import asyncio

from celery import bootsteps
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.application.registry_ledger import verify_registry_ledger
from app.core.config import get_settings


async def verify_worker_registry() -> None:
    # A private nonpooled engine avoids inheriting an API loop/connection across
    # worker forks. Dispose it even when a read or contract check fails.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        await asyncio.wait_for(verify_registry_ledger(engine), timeout=30)
    finally:
        await engine.dispose()


class RegistryLedgerGate(bootsteps.StartStopStep):
    # Consumer is Celery's last worker step; this check runs after Pool setup and
    # before any task is consumed. Bootstep errors propagate, unlike signals.
    requires = ("celery.worker.components:Pool",)

    def start(self, worker) -> None:
        asyncio.run(verify_worker_registry())
