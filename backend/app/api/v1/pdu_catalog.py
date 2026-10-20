"""PDU catalog API (M2 narrow scope)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog_designer import _request_ids, write_audit_log, write_outbox_event
from app.application.concurrency import require_if_match
from app.application.pdu_catalog.draft_service import (
    _require_pdu_revision,
    get_pdu_draft,
    update_pdu_draft,
)
from app.application.rbac import (
    AuthContext,
    get_auth_context,
    require_catalog_administrator,
)
from app.core.errors import ApiError
from app.domain.catalog.designer_models import PDUOutletTemplate

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
    pdu_rated_capacity_kw: float | None = None
    pdu_plug_type: str | None = Field(default=None, max_length=32)
    pdu_monitoring_protocol: str | None = Field(default=None, max_length=16)
    pdu_monitoring_endpoint: str | None = Field(default=None, max_length=255)
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
    pdu_rated_capacity_kw: float | None
    pdu_plug_type: str | None
    pdu_monitoring_protocol: str | None
    pdu_monitoring_endpoint: str | None
    outlets: list[PDUOutletOut]
    version: int

    model_config = {"from_attributes": True}


@router.get("/{revision_id}/pdu", response_model=PDUDraftOut)
async def get_pdu_draft_endpoint(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> PDUDraftOut:
    """Conditional-permission PDU draft read (spec §10): `catalog:read_draft` when the
    revision is a draft, `catalog:read` once it is published or retired — matching
    `GET /revisions/{id}`'s own gate, so a caller without draft access cannot learn a
    PDU draft's electrical specification or outlet table through this sub-resource."""
    revision = await _require_pdu_revision(db, revision_id=revision_id)
    required = "catalog:read_draft" if revision.lifecycle_status == "draft" else "catalog:read"
    if not ctx.has_permission(required):
        raise ApiError(status_code=403, title="Forbidden", detail=f"Missing required permission: {required}")
    rev, outlets = await get_pdu_draft(db, revision_id=revision_id)
    return PDUDraftOut(
        revision_id=rev.id,
        lifecycle_status=rev.lifecycle_status,
        pdu_input_voltage_v=rev.pdu_input_voltage_v,
        pdu_input_current_a=rev.pdu_input_current_a,
        pdu_phase=rev.pdu_phase,
        mounting_orientation=rev.mounting_orientation,
        pdu_rated_capacity_kw=rev.pdu_rated_capacity_kw,
        pdu_plug_type=rev.pdu_plug_type,
        pdu_monitoring_protocol=rev.pdu_monitoring_protocol,
        pdu_monitoring_endpoint=rev.pdu_monitoring_endpoint,
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
    if_match_version: int = Depends(require_if_match),
) -> PDUDraftOut:
    try:
        # Partial PATCH semantics: only fields the caller explicitly sent are applied.
        # `exclude_unset=True` keeps omitted scalars (rated capacity, plug type,
        # monitoring attributes) untouched instead of silently erasing them, and an
        # omitted `outlets` key preserves the existing outlet templates.
        payload = body.model_dump(exclude_unset=True)
        outlets_provided = "outlets" in payload
        rev, outlets = await update_pdu_draft(
            db,
            revision_id=revision_id,
            if_match_version=if_match_version,
            draft_updates={k: v for k, v in payload.items() if k != "outlets"},
            outlets=payload["outlets"] if outlets_provided else None,
            actor_user_id=ctx.user.id,
        )
    except ValueError as e:
        raise ApiError(status_code=422, title="Validation Failed", detail=str(e)) from e
    except IntegrityError as e:
        # Any constraint that slipped past validation (unique, numeric range) must not
        # surface as a raw PostgreSQL 500; report it as a controlled conflict instead.
        await db.rollback()
        raise ApiError(status_code=409, title="Conflict", detail=str(e.orig) if getattr(e, "orig", None) else str(e)) from e
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
        pdu_rated_capacity_kw=rev.pdu_rated_capacity_kw,
        pdu_plug_type=rev.pdu_plug_type,
        pdu_monitoring_protocol=rev.pdu_monitoring_protocol,
        pdu_monitoring_endpoint=rev.pdu_monitoring_endpoint,
        outlets=[_outlet_out(t, rev.version) for t in outlets],
        version=rev.version,
    )
