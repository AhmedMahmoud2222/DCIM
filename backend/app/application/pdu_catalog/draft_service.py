"""PDU draft persistence service (M2)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog_designer_service import lock_draft_revision_for_edit
from app.core.errors import NotFoundError
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, PDUOutletTemplate

from .validators import validate_pdu_revision


async def _require_pdu_revision(db: AsyncSession, *, revision_id: uuid.UUID) -> CatalogModelRevision:
    """Load a revision and reject anything that is not a PDU-category revision.

    The `/catalog/revisions/{id}/pdu` sub-resource only exists for `category == 'pdu'`
    models - mirroring `equipment_instantiation_service`'s category gate, but as a 404
    (no such representation) rather than a 422, so a rack/equipment revision id cannot be
    probed, read or mutated through the PDU endpoint at all."""
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    model = await db.get(CatalogModel, revision.catalog_model_id)
    if model is None or model.category != "pdu":
        raise NotFoundError(
            f"CatalogModelRevision {revision_id} has no PDU representation "
            "(only revisions of 'pdu' category models expose the /pdu sub-resource)."
        )
    return revision


async def get_pdu_draft(
    db: AsyncSession,
    *,
    revision_id: uuid.UUID,
) -> tuple[CatalogModelRevision, list[PDUOutletTemplate]]:
    """Load a PDU revision's spec and its outlet templates. Readable for any lifecycle
    status (draft, published, retired) -- publication immutability is enforced at the
    PATCH path (`lock_draft_revision_for_edit`) and at instantiation, not here. The
    category gate (404 for non-`pdu` revisions) still applies."""
    revision = await _require_pdu_revision(db, revision_id=revision_id)
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
    # Category gate first: a non-PDU revision must never take the draft row lock or the
    # optimistic-concurrency path below - it 404s before any If-Match comparison happens.
    revision = await _require_pdu_revision(db, revision_id=revision_id)
    outlets_in = outlets or []

    # Validate before taking the row lock: an invalid draft must not consume a version
    # increment or block concurrent writers. Apply draft_updates to the in-memory
    # revision copy for validation purposes only.
    if draft_updates:
        for k, v in draft_updates.items():
            if hasattr(revision, k):
                setattr(revision, k, v)
    s = validate_pdu_revision(
        {
            k: getattr(revision, k)
            for k in [
                "pdu_input_voltage_v",
                "pdu_input_current_a",
                "pdu_phase",
                "mounting_orientation",
                "pdu_rated_capacity_kw",
                "pdu_plug_type",
                "pdu_monitoring_protocol",
                "pdu_monitoring_endpoint",
            ]
        },
        outlets_in,
    )
    if not s.valid:
        raise ValueError(s.model_dump_json())

    # Lock the draft revision (version check + increment). The lock may load a fresh
    # copy of the row, so re-apply the validated updates against the locked object.
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    if draft_updates:
        for k, v in draft_updates.items():
            if hasattr(revision, k):
                setattr(revision, k, v)

    # Database-safe deletion/replacement: flush the deletes first so the unique
    # constraint slots they free are visible before any new row is inserted. This avoids
    # transient uniqueness violations on `uq_pdu_outlet_template_cmr_on` when a repeated
    # edit re-uses outlet numbers (or stable keys) from the previous generation, while
    # keeping the whole replacement inside one transaction (atomicity preserved - any
    # failure rolls back deletes and inserts together).
    existing = (await db.execute(
        select(PDUOutletTemplate).where(PDUOutletTemplate.catalog_model_revision_id == revision_id)
    )).scalars().all()
    for e in existing:
        await db.delete(e)
    await db.flush()
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
