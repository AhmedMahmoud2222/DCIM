"""Equipment endpoints (docs/history/ARCHITECTURE_REVIEW.md §4b/§7). Unlike Rack, Equipment's
placement is polymorphic (rack/floor/wall/ceiling/other — §7); `move` accepts whichever
fields the chosen `placement_type` requires and leaves the rest NULL, matching
EquipmentPlacement's own CHECK constraint."""

import uuid
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.api.v1.bulk_import import BulkImportJobOut, dispatch_parse_job_or_fail
from app.application.access_control import (
    AccessScope,
    ensure_equipment_access,
    equipment_in_site_clause,
    equipment_visible_clause,
)
from app.application.audit_service import write_audit_log
from app.application.bulk_import.service import create_job as create_bulk_import_job
from app.application.bulk_import.templates import build_equipment_template
from app.application.bulk_import.upload import validate_mode, validate_upload_bytes
from app.application.concurrency import lock_versioned_row, require_if_match
from app.application.equipment_instantiation_service import (
    InstantiationRejected,
    InvalidPortTarget,
    PortConnectionOwnedByCable,
    PortNotFound,
    connect_port,
    instantiate_equipment,
    list_equipment_ports,
    list_equipment_power_inlets,
    list_port_connections,
)
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
from app.domain.location.models import Building, Floor, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.physical.ports import PORT_CONNECTION_STATUSES, EquipmentPort
from app.domain.placement.models import PLACEMENT_TYPES, SIDES
from app.infrastructure.storage import get_storage_backend

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


class NamedRef(BaseModel):
    id: uuid.UUID
    name: str
    code: str | None = None


class EquipmentLocationOut(BaseModel):
    """Where the equipment physically is: Site > Building > Floor > Room > Rack. Site, building and floor are
    withheld (null) from callers whose scope does not include the site; room and rack are always present for a
    placed item the caller can already read."""

    site: NamedRef | None = None
    building: NamedRef | None = None
    floor: NamedRef | None = None
    room: NamedRef | None = None
    rack: NamedRef | None = None


class EquipmentOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    lifecycle_status: str
    model_revision_id: uuid.UUID
    # Phase 10B: NULL for equipment created via the legacy path below (a direct
    # model_revision_id, never instantiated from a published catalog revision).
    catalog_model_revision_id: uuid.UUID | None
    hostname: str | None
    owner: str | None
    service: str | None
    environment: str | None
    notes: str | None
    version: int
    created_at: datetime
    placement: EquipmentPlacementOut | None = None
    location: EquipmentLocationOut | None = None


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


async def _location_lookup(db: AsyncSession, placements: list) -> tuple[dict, dict]:
    """Room hierarchy rows and rack names for a set of placements, in two queries regardless of how many items."""
    room_ids = {p.room_id for p in placements}
    rack_ids = {p.rack_id for p in placements if p.rack_id is not None}
    rooms: dict = {}
    racks: dict = {}
    if room_ids:
        rows = await db.execute(
            select(
                Room.id.label("room_id"), Room.name.label("room_name"), Room.code.label("room_code"),
                Floor.id.label("floor_id"), Floor.name.label("floor_name"),
                Building.id.label("building_id"), Building.name.label("building_name"), Building.code.label("building_code"),
                Site.id.label("site_id"), Site.name.label("site_name"), Site.code.label("site_code"),
            )
            .join(Floor, Floor.id == Room.floor_id)
            .join(Building, Building.id == Floor.building_id)
            .join(Site, Site.id == Building.site_id)
            .where(Room.id.in_(room_ids))
        )
        rooms = {row.room_id: row for row in rows.all()}
    if rack_ids:
        racks = {rid: name for rid, name in (await db.execute(select(Rack.id, Rack.name).where(Rack.id.in_(rack_ids)))).all()}
    return rooms, racks


def caller_may_see_location(placement, site_id: uuid.UUID, scope: AccessScope | None) -> bool:
    """The same rule `equipment_visible_clause` applies in SQL, for one placed item: an unrestricted caller, a caller with
    the whole site, or a caller granted the item's rack. Anything else sees no location at all, not even the room or rack
    name, because a mutation response can concern equipment the caller was never allowed to read."""
    if scope is None or scope.unrestricted or site_id in scope.full_site_ids:
        return True
    return placement.rack_id is not None and placement.rack_id in scope.rack_ids


def _location_out(placement, rooms: dict, racks: dict, scope: AccessScope | None) -> EquipmentLocationOut | None:
    if placement is None:
        return None
    row = rooms.get(placement.room_id)
    out = EquipmentLocationOut()
    if row is None or not caller_may_see_location(placement, row.site_id, scope):
        return out
    out.room = NamedRef(id=row.room_id, name=row.room_name, code=row.room_code)
    if scope is None or scope.allows_site(row.site_id):
        out.floor = NamedRef(id=row.floor_id, name=row.floor_name)
        out.building = NamedRef(id=row.building_id, name=row.building_name, code=row.building_code)
        out.site = NamedRef(id=row.site_id, name=row.site_name, code=row.site_code)
    if placement.rack_id is not None:
        out.rack = NamedRef(id=placement.rack_id, name=racks.get(placement.rack_id, str(placement.rack_id)))
    return out


async def _serialize_equipment(
    db: AsyncSession, equipment: Equipment, asset: ManagedAsset, *, scope: AccessScope | None = None,
    lookup: tuple[dict, dict] | None = None, placement=None, placement_loaded: bool = False,
) -> EquipmentOut:
    if not placement_loaded:
        placement = await get_current_equipment_placement(db, equipment.id)
    if lookup is None:
        lookup = await _location_lookup(db, [placement] if placement is not None else [])
    return EquipmentOut(
        id=equipment.id,
        asset_tag=asset.asset_tag,
        lifecycle_status=asset.lifecycle_status,
        model_revision_id=equipment.model_revision_id,
        catalog_model_revision_id=equipment.catalog_model_revision_id,
        hostname=equipment.hostname,
        owner=equipment.owner,
        service=equipment.service,
        environment=equipment.environment,
        notes=equipment.notes,
        version=equipment.version,
        created_at=equipment.created_at,
        placement=_out_placement(placement),
        location=_location_out(placement, lookup[0], lookup[1], scope),
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
            outcome = await get_or_claim(
                db, principal=user_principal(ctx.user.id), key=idempotency_key,
                endpoint="POST:/equipment", request_hash=request_hash,
            )
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
    claim_ref = ClaimRef.of(claim) if claim is not None else None

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

        out = await _serialize_equipment(db, equipment, asset, scope=ctx.scope)
        if claim is not None:
            await complete_claim(db, claim, response_status=201, response_body=out.model_dump(mode="json"))
        await db.commit()
        return out
    except Exception:
        await db.rollback()
        if claim_ref is not None:
            await release_claim(db, claim_ref)
        raise


@router.get("", response_model=Page[EquipmentOut])
async def list_equipment(
    rack_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("equipment:read")),
) -> Page:
    from app.domain.placement.models import EquipmentPlacement

    visible = equipment_visible_clause(ctx.scope, Equipment.id)
    if site_id is not None:
        visible = visible & equipment_in_site_clause(site_id, Equipment.id)
    stmt = select(Equipment, ManagedAsset).join(ManagedAsset, ManagedAsset.id == Equipment.id).where(visible)
    count_stmt = select(func.count()).select_from(Equipment).where(visible)
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
    placements = [await get_current_equipment_placement(db, equipment.id) for equipment, _ in rows]
    lookup = await _location_lookup(db, [p for p in placements if p is not None])
    items = [
        await _serialize_equipment(
            db, equipment, asset, scope=ctx.scope, lookup=lookup, placement=placement, placement_loaded=True,
        )
        for (equipment, asset), placement in zip(rows, placements, strict=True)
    ]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/import-template")
async def download_equipment_import_template(ctx=Depends(require_permission("equipment:read"))) -> StreamingResponse:
    content = build_equipment_template()
    return StreamingResponse(
        BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="equipment-import-template.xlsx"'},
    )


@router.post("/import-jobs", response_model=BulkImportJobOut, status_code=202)
async def upload_equipment_import_job(
    request: Request,
    mode: str = Query(...),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    ctx=Depends(require_permission("equipment:import")),
) -> BulkImportJobOut:
    validate_mode(mode)
    content = await file.read()
    file_hash = validate_upload_bytes(content)

    storage = get_storage_backend()
    storage_key = f"{file_hash}.xlsx"
    storage.save(storage_key, content)

    job = await create_bulk_import_job(
        db, import_type="equipment", mode=mode, uploaded_by_user_id=ctx.user.id,
        original_filename=(file.filename or "upload.xlsx")[:255], file_hash=file_hash, file_size_bytes=len(content),
        storage_key=storage_key,
    )

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="equipment.bulk_import.upload", entity_type="bulk_import_job",
        entity_id=job.id, request_id=request_id, correlation_id=correlation_id,
        after={"filename": job.original_filename, "file_hash": file_hash, "size_bytes": len(content), "mode": mode},
    )
    await db.commit()
    await db.refresh(job)

    await dispatch_parse_job_or_fail(db, job)

    return BulkImportJobOut.from_job(job)


@router.get("/{equipment_id}", response_model=EquipmentOut)
async def get_equipment(
    equipment_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("equipment:read"))
) -> EquipmentOut:
    await ensure_equipment_access(db, ctx.scope, equipment_id)
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")
    asset = await db.get(ManagedAsset, equipment_id)
    assert asset is not None
    return await _serialize_equipment(db, equipment, asset, scope=ctx.scope)


@router.patch("/{equipment_id}", response_model=EquipmentOut)
async def update_equipment(
    equipment_id: uuid.UUID,
    body: EquipmentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("equipment:manage")),
) -> EquipmentOut:
    equipment = await lock_versioned_row(db, Equipment, equipment_id, expected_version=if_match_version)

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
    return await _serialize_equipment(db, equipment, asset, scope=ctx.scope)


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
    return await _serialize_equipment(db, equipment, asset, scope=ctx.scope)


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
    return await _serialize_equipment(db, equipment, asset, scope=ctx.scope)


# ------------------------------------------------------------- Phase 10B: instantiation


class EquipmentInstantiateIn(BaseModel):
    asset_tag: str = Field(max_length=64)
    catalog_model_revision_id: uuid.UUID
    hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    service: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)

    # Optional inline placement — identical shape/semantics to EquipmentMoveIn, so a
    # caller can instantiate directly into a rack slot in one request instead of a
    # separate POST /equipment/instantiate then POST /equipment/{id}/move round trip.
    placement_type: str | None = None
    room_id: uuid.UUID | None = None
    rack_id: uuid.UUID | None = None
    u_start: int | None = None
    u_end: int | None = None
    side: str | None = None
    rotation_deg: int | None = None
    mounting_method: str | None = Field(default=None, max_length=64)
    orientation: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _check_placement_shape(self) -> "EquipmentInstantiateIn":
        if self.placement_type is None:
            return self
        if self.placement_type not in PLACEMENT_TYPES:
            raise ValueError(f"placement_type must be one of {PLACEMENT_TYPES}")
        if self.room_id is None:
            raise ValueError("room_id is required when placement_type is given.")
        if self.side is not None and self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}")
        if self.placement_type == "rack_mounted":
            if self.rack_id is None or self.u_start is None or self.u_end is None or self.side is None:
                raise ValueError("rack_mounted placement requires rack_id, u_start, u_end, and side.")
        return self


class EquipmentPortOut(BaseModel):
    id: uuid.UUID
    equipment_id: uuid.UUID
    network_port_template_id: uuid.UUID | None
    stable_key: str
    display_name: str
    media_type: str
    supported_speeds_mbps: list
    connector_type: str
    role: str
    side: str
    module_group: str | None
    sort_order: int
    connection: "PortConnectionOut | None" = None

    model_config = {"from_attributes": True}


class EquipmentPowerInletOut(BaseModel):
    id: uuid.UUID
    equipment_id: uuid.UUID
    power_supply_template_id: uuid.UUID | None
    power_node_id: uuid.UUID
    stable_key: str
    label: str
    connector_type: str
    sort_order: int

    model_config = {"from_attributes": True}


class PortConnectionOut(BaseModel):
    id: uuid.UUID
    source_port_id: uuid.UUID
    target_port_id: uuid.UUID | None
    target_power_node_id: uuid.UUID | None
    cable_id: str | None
    status: str

    model_config = {"from_attributes": True}


class EquipmentInstantiateOut(EquipmentOut):
    ports: list[EquipmentPortOut]
    power_inlets: list[EquipmentPowerInletOut]


async def _serialize_ports(db: AsyncSession, equipment_id: uuid.UUID) -> list[EquipmentPortOut]:
    ports = await list_equipment_ports(db, equipment_id=equipment_id)
    connections = await list_port_connections(db, port_ids=[p.id for p in ports])
    out = []
    for port in ports:
        connection = connections.get(port.id)
        out.append(
            EquipmentPortOut(
                id=port.id, equipment_id=port.equipment_id, network_port_template_id=port.network_port_template_id,
                stable_key=port.stable_key, display_name=port.display_name, media_type=port.media_type,
                supported_speeds_mbps=port.supported_speeds_mbps, connector_type=port.connector_type, role=port.role,
                side=port.side, module_group=port.module_group, sort_order=port.sort_order,
                connection=PortConnectionOut.model_validate(connection) if connection else None,
            )
        )
    return out


@router.post("/instantiate", response_model=EquipmentInstantiateOut, status_code=201)
async def instantiate_equipment_endpoint(
    body: EquipmentInstantiateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx=Depends(require_permission("equipment:manage")),
) -> EquipmentInstantiateOut:
    """Phase 10B: instantiate a physical Equipment (plus its EquipmentPort/
    EquipmentPowerInlet children) from a published, category='equipment'
    CatalogModelRevision — see app/application/equipment_instantiation_service.py for the
    full contract. Draft/retired/non-equipment revisions are rejected with 409, matching
    catalog_designer_service.py's own ConflictError convention for lifecycle violations."""
    request_hash = hash_request_body(body.model_dump(mode="json"))
    claim = None
    if idempotency_key is not None:
        try:
            outcome = await get_or_claim(
                db, principal=user_principal(ctx.user.id), key=idempotency_key,
                endpoint="POST:/equipment/instantiate", request_hash=request_hash
            )
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
            return EquipmentInstantiateOut(**outcome.cached.response_body)
        claim = outcome.claim
    claim_ref = ClaimRef.of(claim) if claim is not None else None

    try:
        if body.placement_type == "rack_mounted":
            assert body.rack_id is not None and body.u_start is not None and body.u_end is not None
            validate_rotation_degrees(body.rotation_deg)
            rack = await db.get(Rack, body.rack_id)
            if rack is None:
                raise NotFoundError(f"Rack {body.rack_id} not found.")
            rack_model_revision = await db.get(RackModelRevision, rack.model_revision_id)
            assert rack_model_revision is not None
            validate_u_range_against_rack_capacity(
                u_start=body.u_start, u_end=body.u_end, rack_height_u=rack_model_revision.height_u
            )

        try:
            equipment = await instantiate_equipment(
                db, asset_tag=body.asset_tag, catalog_model_revision_id=body.catalog_model_revision_id,
                hostname=body.hostname, ip_address=body.ip_address, owner=body.owner, service=body.service,
                environment=body.environment, notes=body.notes,
            )
        except InstantiationRejected as exc:
            raise ApiError(
                status_code=409, title="Instantiation Rejected", detail=exc.detail,
                type_="https://dcim.internal/errors/conflict",
            ) from exc

        if body.placement_type is not None:
            assert body.room_id is not None
            await move_equipment(
                db, equipment_id=equipment.id, placement_type=body.placement_type, room_id=body.room_id,
                rack_id=body.rack_id, u_start=body.u_start, u_end=body.u_end, side=body.side,
                rotation_deg=body.rotation_deg, mounting_method=body.mounting_method, orientation=body.orientation,
                if_match_version=None,
            )

        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="equipment.instantiate", entity_type="equipment", entity_id=equipment.id,
            request_id=request_id, correlation_id=correlation_id,
            after={"asset_tag": equipment.hostname, "catalog_model_revision_id": str(body.catalog_model_revision_id)},
        )
        await write_outbox_event(
            db, event_type="EquipmentInstantiated", aggregate_type="equipment", aggregate_id=equipment.id,
            payload={"catalog_model_revision_id": str(body.catalog_model_revision_id)}, correlation_id=correlation_id,
        )

        asset = await db.get(ManagedAsset, equipment.id)
        assert asset is not None
        base = await _serialize_equipment(db, equipment, asset, scope=ctx.scope)
        ports = await _serialize_ports(db, equipment.id)
        power_inlets = await list_equipment_power_inlets(db, equipment_id=equipment.id)
        inlets = [EquipmentPowerInletOut.model_validate(inlet) for inlet in power_inlets]
        out = EquipmentInstantiateOut(**base.model_dump(), ports=ports, power_inlets=inlets)

        if claim is not None:
            await complete_claim(db, claim, response_status=201, response_body=out.model_dump(mode="json"))
        await db.commit()
        return out
    except PlacementConflict as exc:
        current = exc.current
        current_version = current.version if current is not None else None
        await db.rollback()
        if claim_ref is not None:
            await release_claim(db, claim_ref)
        detail = (
            f"Requested placement conflicts with existing equipment; current version={current_version}."
            if current is not None
            else "Requested placement conflicts with existing equipment."
        )
        raise ConflictError(detail=detail) from exc
    except Exception:
        await db.rollback()
        if claim_ref is not None:
            await release_claim(db, claim_ref)
        raise


class PortsListOut(BaseModel):
    ports: list[EquipmentPortOut]
    power_inlets: list[EquipmentPowerInletOut]


@router.get("/{equipment_id}/ports", response_model=PortsListOut)
async def list_equipment_ports_endpoint(
    equipment_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("equipment:read"))
) -> PortsListOut:
    await ensure_equipment_access(db, ctx.scope, equipment_id)
    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")
    ports = await _serialize_ports(db, equipment_id)
    power_inlets = await list_equipment_power_inlets(db, equipment_id=equipment_id)
    inlets = [EquipmentPowerInletOut.model_validate(inlet) for inlet in power_inlets]
    return PortsListOut(ports=ports, power_inlets=inlets)


class PortConnectIn(BaseModel):
    port_id: uuid.UUID
    target_port_id: uuid.UUID | None = None
    target_power_node_id: uuid.UUID | None = None
    cable_id: str | None = Field(default=None, max_length=64)
    status: str = "active"

    @model_validator(mode="after")
    def _check_target_shape(self) -> "PortConnectIn":
        if self.status not in PORT_CONNECTION_STATUSES:
            raise ValueError(f"status must be one of {PORT_CONNECTION_STATUSES}")
        if (self.target_port_id is None) == (self.target_power_node_id is None):
            raise ValueError("Exactly one of target_port_id or target_power_node_id must be set.")
        return self


@router.post("/{equipment_id}/ports/connect", response_model=PortConnectionOut, status_code=201)
async def connect_port_endpoint(
    equipment_id: uuid.UUID,
    body: PortConnectIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("equipment:manage")),
) -> PortConnectionOut:
    """Cabling mutation — gated by `equipment:manage`, the same permission
    `POST /equipment` and `PATCH /equipment/{id}` already require, so the Operator role
    (which holds `equipment:place`/`equipment:read` but not `equipment:manage` —
    app/application/rbac.py's DEFAULT_ROLE_PERMISSIONS) cannot cable equipment, only view
    it — the "operator permission gate for cabling mutations" this phase asks for,
    without introducing a new permission code."""
    port = await db.get(EquipmentPort, body.port_id)
    if port is None or port.equipment_id != equipment_id:
        raise NotFoundError(f"EquipmentPort {body.port_id} not found on equipment {equipment_id}.")

    try:
        connection = await connect_port(
            db, source_port_id=body.port_id, target_port_id=body.target_port_id,
            target_power_node_id=body.target_power_node_id, cable_id=body.cable_id, status=body.status,
        )
    except PortNotFound as exc:
        await db.rollback()
        raise NotFoundError(str(exc)) from exc
    except InvalidPortTarget as exc:
        await db.rollback()
        raise ApiError(status_code=422, title="Invalid Port Target", detail=exc.detail) from exc
    except PortConnectionOwnedByCable as exc:
        await db.rollback()
        raise ConflictError(exc.detail) from exc

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="equipment.port.connect", entity_type="equipment_port", entity_id=port.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"target_port_id": str(body.target_port_id) if body.target_port_id else None,
               "target_power_node_id": str(body.target_power_node_id) if body.target_power_node_id else None,
               "status": body.status},
    )
    await write_outbox_event(
        db, event_type="PortConnected", aggregate_type="equipment_port", aggregate_id=port.id,
        payload={"connection_id": str(connection.id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return PortConnectionOut.model_validate(connection)
