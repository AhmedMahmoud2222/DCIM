"""Rack endpoints (ARCHITECTURE_REVIEW.md §4b/§7c/§8). Create/update/list/retrieve the
`Rack` ManagedAsset subtype; `move`/`retire` own placement via the locked close-then-open
transaction (app/application/placement_service.py); `elevation` is computed on every
read from EquipmentPlacement, never stored (§12/§7c)."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match, require_if_match
from app.application.idempotency import (
    IdempotencyConflict,
    IdempotencyStillProcessing,
    complete_claim,
    get_or_claim,
    hash_request_body,
    release_claim,
)
from app.application.outbox_service import write_outbox_event
from app.application.placement_service import PlacementConflict, get_current_rack_placement, move_rack, retire_rack_placement
from app.application.rbac import require_permission
from app.application.spatial_validation import validate_coordinate, validate_rotation_degrees
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Rack

router = APIRouter(prefix="/racks", tags=["racks"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class RackIn(BaseModel):
    asset_tag: str = Field(max_length=64)
    model_revision_id: uuid.UUID
    name: str = Field(max_length=128)
    owner: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    # Optional initial placement, opened in the same request if provided.
    room_id: uuid.UUID | None = None
    x_mm: int | None = None
    y_mm: int | None = None
    rotation_deg: int | None = None


class RackUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    owner: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class RackPlacementOut(BaseModel):
    room_id: uuid.UUID
    x_mm: int | None
    y_mm: int | None
    rotation_deg: int | None
    effective_from: datetime

    model_config = {"from_attributes": True}


class RackOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    lifecycle_status: str
    model_revision_id: uuid.UUID
    name: str
    owner: str | None
    notes: str | None
    version: int
    created_at: datetime
    placement: RackPlacementOut | None = None


class RackMoveIn(BaseModel):
    room_id: uuid.UUID
    x_mm: int | None = None
    y_mm: int | None = None
    rotation_deg: int | None = None


async def _serialize_rack(db: AsyncSession, rack: Rack, asset: ManagedAsset) -> RackOut:
    placement = await get_current_rack_placement(db, rack.id)
    return RackOut(
        id=rack.id,
        asset_tag=asset.asset_tag,
        lifecycle_status=asset.lifecycle_status,
        model_revision_id=rack.model_revision_id,
        name=rack.name,
        owner=rack.owner,
        notes=rack.notes,
        version=rack.version,
        created_at=rack.created_at,
        placement=RackPlacementOut.model_validate(placement) if placement is not None else None,
    )


@router.post("", response_model=RackOut, status_code=201)
async def create_rack(
    body: RackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx=Depends(require_permission("rack:manage")),
) -> RackOut:
    validate_coordinate("x_mm", body.x_mm)
    validate_coordinate("y_mm", body.y_mm)
    validate_rotation_degrees(body.rotation_deg)

    request_hash = hash_request_body(body.model_dump(mode="json"))
    claim = None
    if idempotency_key is not None:
        try:
            outcome = await get_or_claim(db, key=idempotency_key, endpoint="POST:/racks", request_hash=request_hash)
        except IdempotencyConflict as exc:
            raise ApiError(
                status_code=422, title="Idempotency Key Reused",
                detail="This Idempotency-Key was already used with a different request body.",
            ) from exc
        except IdempotencyStillProcessing as exc:
            raise ApiError(
                status_code=503, title="Request Still Processing",
                detail="An identical request with this Idempotency-Key is still being processed. Retry shortly.",
            ) from exc
        if outcome.cached is not None:
            assert outcome.cached.response_body is not None
            return RackOut(**outcome.cached.response_body)
        claim = outcome.claim
    claim_id = claim.id if claim is not None else None

    try:
        if await db.get(RackModelRevision, body.model_revision_id) is None:
            raise NotFoundError(f"RackModelRevision {body.model_revision_id} not found.")

        asset = ManagedAsset(asset_type="rack", asset_tag=body.asset_tag, lifecycle_status="planned")
        db.add(asset)
        await db.flush()
        rack = Rack(
            id=asset.id, model_revision_id=body.model_revision_id, name=body.name, owner=body.owner, notes=body.notes,
        )
        db.add(rack)
        await db.flush()

        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="rack.create", entity_type="rack", entity_id=rack.id,
            request_id=request_id, correlation_id=correlation_id,
            after={"asset_tag": asset.asset_tag, "name": rack.name},
        )
        await write_outbox_event(
            db, event_type="RackCreated", aggregate_type="rack", aggregate_id=rack.id,
            payload={"asset_tag": asset.asset_tag, "name": rack.name}, correlation_id=correlation_id,
        )

        if body.room_id is not None:
            await move_rack(db, rack_id=rack.id, room_id=body.room_id, x_mm=body.x_mm, y_mm=body.y_mm, rotation_deg=body.rotation_deg)
            await write_outbox_event(
                db, event_type="RackMoved", aggregate_type="rack", aggregate_id=rack.id,
                payload={"room_id": str(body.room_id)}, correlation_id=correlation_id,
            )

        out = await _serialize_rack(db, rack, asset)
        if claim is not None:
            await complete_claim(db, claim, response_status=201, response_body=out.model_dump(mode="json"))
        await db.commit()
        return out
    except Exception:
        await db.rollback()
        if claim_id is not None:
            await release_claim(db, claim_id)
        raise


@router.get("", response_model=Page[RackOut])
async def list_racks(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("rack:read")),
) -> Page:
    total = (await db.execute(select(func.count()).select_from(Rack))).scalar_one()
    rows = (
        await db.execute(
            select(Rack, ManagedAsset)
            .join(ManagedAsset, ManagedAsset.id == Rack.id)
            .order_by(Rack.name)
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    ).all()
    items = [await _serialize_rack(db, rack, asset) for rack, asset in rows]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{rack_id}", response_model=RackOut)
async def get_rack(
    rack_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("rack:read"))
) -> RackOut:
    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise NotFoundError(f"Rack {rack_id} not found.")
    asset = await db.get(ManagedAsset, rack_id)
    assert asset is not None
    return await _serialize_rack(db, rack, asset)


@router.patch("/{rack_id}", response_model=RackOut)
async def update_rack(
    rack_id: uuid.UUID,
    body: RackUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("rack:manage")),
) -> RackOut:
    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise NotFoundError(f"Rack {rack_id} not found.")
    check_version_match(expected=if_match_version, actual=rack.version)

    before = {"name": rack.name, "owner": rack.owner, "notes": rack.notes, "version": rack.version}
    if body.name is not None:
        rack.name = body.name
    if body.owner is not None:
        rack.owner = body.owner
    if body.notes is not None:
        rack.notes = body.notes
    rack.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="rack.update", entity_type="rack", entity_id=rack.id,
        request_id=request_id, correlation_id=correlation_id, before=before,
        after={"name": rack.name, "owner": rack.owner, "notes": rack.notes, "version": rack.version},
    )
    await write_outbox_event(
        db, event_type="RackUpdated", aggregate_type="rack", aggregate_id=rack.id,
        payload={"version": rack.version}, correlation_id=correlation_id,
    )
    await db.commit()
    asset = await db.get(ManagedAsset, rack_id)
    assert asset is not None
    return await _serialize_rack(db, rack, asset)


@router.post("/{rack_id}/move", response_model=RackOut)
async def move_rack_endpoint(
    rack_id: uuid.UUID,
    body: RackMoveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
    ctx=Depends(require_permission("rack:place")),
) -> RackOut:
    validate_coordinate("x_mm", body.x_mm)
    validate_coordinate("y_mm", body.y_mm)
    validate_rotation_degrees(body.rotation_deg)

    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise NotFoundError(f"Rack {rack_id} not found.")

    if_match_version = int(if_match.strip().strip('"')) if if_match else None
    try:
        await move_rack(
            db, rack_id=rack_id, room_id=body.room_id, x_mm=body.x_mm, y_mm=body.y_mm, rotation_deg=body.rotation_deg,
            if_match_version=if_match_version,
        )
    except PlacementConflict as exc:
        # Read the conflicting row's fields *before* rolling back — Session.rollback()
        # expires every object attached to the session, and an expired attribute access
        # would otherwise attempt an implicit lazy-load reconnect outside of any awaited
        # context (MissingGreenlet), crashing the very handler meant to report the 409.
        current = exc.current
        current_room_id = current.room_id if current is not None else None
        current_version = current.version if current is not None else None
        await db.rollback()
        detail = (
            f"Rack placement was changed by another request; current room_id={current_room_id}, version={current_version}."
            if current is not None
            else "Rack placement was changed by another request and is no longer where expected."
        )
        raise ConflictError(detail=detail) from exc

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="rack.move", entity_type="rack", entity_id=rack_id,
        request_id=request_id, correlation_id=correlation_id, after={"room_id": str(body.room_id)},
    )
    await write_outbox_event(
        db, event_type="RackMoved", aggregate_type="rack", aggregate_id=rack_id,
        payload={"room_id": str(body.room_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    asset = await db.get(ManagedAsset, rack_id)
    assert asset is not None
    return await _serialize_rack(db, rack, asset)


@router.post("/{rack_id}/retire", response_model=RackOut)
async def retire_rack_endpoint(
    rack_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("rack:place")),
) -> RackOut:
    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise NotFoundError(f"Rack {rack_id} not found.")

    retired = await retire_rack_placement(db, rack_id=rack_id)
    if retired is not None:
        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="rack.unplace", entity_type="rack", entity_id=rack_id,
            request_id=request_id, correlation_id=correlation_id, before={"room_id": str(retired.room_id)},
        )
        await write_outbox_event(
            db, event_type="RackUnplaced", aggregate_type="rack", aggregate_id=rack_id, payload={},
            correlation_id=correlation_id,
        )
    await db.commit()
    asset = await db.get(ManagedAsset, rack_id)
    assert asset is not None
    return await _serialize_rack(db, rack, asset)


# --------------------------------------------------------------------- Elevation


class ElevationSlotOut(BaseModel):
    equipment_id: uuid.UUID
    asset_tag: str
    hostname: str | None
    u_start: int
    u_end: int
    side: str
    mounting_method: str | None


class RackElevationOut(BaseModel):
    rack_id: uuid.UUID
    rack_name: str
    height_u: int
    slots: list[ElevationSlotOut]


@router.get("/{rack_id}/elevation", response_model=RackElevationOut)
async def get_rack_elevation(
    rack_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("rack:read"))
) -> RackElevationOut:
    """Computed on every read from EquipmentPlacement — never stored (§12/§7c's closing
    line: "Rack elevation is still computed, never stored")."""
    from app.domain.physical.models import Equipment
    from app.domain.placement.models import EquipmentPlacement

    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise NotFoundError(f"Rack {rack_id} not found.")
    model_revision = await db.get(RackModelRevision, rack.model_revision_id)
    assert model_revision is not None

    stmt = (
        select(EquipmentPlacement, ManagedAsset, Equipment)
        .join(ManagedAsset, ManagedAsset.id == EquipmentPlacement.equipment_id)
        .join(Equipment, Equipment.id == EquipmentPlacement.equipment_id)
        .where(
            EquipmentPlacement.rack_id == rack_id,
            EquipmentPlacement.placement_type == "rack_mounted",
            EquipmentPlacement.effective_to.is_(None),
        )
    )
    rows = (await db.execute(stmt)).all()

    slots = []
    for placement, asset, equipment in rows:
        u_range = placement.u_range
        slots.append(
            ElevationSlotOut(
                equipment_id=equipment.id,
                asset_tag=asset.asset_tag,
                hostname=equipment.hostname,
                u_start=u_range.lower,
                u_end=u_range.upper,
                side=placement.side,
                mounting_method=placement.mounting_method,
            )
        )
    slots.sort(key=lambda s: s.u_start)
    return RackElevationOut(rack_id=rack.id, rack_name=rack.name, height_u=model_revision.height_u, slots=slots)
