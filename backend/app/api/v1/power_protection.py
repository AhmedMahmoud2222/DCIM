"""Protection device endpoints (Issue #102, area A). Devices are `PowerNode`s of type
`protection_device`; links use the existing `/power/connections` endpoints. Every mutation is audited,
outboxed and versioned (`If-Match`) like the rest of the power API, and requires `power:manage`; reads
require `power:read`."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import lock_versioned_row, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.power_protection import (
    ProtectionScopeError,
    device_rated_kw,
    housing_site,
    resolve_node_site,
)
from app.application.rbac import require_permission
from app.core.errors import ApiError, NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Site
from app.domain.power.models import (
    PROTECTION_DEVICE_TYPES,
    PROTECTION_PHASE_CONFIGS,
    PROTECTION_STATES,
    PROTECTION_STATUSES,
    PowerConnection,
    PowerNode,
    ProtectionDevice,
)

router = APIRouter(prefix="/power/protection-devices", tags=["power-protection"])


def _ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class ProtectionDeviceIn(BaseModel):
    housing_asset_id: uuid.UUID
    site_id: uuid.UUID
    label: str = Field(min_length=1, max_length=128)
    device_type: str = "breaker"
    rating_a: float = Field(gt=0)
    voltage_v: float = Field(gt=0)
    poles: int = Field(ge=1, le=3)
    phase_config: str
    status: str = "in_service"


class ProtectionDeviceUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    device_type: str | None = None
    rating_a: float | None = Field(default=None, gt=0)
    voltage_v: float | None = Field(default=None, gt=0)
    poles: int | None = Field(default=None, ge=1, le=3)
    phase_config: str | None = None
    status: str | None = None


class ProtectionStateIn(BaseModel):
    state: str


class ProtectionDeviceOut(BaseModel):
    id: uuid.UUID
    housing_asset_id: uuid.UUID | None
    site_id: uuid.UUID
    label: str
    device_type: str
    rating_a: float
    voltage_v: float
    poles: int
    phase_config: str
    rated_kw: float
    state: str
    status: str
    state_changed_at: datetime | None
    retired_at: datetime | None
    upstream_node_ids: list[uuid.UUID]
    downstream_node_ids: list[uuid.UUID]
    version: int


def _validate_shape(device_type: str, phase_config: str, poles: int, status: str, rating_a: float, voltage_v: float) -> None:
    if device_type not in PROTECTION_DEVICE_TYPES:
        raise ProtectionScopeError(f"device_type must be one of {PROTECTION_DEVICE_TYPES}.")
    if phase_config not in PROTECTION_PHASE_CONFIGS:
        raise ProtectionScopeError(f"phase_config must be one of {PROTECTION_PHASE_CONFIGS}.")
    if status not in PROTECTION_STATUSES:
        raise ProtectionScopeError(f"status must be one of {PROTECTION_STATUSES}.")
    if (phase_config == "single" and poles not in (1, 2)) or (phase_config == "three" and poles != 3):
        raise ProtectionScopeError("Pole count does not match the phase configuration.")
    if not (0 < rating_a <= 6300):
        raise ProtectionScopeError("rating_a must be between 0 and 6300 A.")
    if not (24 <= voltage_v <= 1000):
        raise ProtectionScopeError("voltage_v must be between 24 and 1000 V.")


async def _out(db: AsyncSession, node: PowerNode, dev: ProtectionDevice) -> ProtectionDeviceOut:
    rows = (
        await db.execute(
            select(PowerConnection.source_node_id, PowerConnection.target_node_id).where(
                PowerConnection.effective_to.is_(None),
                (PowerConnection.source_node_id == node.id) | (PowerConnection.target_node_id == node.id),
            )
        )
    ).all()
    return ProtectionDeviceOut(
        id=node.id, housing_asset_id=node.owning_asset_id, site_id=dev.site_id, label=node.label,
        device_type=dev.device_type, rating_a=float(dev.rating_a), voltage_v=float(dev.voltage_v), poles=dev.poles,
        phase_config=dev.phase_config, rated_kw=device_rated_kw(dev.rating_a, dev.voltage_v, dev.phase_config),
        state=dev.state, status=dev.status, state_changed_at=dev.state_changed_at, retired_at=node.retired_at,
        upstream_node_ids=sorted(r.source_node_id for r in rows if r.target_node_id == node.id),
        downstream_node_ids=sorted(r.target_node_id for r in rows if r.source_node_id == node.id),
        version=dev.version,
    )


async def _load(db: AsyncSession, device_id: uuid.UUID) -> tuple[PowerNode, ProtectionDevice]:
    node = await db.get(PowerNode, device_id)
    dev = await db.get(ProtectionDevice, device_id)
    if node is None or dev is None:
        raise NotFoundError(f"Protection device {device_id} not found.")
    return node, dev


@router.post("", response_model=ProtectionDeviceOut, status_code=201)
async def create_protection_device(
    body: ProtectionDeviceIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> ProtectionDeviceOut:
    _validate_shape(body.device_type, body.phase_config, body.poles, body.status, body.rating_a, body.voltage_v)
    if await db.get(Site, body.site_id) is None:
        raise NotFoundError(f"Site {body.site_id} not found.")
    housing = await db.get(ManagedAsset, body.housing_asset_id)
    exists, house_site = await housing_site(db, body.housing_asset_id)
    if housing is None or not exists:
        raise ProtectionScopeError("housing_asset_id must reference a generator, UPS, power panel or PDU.")
    if house_site is not None and house_site != body.site_id:
        raise ProtectionScopeError("The housing asset belongs to a different site than site_id.")
    node = PowerNode(node_type="protection_device", owning_asset_id=body.housing_asset_id, label=body.label)
    db.add(node)
    await db.flush()
    dev = ProtectionDevice(
        power_node_id=node.id, site_id=body.site_id, device_type=body.device_type, rating_a=body.rating_a,
        voltage_v=body.voltage_v, poles=body.poles, phase_config=body.phase_config, status=body.status,
        state="closed", state_changed_at=datetime.now(UTC), version=1,
    )
    db.add(dev)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.protection_device.create", entity_type="protection_device",
        entity_id=node.id, request_id=request_id, correlation_id=correlation_id,
        after={"label": body.label, "site_id": str(body.site_id), "rating_a": body.rating_a, "state": "closed"},
    )
    await write_outbox_event(
        db, event_type="ProtectionDeviceCreated", aggregate_type="protection_device", aggregate_id=node.id,
        payload={"site_id": str(body.site_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return await _out(db, node, dev)


@router.get("", response_model=Page[ProtectionDeviceOut])
async def list_protection_devices(
    db: AsyncSession = Depends(get_db), pagination: Pagination = Depends(pagination_params),
    site_id: uuid.UUID | None = None, state: str | None = None, include_retired: bool = False,
    ctx=Depends(require_permission("power:read")),
) -> Page:
    stmt = select(PowerNode, ProtectionDevice).join(ProtectionDevice, ProtectionDevice.power_node_id == PowerNode.id)
    if site_id is not None:
        stmt = stmt.where(ProtectionDevice.site_id == site_id)
    if state is not None:
        if state not in PROTECTION_STATES:
            raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"state must be one of {PROTECTION_STATES}.")
        stmt = stmt.where(ProtectionDevice.state == state)
    if not include_retired:
        stmt = stmt.where(PowerNode.retired_at.is_(None))
    all_rows = (await db.execute(stmt.order_by(PowerNode.label, PowerNode.id))).all()
    page_rows = all_rows[pagination.offset : pagination.offset + pagination.limit]
    items = [await _out(db, n, d) for n, d in page_rows]
    return Page(items=items, total=len(all_rows), limit=pagination.limit, offset=pagination.offset)


@router.get("/{device_id}", response_model=ProtectionDeviceOut)
async def get_protection_device(
    device_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:read"))
) -> ProtectionDeviceOut:
    node, dev = await _load(db, device_id)
    return await _out(db, node, dev)


async def _assert_links_still_valid(db: AsyncSession, node: PowerNode, dev: ProtectionDevice) -> None:
    rows = (
        await db.execute(
            select(PowerConnection).where(
                PowerConnection.effective_to.is_(None),
                (PowerConnection.source_node_id == node.id) | (PowerConnection.target_node_id == node.id),
            )
        )
    ).scalars().all()
    for c in rows:
        if c.rated_current_a is not None and float(c.rated_current_a) > float(dev.rating_a):
            raise ProtectionScopeError(
                f"Lowering the rating below connection {c.id}'s rated current ({float(c.rated_current_a)} A) is unsafe."
            )
        if c.phase is not None and c.phase != dev.phase_config:
            raise ProtectionScopeError(f"Connection {c.id} phase {c.phase!r} conflicts with the new phase configuration.")


@router.patch("/{device_id}", response_model=ProtectionDeviceOut)
async def update_protection_device(
    device_id: uuid.UUID, body: ProtectionDeviceUpdate, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("power:manage")),
) -> ProtectionDeviceOut:
    await _load(db, device_id)
    dev = await lock_versioned_row(
        db, ProtectionDevice, device_id, expected_version=if_match_version, label="Protection device"
    )
    node = await db.get(PowerNode, device_id)
    assert node is not None
    before = {"rating_a": float(dev.rating_a), "voltage_v": float(dev.voltage_v), "status": dev.status, "version": dev.version}
    data = body.model_dump(exclude_unset=True)
    _validate_shape(
        data.get("device_type", dev.device_type), data.get("phase_config", dev.phase_config),
        data.get("poles", dev.poles), data.get("status", dev.status),
        data.get("rating_a", float(dev.rating_a)), data.get("voltage_v", float(dev.voltage_v)),
    )
    for field in ("device_type", "rating_a", "voltage_v", "poles", "phase_config", "status"):
        if field in data and data[field] is not None:
            setattr(dev, field, data[field])
    if data.get("label") is not None:
        node.label = data["label"]
    await _assert_links_still_valid(db, node, dev)
    dev.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.protection_device.update", entity_type="protection_device",
        entity_id=device_id, request_id=request_id, correlation_id=correlation_id, before=before,
        after={"rating_a": float(dev.rating_a), "voltage_v": float(dev.voltage_v), "status": dev.status, "version": dev.version},
    )
    await write_outbox_event(
        db, event_type="ProtectionDeviceChanged", aggregate_type="protection_device", aggregate_id=device_id,
        payload={"version": dev.version}, correlation_id=correlation_id,
    )
    await db.commit()
    return await _out(db, node, dev)


@router.post("/{device_id}/state", response_model=ProtectionDeviceOut)
async def set_protection_state(
    device_id: uuid.UUID, body: ProtectionStateIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("power:manage")),
) -> ProtectionDeviceOut:
    """Record the device's reported state. Only the record changes: nothing is switched in the field,
    and `unknown` is stored as unknown rather than guessed."""
    if body.state not in PROTECTION_STATES:
        raise ProtectionScopeError(f"state must be one of {PROTECTION_STATES}.")
    await _load(db, device_id)
    dev = await lock_versioned_row(
        db, ProtectionDevice, device_id, expected_version=if_match_version, label="Protection device"
    )
    node = await db.get(PowerNode, device_id)
    assert node is not None
    if node.retired_at is not None:
        raise ApiError(status_code=422, title="Node Retired", detail="A retired protection device cannot change state.")
    before = {"state": dev.state, "version": dev.version}
    if dev.state != body.state:
        dev.state = body.state
        dev.state_changed_at = datetime.now(UTC)
    dev.version += 1
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.protection_device.state", entity_type="protection_device",
        entity_id=device_id, request_id=request_id, correlation_id=correlation_id, before=before,
        after={"state": dev.state, "version": dev.version},
    )
    await write_outbox_event(
        db, event_type="ProtectionDeviceStateChanged", aggregate_type="protection_device", aggregate_id=device_id,
        payload={"state": dev.state}, correlation_id=correlation_id,
    )
    await db.commit()
    return await _out(db, node, dev)


async def device_site(db: AsyncSession, node: PowerNode) -> uuid.UUID | None:  # re-exported for services
    return await resolve_node_site(db, node)
