"""Discovery / reconciliation endpoints (master prompt §7). Read/list endpoints are
`discovery:read`; the two decision endpoints (`accept`/`reject`) are
`discovery:reconcile` -- a human decision, never automatic."""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.discovery_service import accept_reconciliation, reject_reconciliation
from app.application.rbac import require_permission
from app.domain.integration.models import DiscoveredDevice, ReconciliationDiff

router = APIRouter(prefix="/discovery", tags=["discovery"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class DiscoveredDeviceOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    external_identifier: str
    status: str
    matched_managed_asset_id: uuid.UUID | None
    raw_attributes: dict

    model_config = {"from_attributes": True}


@router.get("/devices", response_model=list[DiscoveredDeviceOut])
async def list_discovered_devices(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("discovery:read")),
) -> list[DiscoveredDeviceOut]:
    rows = (await db.execute(select(DiscoveredDevice))).scalars().all()
    return [DiscoveredDeviceOut.model_validate(r) for r in rows]


class ReconciliationDiffOut(BaseModel):
    id: uuid.UUID
    discovered_device_id: uuid.UUID
    diff_type: str
    status: str
    field_name: str | None
    discovered_value: str | None
    authoritative_value: str | None

    model_config = {"from_attributes": True}


@router.get("/reconciliation", response_model=list[ReconciliationDiffOut])
async def list_reconciliation_diffs(
    status_filter: str | None = None, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("discovery:read")),
) -> list[ReconciliationDiffOut]:
    stmt = select(ReconciliationDiff)
    if status_filter is not None:
        stmt = stmt.where(ReconciliationDiff.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [ReconciliationDiffOut.model_validate(r) for r in rows]


class ReconciliationDecisionIn(BaseModel):
    matched_managed_asset_id: uuid.UUID | None = None
    reason: str | None = None


@router.post("/reconciliation/{diff_id}/accept", response_model=ReconciliationDiffOut)
async def accept_diff(
    diff_id: uuid.UUID, body: ReconciliationDecisionIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("discovery:reconcile")),
) -> ReconciliationDiffOut:
    request_id, correlation_id = _request_ids(request)
    diff = await accept_reconciliation(
        db, diff_id=diff_id, matched_managed_asset_id=body.matched_managed_asset_id, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id, reason=body.reason,
    )
    await db.commit()
    return ReconciliationDiffOut.model_validate(diff)


@router.post("/reconciliation/{diff_id}/reject", response_model=ReconciliationDiffOut)
async def reject_diff(
    diff_id: uuid.UUID, body: ReconciliationDecisionIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("discovery:reconcile")),
) -> ReconciliationDiffOut:
    request_id, correlation_id = _request_ids(request)
    diff = await reject_reconciliation(
        db, diff_id=diff_id, actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id, reason=body.reason,
    )
    await db.commit()
    return ReconciliationDiffOut.model_validate(diff)
