"""Builds the synthetic stand-in world (3 DEMO-SITE sites + HRC) in the database named by DATABASE_URL.

DISPOSABLE DATABASES ONLY. Run from backend/ with PYTHONPATH=backend:repo-root. Used by run.sh."""

import asyncio
import importlib

from app.db.session import AsyncSessionLocal
from app.domain.auth.models import User
from tests.demo_world import build_world


async def main() -> None:
    importlib.import_module("app.db.models")  # register every ORM model on the shared metadata
    async with AsyncSessionLocal() as db:
        db.add(User(email="bench@example.com", password_hash="x", full_name="Bench"))
        await db.commit()
        await build_world(db, racks_per_site=4, devices_per_rack=5)


asyncio.run(main())
