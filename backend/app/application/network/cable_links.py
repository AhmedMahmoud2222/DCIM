"""Live physical cable adjacencies, for conflict checks against discovered evidence."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.network.cable_models import Cable, CableEndpoint


async def live_cable_links(db: AsyncSession, port_ids: list[uuid.UUID]) -> list[dict]:
    """Every live cable touching any of `port_ids`, as {"kind", "id", "ports": [sorted pair]}."""
    near = aliased(CableEndpoint)
    far = aliased(CableEndpoint)
    rows = await db.execute(
        select(Cable.id, near.equipment_port_id, far.equipment_port_id)
        .join(near, near.cable_id == Cable.id)
        .join(far, (far.cable_id == Cable.id) & (far.id != near.id))
        .where(Cable.is_live.is_(True), near.equipment_port_id.in_(port_ids))
    )
    links: dict[uuid.UUID, list[str]] = {}
    for cable_id, near_port, far_port in rows:
        links[cable_id] = sorted([str(near_port), str(far_port)])
    return [{"kind": "cable", "id": str(cable_id), "ports": ports} for cable_id, ports in links.items()]
