"""ManagedAsset foundation endpoints (§9/§10/§25 of the Phase 1 prompt). Only the bare
identity/lifecycle anchor is exposed here — no Rack/Equipment/PDU subtype is attached
(Phase 2/3/7). Create demonstrates the Idempotency-Key foundation (§21); lifecycle
transition demonstrates auditable, architecture-defined state changes only (§10: "do not
invent lifecycle states")."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.idempotency import (
    IdempotencyConflict,
    IdempotencyStillProcessing,
    complete_claim,
    get_or_claim,
    hash_request_body,
    release_claim,
)
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import ApiError, NotFoundError
from app.domain.identity.models import ALLOWED_LIFECYCLE_TRANSITIONS, ASSET_TYPES, ManagedAsset

router = APIRouter(prefix="/managed-assets", tags=["managed-assets"])


class ManagedAssetIn(BaseModel):
    # Finding M1 (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md / PHASE1_CORRECTION_REPORT.md):
    # max_length here matches each column's actual width (app/domain/identity/models.py)
    # so an oversized field is rejected as a clean 422 at the API boundary, instead of
    # reaching the database and surfacing as an unhandled 500 when Postgres rejects it.
    asset_type: str = Field(max_length=32)
    asset_tag: str = Field(max_length=64)
    serial_number: str | None = Field(default=None, max_length=128)


class ManagedAssetOut(BaseModel):
    id: uuid.UUID
    asset_type: str
    asset_tag: str
    serial_number: str | None
    lifecycle_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class LifecycleTransitionIn(BaseModel):
    to_status: str
    reason: str | None = None


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


@router.post("", response_model=ManagedAssetOut, status_code=201)
async def create_managed_asset(
    body: ManagedAssetIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx=Depends(require_permission("managed_asset:manage")),
) -> ManagedAssetOut:
    if body.asset_type not in ASSET_TYPES:
        raise ApiError(status_code=422, title="Validation Error", detail=f"asset_type must be one of {ASSET_TYPES}")

    request_hash = hash_request_body(body.model_dump())
    claim = None
    if idempotency_key is not None:
        try:
            outcome = await get_or_claim(db, key=idempotency_key, endpoint="POST:/managed-assets", request_hash=request_hash)
        except IdempotencyConflict as exc:
            raise ApiError(
                status_code=422,
                title="Idempotency Key Reused",
                detail="This Idempotency-Key was already used with a different request body.",
            ) from exc
        except IdempotencyStillProcessing as exc:
            raise ApiError(
                status_code=503,
                title="Request Still Processing",
                detail="An identical request with this Idempotency-Key is still being processed. Retry shortly.",
            ) from exc
        if outcome.cached is not None:
            assert outcome.cached.response_body is not None  # only unset while status='processing'
            return ManagedAssetOut(**outcome.cached.response_body)
        claim = outcome.claim

    # Captured now, before any rollback can expire `claim`'s attributes — accessing an
    # expired ORM attribute triggers an implicit lazy-load, which cannot run outside an
    # awaited context and would raise MissingGreenlet inside the except block below.
    claim_id = claim.id if claim is not None else None

    try:
        asset = ManagedAsset(asset_type=body.asset_type, asset_tag=body.asset_tag, serial_number=body.serial_number)
        db.add(asset)
        await db.flush()

        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db,
            actor_user_id=ctx.user.id,
            action="managed_asset.create",
            entity_type="managed_asset",
            entity_id=asset.id,
            request_id=request_id,
            correlation_id=correlation_id,
            after={"asset_type": asset.asset_type, "asset_tag": asset.asset_tag, "lifecycle_status": asset.lifecycle_status},
        )
        await write_outbox_event(
            db,
            event_type="ManagedAssetCreated",
            aggregate_type="managed_asset",
            aggregate_id=asset.id,
            payload={"asset_type": asset.asset_type, "asset_tag": asset.asset_tag},
            correlation_id=correlation_id,
        )

        out = ManagedAssetOut.model_validate(asset)
        if claim is not None:
            await complete_claim(db, claim, response_status=201, response_body=out.model_dump(mode="json"))

        await db.commit()
        return out
    except Exception:
        await db.rollback()
        if claim_id is not None:
            await release_claim(db, claim_id)
        raise


@router.get("", response_model=Page[ManagedAssetOut])
async def list_managed_assets(
    asset_type: str | None = None,
    lifecycle_status: str | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("managed_asset:read")),
) -> Page:
    stmt = select(ManagedAsset)
    count_stmt = select(func.count()).select_from(ManagedAsset)
    if asset_type is not None:
        stmt = stmt.where(ManagedAsset.asset_type == asset_type)
        count_stmt = count_stmt.where(ManagedAsset.asset_type == asset_type)
    if lifecycle_status is not None:
        stmt = stmt.where(ManagedAsset.lifecycle_status == lifecycle_status)
        count_stmt = count_stmt.where(ManagedAsset.lifecycle_status == lifecycle_status)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(ManagedAsset.created_at.desc()).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{asset_id}", response_model=ManagedAssetOut)
async def get_managed_asset(
    asset_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("managed_asset:read"))
) -> ManagedAsset:
    asset = await db.get(ManagedAsset, asset_id)
    if asset is None:
        raise NotFoundError(f"ManagedAsset {asset_id} not found.")
    return asset


@router.post("/{asset_id}/lifecycle-transition", response_model=ManagedAssetOut)
async def transition_lifecycle(
    asset_id: uuid.UUID,
    body: LifecycleTransitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("managed_asset:update_lifecycle")),
) -> ManagedAsset:
    asset = await db.get(ManagedAsset, asset_id)
    if asset is None:
        raise NotFoundError(f"ManagedAsset {asset_id} not found.")

    allowed = ALLOWED_LIFECYCLE_TRANSITIONS.get(asset.lifecycle_status, set())
    if body.to_status not in allowed:
        raise ApiError(
            status_code=422,
            title="Invalid Lifecycle Transition",
            detail=f"Cannot transition from {asset.lifecycle_status!r} to {body.to_status!r}. Allowed: {sorted(allowed)}",
        )

    before_status = asset.lifecycle_status
    asset.lifecycle_status = body.to_status
    if body.to_status == "decommissioned":
        asset.decommissioned_at = datetime.now(UTC)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="managed_asset.lifecycle_transition",
        entity_type="managed_asset",
        entity_id=asset.id,
        request_id=request_id,
        correlation_id=correlation_id,
        before={"lifecycle_status": before_status},
        after={"lifecycle_status": asset.lifecycle_status},
        reason=body.reason,
    )
    await write_outbox_event(
        db,
        event_type="ManagedAssetLifecycleChanged",
        aggregate_type="managed_asset",
        aggregate_id=asset.id,
        payload={"from": before_status, "to": asset.lifecycle_status},
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(asset)
    return asset
