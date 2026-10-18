"""PDU catalog API (M2 narrow scope)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog_designer import _request_ids, write_audit_log, write_outbox_event
from app.application.pdu_catalog.draft_service import get_pdu_draft, update_pdu_draft
from app.application.rbac import AuthContext, require_catalog_administrator, require_permission
from app.core.errors import ApiError
from app.domain.catalog.designer_models import CatalogModelRevision, PDUOutletTemplate

router = APIRouter(prefix="/catalog/revisions", tags=["pdu-catalog"])


class PDUOutletIn(BaseModel):
    stable_key: str = Field(max_length=64)
    outlet_number: int
    label: str | None = Field(default=None, max_length=128)
    outlet_type: str = "other"
    outlet_count: int = 1
    phase: str = "single"
    rated_voltage_v: float | None = None
    rated_current_a: float | None = None
    position: str | None = Field(default=None, max_length=32)
    side: str = "front"
    sort_order: int = 0


class PDUDraftUpdateIn(BaseModel):
    pdu_input_voltage_v: float | None = None
    pdu_input_current_a: float | None = None
    pdu_phase: str | None = None
    mounting_orientation: str | None = None
    outlets: list[PDUOutletIn] = []


class PDUOutletOut(BaseModel):
    id: uuid.UUID
    stable_key: str
    outlet_number: int
    label: str
    outlet_type: str
    outlet_count: int
    phase: str
    rated_voltage_v: float | None
    rated_current_a: float | None
    position: str | None
    side: str
    sort_order: int
    revision_version: int

    model_config = {"from_attributes": True}


def _outlet_out(t: PDUOutletTemplate, rev_version: int) -> PDUOutletOut:
    return PDUOutletOut(
        id=t.id,
        stable_key=t.stable_key,
        outlet_number=t.outlet_number,
        label=t.label,
        outlet_type=t.outlet_type,
        outlet_count=t.outlet_count,
        phase=t.phase,
        rated_voltage_v=t.rated_voltage_v,
        rated_current_a=t.rated_current_a,
        position=t.position,
        side=t.side,
        sort_order=t.sort_order,
        revision_version=rev_version,
    )


class PDUDraftOut(BaseModel):
    revision_id: uuid.UUID
    lifecycle_status: str
    pdu_input_voltage_v: float | None
    pdu_input_current_a: float | None
    pdu_phase: str | None
    mounting_orientation: str | None
    outlets: list[PDUOutletOut]
    version: int

    model_config = {"from_attributes": True}


@router.get("/{revision_id}/pdu", response_model=PDUDraftOut)
async def get_pdu_draft_endpoint(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:read")),
) -> PDUDraftOut:
    rev, outlets = await get_pdu_draft(db, revision_id=revision_id)
    return PDUDraftOut(
        revision_id=rev.id,
        lifecycle_status=rev.lifecycle_status,
        pdu_input_voltage_v=rev.pdu_input_voltage_v,
        pdu_input_current_a=rev.pdu_input_current_a,
        pdu_phase=rev.pdu_phase,
        mounting_orientation=rev.mounting_orientation,
        outlets=[_outlet_out(t, rev.version) for t in outlets],
        version=rev.version,
    )


@router.patch("/{revision_id}/pdu", response_model=PDUDraftOut)
async def update_pdu_draft_endpoint(
    revision_id: uuid.UUID,
    body: PDUDraftUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
    if_match: int | None = None,
) -> PDUDraftOut:
    if if_match is None:
        raise ApiError(status_code=428, title="Precondition Required", detail="If-Match header is required")
    try:
        rev, outlets = await update_pdu_draft(
            db,
            revision_id=revision_id,
            if_match_version=if_match,
            draft_updates={
                "pdu_input_voltage_v": body.pdu_input_voltage_v,
                "pdu_input_current_a": body.pdu_input_current_a,
                "pdu_phase": body.pdu_phase,
                "mounting_orientation": body.mounting_orientation,
            },
            outlets=[o.model_dump() for o in body.outlets],
            actor_user_id=ctx.user.id,
        )
    except ValueError as e:
        raise ApiError(status_code=422, title="Validation Failed", detail=str(e))
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="catalog.pdu_draft.update",
        entity_type="catalog_model_revision",
        entity_id=rev.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"outlet_count": len(outlets)},
    )
    await write_outbox_event(
        db,
        event_type="PDUDraftUpdated",
        aggregate_type="catalog_model_revision",
        aggregate_id=rev.id,
        payload={"outlet_count": len(outlets)},
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(rev)
    return PDUDraftOut(
        revision_id=rev.id,
        lifecycle_status=rev.lifecycle_status,
        pdu_input_voltage_v=rev.pdu_input_voltage_v,
        pdu_input_current_a=rev.pdu_input_current_a,
        pdu_phase=rev.pdu_phase,
        mounting_orientation=rev.mounting_orientation,
        outlets=[_outlet_out(t, rev.version) for t in outlets],
        version=rev.version,
    )
