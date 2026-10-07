"""Serialises writers of authoritative network topology (neighbor confirmation, cables).

One transaction-scoped advisory lock keeps the "a port belongs to at most one live physical
link / one confirmed adjacency" checks race-free: two operators confirming or cabling the
same port cannot both pass the check. Writers acquire it first, before any row lock of the
tables they touch, and release it only by committing or rolling back. Never take it in a
transaction that also takes the authority lock (`authority_lock.py`); the two domains do
not overlap.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TOPOLOGY_LOCK_NAME = "dcim.network_topology"


async def acquire_topology_lock(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": TOPOLOGY_LOCK_NAME})
