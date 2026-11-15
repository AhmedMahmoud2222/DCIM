"""ManagedAsset foundation endpoints (§9/§10/§25 of the Phase 1 prompt). Only the bare
identity/lifecycle anchor is exposed here — no Rack/Equipment/PDU subtype is attached
(Phase 2/3/7). Create demonstrates the Idempotency-Key foundation (§21); lifecycle
transition demonstrates auditable, architecture-defined state changes only (§10: "do not
invent lifecycle states")."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.access_control import (
    AccessScope,
    ensure_equipment_access,
    ensure_rack_access,
    equipment_visible_clause,
    rack_visible_clause,
)
from app.application.audit_service import write_audit_log
from app.application.idempotency import (
    ClaimRef,
    IdempotencyConflict,
    IdempotencyStillProcessing,
    complete_claim,
    get_or_claim,
    hash_request_body,
    release_claim,
    user_principal,
)
from app.application.lifecycle_settings import get_configured_allowed_transitions
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.audit.models import AuditLog
from app.domain.auth.models import User
from app.domain.identity.models import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    ASSET_TYPES,
    LEGACY_LIFECYCLE_STATUSES,
    ManagedAsset,
)

router = APIRouter(prefix="/managed-assets", tags=["managed-assets"])


class ManagedAssetIn(BaseModel):
    # Finding M1 (docs/history/PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md / docs/history/PHASE1_CORRECTION_REPORT.md):
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
    to_status: str = Field(min_length=1, max_length=32)
    reason: str | None = Field(default=None, max_length=500)


class LifecycleHistoryEntryOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    from_status: str | None
    to_status: str
    reason: str | None
    actor_user_id: uuid.UUID | None
    actor_name: str | None
    timestamp: datetime


class LifecycleHistoryListOut(BaseModel):
    items: list[LifecycleHistoryEntryOut]


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
            outcome = await get_or_claim(
                db, principal=user_principal(ctx.user.id), key=idempotency_key,
                endpoint="POST:/managed-assets", request_hash=request_hash,
            )
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
    claim_ref = ClaimRef.of(claim) if claim is not None else None

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
        if claim_ref is not None:
            await release_claim(db, claim_ref)
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
    if not ctx.scope.unrestricted:
        # Restrict to equipment or rack assets visible under ctx.scope;
        # unsupported asset types fail closed for restricted callers.
        visibility = or_(
            and_(ManagedAsset.asset_type == "equipment", equipment_visible_clause(ctx.scope, ManagedAsset.id)),
            and_(ManagedAsset.asset_type == "rack", rack_visible_clause(ctx.scope, ManagedAsset.id)),
        )
        stmt = stmt.where(visibility)
        count_stmt = count_stmt.where(visibility)
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


async def _ensure_managed_asset_scope(
    db: AsyncSession, scope: AccessScope, asset: ManagedAsset
) -> None:
    if scope.unrestricted:
        return
    try:
        if asset.asset_type == "equipment":
            await ensure_equipment_access(db, scope, asset.id)
        elif asset.asset_type == "rack":
            await ensure_rack_access(db, scope, asset.id)
        else:
            raise NotFoundError(f"ManagedAsset {asset.id} not found.")
    except NotFoundError:
        raise NotFoundError(f"ManagedAsset {asset.id} not found.") from None


async def _load_accessible_managed_asset(
    db: AsyncSession, scope: AccessScope, asset_id: uuid.UUID
) -> ManagedAsset:
    """Load a ManagedAsset and enforce site/rack AccessScope fail-closed.

    Raises an identical NotFoundError whether the asset does not exist, has no
    resolvable placement, lies outside the caller's permitted sites/racks, or has an
    asset_type without a verified scope-resolution path.
    """
    asset = await db.get(ManagedAsset, asset_id)
    if asset is None:
        raise NotFoundError(f"ManagedAsset {asset_id} not found.")
    await _ensure_managed_asset_scope(db, scope, asset)
    return asset


@router.get("/{asset_id}", response_model=ManagedAssetOut)
async def get_managed_asset(
    asset_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("managed_asset:read"))
) -> ManagedAsset:
    return await _load_accessible_managed_asset(db, ctx.scope, asset_id)


@router.post("/{asset_id}/lifecycle-transition", response_model=ManagedAssetOut)
async def transition_lifecycle(
    asset_id: uuid.UUID,
    body: LifecycleTransitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("managed_asset:update_lifecycle")),
) -> ManagedAsset:
    # Preliminary scope check before acquiring a row lock so unauthorized callers
    # fail closed immediately without contending on locks.
    await _load_accessible_managed_asset(db, ctx.scope, asset_id)
    # Lock the row, then re-verify scope in case a concurrent placement change
    # moved the asset out of scope before the lock was acquired.
    asset = (
        await db.execute(
            select(ManagedAsset).where(ManagedAsset.id == asset_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if asset is None:
        raise NotFoundError(f"ManagedAsset {asset_id} not found.")
    await _ensure_managed_asset_scope(db, ctx.scope, asset)

    is_equipment = asset.asset_type == "equipment"
    if is_equipment and asset.lifecycle_status in LEGACY_LIFECYCLE_STATUSES:
        raise ApiError(
            status_code=422,
            title="Invalid Lifecycle Transition",
            detail=(
                f"Equipment in legacy lifecycle state {asset.lifecycle_status!r} is read-only; "
                "transitions out of a legacy state are not permitted."
            ),
        )

    allowed = ALLOWED_LIFECYCLE_TRANSITIONS.get(asset.lifecycle_status, set())
    if body.to_status not in allowed:
        raise ApiError(
            status_code=422,
            title="Invalid Lifecycle Transition",
            detail=f"Cannot transition from {asset.lifecycle_status!r} to {body.to_status!r}. Allowed: {sorted(allowed)}",
        )

    # Administrator lifecycle settings describe equipment only; racks, PDUs, cooling units and other
    # asset types keep the universal domain transition table.
    if is_equipment:
        configured_allowed = await get_configured_allowed_transitions(db, asset.lifecycle_status)
        if body.to_status not in configured_allowed:
            raise ApiError(
                status_code=422,
                title="Invalid Lifecycle Transition",
                detail=(
                    f"Transition from {asset.lifecycle_status!r} to {body.to_status!r} is disabled by "
                    f"lifecycle settings. Allowed: {sorted(configured_allowed)}"
                ),
            )

    clean_reason = body.reason.strip() if body.reason is not None else None
    if body.to_status in ("decommissioned", "removed"):
        if is_equipment and not clean_reason:
            raise ApiError(
                status_code=422,
                title="Reason Required",
                detail=f"A reason is required when transitioning equipment to {body.to_status!r}.",
            )
        # Omitted or explicit-null reason stays accepted for non-equipment assets (client compatibility);
        # only a supplied-but-blank string is rejected.
        if body.reason is not None and not clean_reason:
            raise ApiError(
                status_code=422,
                title="Reason Required",
                detail=f"A non-empty reason is required when transitioning to {body.to_status!r}.",
            )

    before_status = asset.lifecycle_status
    asset.lifecycle_status = body.to_status
    if body.to_status == "decommissioned":
        # The existing column is TIMESTAMP WITHOUT TIME ZONE. Store naive UTC to
        # match that contract; asyncpg rejects an aware datetime for this column.
        asset.decommissioned_at = datetime.now(UTC).replace(tzinfo=None)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Issue #105: the database refuses to retire a cooling unit that is still related to a thermal zone.
        await db.rollback()
        if "restrict_violation" in str(exc.orig) or "still related to thermal zones" in str(exc.orig):
            raise ConflictError(detail="Cooling unit still has thermal-zone relationships; remove them first.") from exc
        raise

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
        reason=clean_reason,
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


@router.get("/{asset_id}/lifecycle-history", response_model=LifecycleHistoryListOut)
async def get_managed_asset_lifecycle_history(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("managed_asset:read")),
) -> LifecycleHistoryListOut:
    await _load_accessible_managed_asset(db, ctx.scope, asset_id)
    rows = (
        await db.execute(
            select(AuditLog, User)
            .outerjoin(User, AuditLog.actor_user_id == User.id)
            .where(
                AuditLog.entity_type == "managed_asset",
                AuditLog.entity_id == asset_id,
                AuditLog.action == "managed_asset.lifecycle_transition",
            )
            .order_by(AuditLog.timestamp.desc(), AuditLog.audit_id.desc())
        )
    ).all()
    items = [
        LifecycleHistoryEntryOut(
            id=audit_row.audit_id,
            asset_id=asset_id,
            from_status=(audit_row.before or {}).get("lifecycle_status") if isinstance(audit_row.before, dict) else None,
            to_status=((audit_row.after or {}).get("lifecycle_status") if isinstance(audit_row.after, dict) else None) or "",
            reason=audit_row.reason,
            actor_user_id=audit_row.actor_user_id,
            actor_name=(actor.full_name or actor.email) if actor is not None else None,
            timestamp=audit_row.timestamp,
        )
        for audit_row, actor in rows
    ]
    return LifecycleHistoryListOut(items=items)
