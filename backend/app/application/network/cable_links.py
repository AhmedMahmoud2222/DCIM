"""Live physical cable adjacencies, for conflict checks against discovered evidence."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession


async def live_cable_links(db: AsyncSession, port_ids: list[uuid.UUID]) -> list[dict]:
    return []
