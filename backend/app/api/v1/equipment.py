"""Equipment endpoints (ARCHITECTURE_REVIEW.md §4b/§7). Unlike Rack, Equipment's
placement is polymorphic (rack/floor/wall/ceiling/other — §7); `move` accepts whichever
fields the chosen `placement_type` requires and leaves the rest NULL, matching
EquipmentPlacement's own CHECK constraint."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, model_validator
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
from app.application.placement_service import (
    PlacementConflict,
    get_current_equipment_placement,
    move_equipment,
    retire_equipment_placement,
)
from app.application.rbac import require_permission
from app.application.spatial_validation import validate_rotation_degrees, validate_u_range_against_rack_capacity
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.catalog.models import EquipmentModelRevision, RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import PLACEMENT_TYPES, SIDES

router = APIRouter(prefix="/equipment", tags=["equipment"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class EquipmentIn(BaseModel):
    asset_tag: str = Field(max_length=64)
    model_revision_id: uuid.UUID
    hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    service: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class EquipmentUpdate(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    owner: str | None = Field(default=None, max_length=128)
    service: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class EquipmentPlacementOut(BaseModel):
    placement_type: str
    room_id: uuid.UUID
    rack_id: uuid.UUID | None
    u_start: int | None
    u_end: int | None
    side: str | None
    effective_from: datetime

    model_config = {"from_attributes": True}


class EquipmentOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    lifecycle_status: str
    model_revision_id: uuid.UUID
    hostname: str | None
    owner: str | None
    service: str | None
    environment: str | None
    notes: str | None
    version: int
    created_at: datetime
    placement: EquipmentPlacementOut | None = None


class EquipmentMoveIn(BaseModel):
    placement_type: str
    room_id: uuid.UUID
    rack_id: uuid.UUID | None = None
    u_start: int | None = None
    u_end: int | None = None
    side: str | None = None
    rotation_deg: int | None = None
    mounting_method: str | None = Field(default=None, max_length=64)
    orientation: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _check_shape(self) -> "EquipmentMoveIn":
        if self.placement_type not in PLACEMENT_TYPES:
            raise ValueError(f"placement_type must be one of {PLACEMENT_TYPES}")
        if self.side is not None and self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}")
        if self.placement_type == "rack_mounted":
            if self.rack_id is None or self.u_start is None or self.u_end is None or self.side is None:
                raise ValueError("rack_mounted placement requires rack_id, u_start, u_end, and side.")
        return self


def _out_placement(placement) -> EquipmentPlacementOut | None:
    if placement is None:
        return None
    u_range = placement.u_range
    return EquipmentPlacementOut(
        placement_type=placement.placement_type,
        room_id=placement.room_id,
        rack_id=placement.rack_id,
        u_start=u_range.lower if u_range is not None else None,
        u_end=u_range.upper if u_range is not None else None,
        side=placement.side,
        effective_from=placement.effective_from,
    )


async def _serialize_equipment(db: AsyncSession, equipment: Equipment, asset: ManagedAsset) -> EquipmentOut:
    placement = await get_current_equipment_placement(db, equipment.id)
    return EquipmentOut(
        id=equipment.id,
        asset_tag=asset.asset_tag,
        lifecycle_status=asset.lifecycle_status,
        model_revision_id=equipment.model_revision_id,
        hostname=equipment.hostname,
        owner=equipment.owner,
        service=equipment.service,
        environment=equipment.environment,
        notes=equipment.notes,
        version=equipment.version,
        created_at=equipment.created_at,
        placement=_out_placement(placement),
    )


@router.post("", response_model=EquipmentOut, status_code=201)
async def create_equipment(
    body: EquipmentIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx=Depends(require_permission("equipment:manage")),
) -> EquipmentOut:
    request_hash = hash_request_body(body.model_dump(mode="json"))
    claim = None
    if idempotency_key is not None:
        try:
            outcome = await get_or_claim(db, key=idempotency_key, endpoint="POST:/equipment", request_hash=request_hash)
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
            return EquipmentOut(**outcome.cached.response_body)
        claim = outcome.claim
    claim_id = claim.id if claim is not None else None

    try:
        if await db.get(EquipmentModelRevision, body.model_revision_id) is None:
            raise NotFoundError(f"EquipmentModelRevision {body.model_revision_id} not found.")

        asset = ManagedAsset(asset_type="equipment", asset_tag=body.asset_tag, lifecycle_status="planned")
        db.add(asset)
        await db.flush()
        equipment = Equipment(
            id=asset.id, model_revision_id=body.model_revision_id, hostname=body.hostname, ip_address=body.ip_address,
            owner=body.owner, service=body.service, environment=body.environment, notes=body.notes,
        )
        db.add(equipment)
        await db.flush()

        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="equipment.create", entity_type="equipment", entity_id=equipment.id,
            request_id=request_id, correlation_id=correlation_id,
            after={"asset_tag": asset.asset_tag, "hostname": equipment.hostname},
        )
        await write_outbox_event(
            db, event_type="EquipmentCreated", aggregate_type="equipment", aggregate_id=equipment.id,
            payload={"asset_tag": asset.asset_tag}, correlation_id=correlation_id,
        )

        out = await _serialize_equipment(db, equipment, asset)
        if claim is not None:
            await complete_claim(db, claim, response_status=201, response_body=out.model_dump(mode="json"))
        await db.commit()
        return out
    except Exception:
        await db.rollback()
        if claim_id is not None:
            await release_claim(db, claim_id)
        raise


@router.get("", response_model=Page[EquipmentOut])
async def list_equipment(
    rack_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("equipment:read")),
) -> Page:
    from app.domain.placement.models import EquipmentPlacement

    stmt = select(Equipment, ManagedAsset).join(ManagedAsset, ManagedAsset.id == Equipment.id)
    count_stmt = select(func.count()).select_from(Equipment)
    if rack_id is not None:
        stmt = stmt.join(
            EquipmentPlacement,
            (EquipmentPlacement.equipment_id == Equipment.id) & (EquipmentPlacement.effective_to.is_(None)),
        ).where(EquipmentPlacement.rack_id == rack_id)
        count_stmt = count_stmt.select_from(Equipment).join(
            EquipmentPlacement,
            (EquipmentPlacement.equipment_id == Equipment.id) & (EquipmentPlacement.effective_to.is_(None)),
        ).where(EquipmentPlacement.rack_id == rack_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(Equipment.created_at.desc()).offset(pagination.offset).limit(pagination.limit))).all()
    items = [await _serialize_equipment(db, equipment, asset) for equipment, asset in rows]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{equipment_id}", response_model=EquipmentOut)
async def get_equipment(
    equipment_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("equipment:read"))
) -> EquipmentOut:
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")
    asset = await db.get(ManagedAsset, equipment_id)
    assert asset is not None
    return await _serialize_equipment(db, equipment, asset)


@router.patch("/{equipment_id}", response_model=EquipmentOut)
async def update_equipment(
    equipment_id: uuid.UUID,
    body: EquipmentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("equipment:manage")),
) -> EquipmentOut:
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")
    check_version_match(expected=if_match_version, actual=equipment.version)

    before = {"hostname": equipment.hostname, "owner": equipment.owner, "version": equipment.version}
    for field in ("hostname", "owner", "service", "environment", "notes"):
        value = getattr(body, field)
        if value is not None:
            setattr(equipment, field, value)
    equipment.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="equipment.update", entity_type="equipment", entity_id=equipment.id,
        request_id=request_id, correlation_id=correlation_id, before=before,
        after={"hostname": equipment.hostname, "owner": equipment.owner, "version": equipment.version},
    )
    await write_outbox_event(
        db, event_type="EquipmentUpdated", aggregate_type="equipment", aggregate_id=equipment.id,
        payload={"version": equipment.version}, correlation_id=correlation_id,
    )
    await db.commit()
    asset = await db.get(ManagedAsset, equipment_id)
    assert asset is not None
    return await _serialize_equipment(db, equipment, asset)


@router.post("/{equipment_id}/move", response_model=EquipmentOut)
async def move_equipment_endpoint(
    equipment_id: uuid.UUID,
    body: EquipmentMoveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
    ctx=Depends(require_permission("equipment:place")),
) -> EquipmentOut:
    validate_rotation_degrees(body.rotation_deg)
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")

    if body.placement_type == "rack_mounted":
        rack = await db.get(Rack, body.rack_id)
        if rack is None:
            raise NotFoundError(f"Rack {body.rack_id} not found.")
        model_revision = await db.get(RackModelRevision, rack.model_revision_id)
        assert model_revision is not None
        # EquipmentMoveIn._check_shape already guarantees u_start/u_end are set when
        # placement_type == "rack_mounted".
        assert body.u_start is not None
        assert body.u_end is not None
        validate_u_range_against_rack_capacity(u_start=body.u_start, u_end=body.u_end, rack_height_u=model_revision.height_u)

    if_match_version = int(if_match.strip().strip('"')) if if_match else None
    try:
        await move_equipment(
            db, equipment_id=equipment_id, placement_type=body.placement_type, room_id=body.room_id, rack_id=body.rack_id,
            u_start=body.u_start, u_end=body.u_end, side=body.side, rotation_deg=body.rotation_deg,
            mounting_method=body.mounting_method, orientation=body.orientation, if_match_version=if_match_version,
        )
    except PlacementConflict as exc:
        # Read the conflicting row's fields *before* rolling back — Session.rollback()
        # expires every object attached to the session, and an expired attribute access
        # would otherwise attempt an implicit lazy-load reconnect outside of any awaited
        # context (MissingGreenlet), crashing the very handler meant to report the 409.
        current = exc.current
        current_version = current.version if current is not None else None
        await db.rollback()
        detail = (
            f"Equipment placement was changed by another request; current version={current_version}."
            if current is not None
            else "Equipment placement was changed by another request and is no longer where expected."
        )
        raise ConflictError(detail=detail) from exc

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="equipment.move", entity_type="equipment", entity_id=equipment_id,
        request_id=request_id, correlation_id=correlation_id,
        after={"placement_type": body.placement_type, "room_id": str(body.room_id)},
    )
    await write_outbox_event(
        db, event_type="EquipmentPlaced", aggregate_type="equipment", aggregate_id=equipment_id,
        payload={"placement_type": body.placement_type, "room_id": str(body.room_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    asset = await db.get(ManagedAsset, equipment_id)
    assert asset is not None
    return await _serialize_equipment(db, equipment, asset)


@router.post("/{equipment_id}/retire", response_model=EquipmentOut)
async def retire_equipment_endpoint(
    equipment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("equipment:place")),
) -> EquipmentOut:
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")

    retired = await retire_equipment_placement(db, equipment_id=equipment_id)
    if retired is not None:
        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="equipment.unplace", entity_type="equipment", entity_id=equipment_id,
            request_id=request_id, correlation_id=correlation_id, before={"room_id": str(retired.room_id)},
        )
        await write_outbox_event(
            db, event_type="EquipmentUnplaced", aggregate_type="equipment", aggregate_id=equipment_id, payload={},
            correlation_id=correlation_id,
        )
    await db.commit()
    asset = await db.get(ManagedAsset, equipment_id)
    assert asset is not None
    return await _serialize_equipment(db, equipment, asset)
