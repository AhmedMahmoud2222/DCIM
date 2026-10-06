"""Discovery / reconciliation endpoints (master prompt §7). Read/list endpoints are
`discovery:read`; the two decision endpoints (`accept`/`reject`) are
`discovery:reconcile` -- a human decision, never automatic."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.concurrency import require_if_match
from app.application.discovery_service import accept_reconciliation, reject_reconciliation
from app.application.network import neighbor_service
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.integration.models import DiscoveredDevice, Integration, ReconciliationDiff
from app.domain.network.discovery_models import DiscoveredNeighbor

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
    profile_match: dict | None = None

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


# ------------------------------------------------------------------------ neighbors (#101)
class NeighborOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    integration_name: str | None = None
    protocol: str
    source_collector_id: uuid.UUID | None
    scan_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    status: str
    effective_status: str = "active"
    local_port_name: str | None
    local_port_ref: str | None
    remote_chassis_ident: str
    remote_chassis_subtype: str | None
    remote_port_ident: str
    remote_port_subtype: str | None
    remote_port_description: str | None
    remote_system_name: str | None
    remote_system_description: str | None
    remote_platform: str | None
    remote_management_address: str | None
    capabilities: list[str]
    native_vlan: int | None
    ttl_seconds: int | None
    raw_evidence: dict
    reconciliation_state: str
    match_evidence: dict
    local_port_id: uuid.UUID | None
    remote_port_id: uuid.UUID | None
    decided_by_user_id: uuid.UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    version: int

    model_config = {"from_attributes": True}


def _neighbor_out(neighbor: DiscoveredNeighbor, integration: Integration | None) -> NeighborOut:
    out = NeighborOut.model_validate(neighbor, from_attributes=True)
    out.integration_name = integration.name if integration else None
    out.effective_status = neighbor_service.effective_status(neighbor, integration.poll_interval_seconds if integration else None)
    return out


async def _load_neighbor_out(db: AsyncSession, neighbor_id: uuid.UUID) -> NeighborOut:
    neighbor = await db.get(DiscoveredNeighbor, neighbor_id)
    if neighbor is None:
        raise NotFoundError(f"DiscoveredNeighbor {neighbor_id} not found.")
    return _neighbor_out(neighbor, await db.get(Integration, neighbor.integration_id))


@router.get("/neighbors", response_model=Page[NeighborOut])
async def list_neighbors(
    integration_id: uuid.UUID | None = None, protocol: str | None = None, reconciliation_state: str | None = None,
    status_filter: str | None = None, page: Pagination = Depends(pagination_params), db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("discovery:read")),
) -> Page[NeighborOut]:
    conditions = []
    if integration_id is not None:
        conditions.append(DiscoveredNeighbor.integration_id == integration_id)
    if protocol is not None:
        conditions.append(DiscoveredNeighbor.protocol == protocol)
    if reconciliation_state is not None:
        conditions.append(DiscoveredNeighbor.reconciliation_state == reconciliation_state)
    if status_filter is not None:
        conditions.append(DiscoveredNeighbor.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(DiscoveredNeighbor).where(*conditions))).scalar_one()
    rows = (
        await db.execute(
            select(DiscoveredNeighbor, Integration)
            .join(Integration, Integration.id == DiscoveredNeighbor.integration_id)
            .where(*conditions)
            .order_by(DiscoveredNeighbor.last_seen_at.desc(), DiscoveredNeighbor.id)
            .limit(page.limit).offset(page.offset)
        )
    ).all()
    return Page[NeighborOut](
        items=[_neighbor_out(n, i) for n, i in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/neighbors/{neighbor_id}", response_model=NeighborOut)
async def get_neighbor(
    neighbor_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("discovery:read")),
) -> NeighborOut:
    return await _load_neighbor_out(db, neighbor_id)


class NeighborDecisionIn(BaseModel):
    local_port_id: uuid.UUID | None = None
    remote_port_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/neighbors/{neighbor_id}/rematch", response_model=NeighborOut)
async def rematch_neighbor(
    neighbor_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("discovery:reconcile")),
) -> NeighborOut:
    request_id, correlation_id = _request_ids(request)
    await neighbor_service.rematch_neighbor(
        db, neighbor_id=neighbor_id, actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id
    )
    await db.commit()
    return await _load_neighbor_out(db, neighbor_id)


@router.post("/neighbors/{neighbor_id}/confirm", response_model=NeighborOut)
async def confirm_neighbor(
    neighbor_id: uuid.UUID, body: NeighborDecisionIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("discovery:reconcile")),
) -> NeighborOut:
    request_id, correlation_id = _request_ids(request)
    await neighbor_service.confirm_neighbor(
        db, neighbor_id=neighbor_id, local_port_id=body.local_port_id, remote_port_id=body.remote_port_id,
        expected_version=if_match_version, reason=body.reason, actor_user_id=ctx.user.id, request_id=request_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    return await _load_neighbor_out(db, neighbor_id)


@router.post("/neighbors/{neighbor_id}/reject", response_model=NeighborOut)
async def reject_neighbor(
    neighbor_id: uuid.UUID, body: NeighborDecisionIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("discovery:reconcile")),
) -> NeighborOut:
    request_id, correlation_id = _request_ids(request)
    await neighbor_service.reject_neighbor(
        db, neighbor_id=neighbor_id, expected_version=if_match_version, reason=body.reason, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _load_neighbor_out(db, neighbor_id)


@router.post("/neighbors/{neighbor_id}/revoke", response_model=NeighborOut)
async def revoke_neighbor(
    neighbor_id: uuid.UUID, body: NeighborDecisionIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("discovery:reconcile")),
) -> NeighborOut:
    request_id, correlation_id = _request_ids(request)
    await neighbor_service.revoke_neighbor(
        db, neighbor_id=neighbor_id, expected_version=if_match_version, reason=body.reason, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _load_neighbor_out(db, neighbor_id)
