"""PDU draft persistence service (M2)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog_designer_service import lock_draft_revision_for_edit
from app.core.errors import ConflictError, NotFoundError
from app.domain.catalog.designer_models import CatalogModelRevision, PDUOutletTemplate

from .validators import validate_pdu_revision


async def get_pdu_draft(
    db: AsyncSession,
    *,
    revision_id: uuid.UUID,
) -> tuple[CatalogModelRevision, list[PDUOutletTemplate]]:
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    if revision.lifecycle_status != "draft":
        raise ConflictError(detail="This revision is published and immutable.")
    outlets = (await db.execute(
        select(PDUOutletTemplate).where(PDUOutletTemplate.catalog_model_revision_id == revision_id).order_by(
            PDUOutletTemplate.sort_order, PDUOutletTemplate.outlet_number
        )
    )).scalars().all()
    return revision, list(outlets)


async def update_pdu_draft(
    db: AsyncSession,
    *,
    revision_id: uuid.UUID,
    if_match_version: int,
    draft_updates: dict[str, Any] | None = None,
    outlets: list[dict[str, Any]] | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[CatalogModelRevision, list[PDUOutletTemplate]]:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    if draft_updates:
        for k, v in draft_updates.items():
            if hasattr(revision, k):
                setattr(revision, k, v)
    # validate before touching outlet templates
    outlets_in = outlets or []
    s = validate_pdu_revision(
        {k: getattr(revision, k) for k in ["pdu_input_voltage_v","pdu_input_current_a","pdu_phase","mounting_orientation","pdu_mounting_orientation"]},
        outlets_in,
    )
    if not s.valid:
        raise ValueError(s.model_dump_json())
    # delete all and recreate for simplicity in this bounded slice
    existing = (await db.execute(
        select(PDUOutletTemplate).where(PDUOutletTemplate.catalog_model_revision_id == revision_id)
    )).scalars().all()
    for e in existing:
        await db.delete(e)
    for o in outlets_in:
        t = PDUOutletTemplate(
            catalog_model_revision_id=revision_id,
            stable_key=o.get("stable_key"),
            outlet_number=o.get("outlet_number"),
            label=o.get("label") or str(o.get("outlet_number")),
            outlet_type=o.get("outlet_type") or "other",
            outlet_count=int(o.get("outlet_count") or 1),
            phase=o.get("phase") or "single",
            rated_voltage_v=o.get("rated_voltage_v"),
            rated_current_a=o.get("rated_current_a"),
            position=o.get("position"),
            side=o.get("side") or "front",
            sort_order=int(o.get("sort_order") or 0),
        )
        db.add(t)
    await db.flush()
    outlets_out = (await db.execute(
        select(PDUOutletTemplate).where(PDUOutletTemplate.catalog_model_revision_id == revision_id).order_by(
            PDUOutletTemplate.sort_order, PDUOutletTemplate.outlet_number
        )
    )).scalars().all()
    return revision, list(outlets_out)
