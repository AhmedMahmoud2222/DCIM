"""Physical cables and port-to-port topology trace (Issue #101).

`cable:read` lists and traces; `cable:manage` records, edits, installs, removes and deletes.
Both permissions are scope-aware: a restricted user only sees cables with at least one
visible endpoint (the far end of a partly visible cable is masked), and may mutate only a
cable whose *both* endpoints are visible. Everything else is a 404, never a 403, so ids
cannot be probed. Turning a discovered adjacency into a cable is a separate, explicit
action that additionally requires `discovery:reconcile`.
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.access_control import AccessScope, ensure_equipment_access, equipment_visible_clause
from app.application.concurrency import require_if_match
from app.application.network import cable_service, trace_service
from app.application.rbac import AuthContext, require_permission
from app.core.errors import ForbiddenError, NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.network.cable_models import CABLE_TYPES, Cable, CableEndpoint
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort

router = APIRouter(tags=["cables"])

CableType = Literal["copper_utp", "copper_stp", "coax", "fiber_sm", "fiber_mm", "dac", "aoc", "console", "other"]
MAX_ROUTE_BYTES = 4_096
MAX_ROUTE_DEPTH = 4


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


def _depth(value: object, current: int = 0) -> int:
    if isinstance(value, dict) and value:
        return max(_depth(v, current + 1) for v in value.values())
    if isinstance(value, list) and value:
        return max(_depth(v, current + 1) for v in value)
    return current


def _bound_route(value: dict) -> dict:
    if len(json.dumps(value)) > MAX_ROUTE_BYTES:
        raise ValueError(f"route_metadata must serialize to at most {MAX_ROUTE_BYTES} bytes")
    if _depth(value) > MAX_ROUTE_DEPTH:
        raise ValueError(f"route_metadata must not nest more than {MAX_ROUTE_DEPTH} levels")
    return value


class CableAttributes(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    cable_type: CableType
    connector_a: str | None = Field(default=None, max_length=32)
    connector_b: str | None = Field(default=None, max_length=32)
    length_m: Decimal | None = Field(default=None, gt=0, le=Decimal("99999.99"), decimal_places=2)
    route_metadata: dict = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("route_metadata")
    @classmethod
    def _route(cls, v: dict) -> dict:
        return _bound_route(v)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("label must not be blank")
        return v


class CableIn(CableAttributes):
    endpoint_a_port_id: uuid.UUID
    endpoint_b_port_id: uuid.UUID
    status: Literal["planned", "installed"] = "planned"
    installed_at: datetime | None = None


class CableFromNeighborIn(CableAttributes):
    status: Literal["planned", "installed"] = "installed"
    installed_at: datetime | None = None


class CablePatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    cable_type: CableType | None = None
    connector_a: str | None = Field(default=None, max_length=32)
    connector_b: str | None = Field(default=None, max_length=32)
    length_m: Decimal | None = Field(default=None, gt=0, le=Decimal("99999.99"), decimal_places=2)
    route_metadata: dict | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("route_metadata")
    @classmethod
    def _route(cls, v: dict | None) -> dict | None:
        return v if v is None else _bound_route(v)


class InstallIn(BaseModel):
    installed_at: datetime | None = None


class RemoveIn(BaseModel):
    removed_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=1000)


class PortRef(BaseModel):
    port_id: uuid.UUID
    port_name: str
    media_type: str
    equipment_id: uuid.UUID
    equipment_hostname: str | None
    equipment_asset_tag: str


class EndpointOut(BaseModel):
    end: str
    restricted: bool = False
    port: PortRef | None = None


class CableOut(BaseModel):
    id: uuid.UUID
    label: str
    cable_type: str
    connector_a: str | None
    connector_b: str | None
    length_m: Decimal | None
    route_metadata: dict
    status: str
    installed_at: datetime | None
    removed_at: datetime | None
    source: str
    source_neighbor_id: uuid.UUID | None
    port_connection_id: uuid.UUID | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    endpoints: list[EndpointOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


async def _visible_equipment(db: AsyncSession, scope: AccessScope, equipment_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if scope.unrestricted or not equipment_ids:
        return set(equipment_ids)
    rows = await db.execute(
        select(Equipment.id).where(Equipment.id.in_(equipment_ids), equipment_visible_clause(scope, Equipment.id))
    )
    return set(rows.scalars())


async def serialize(db: AsyncSession, scope: AccessScope, cables: list[Cable]) -> list[CableOut]:
    if not cables:
        return []
    rows = (
        await db.execute(
            select(CableEndpoint, EquipmentPort, Equipment.hostname, ManagedAsset.asset_tag)
            .join(EquipmentPort, EquipmentPort.id == CableEndpoint.equipment_port_id)
            .join(Equipment, Equipment.id == EquipmentPort.equipment_id)
            .join(ManagedAsset, ManagedAsset.id == Equipment.id)
            .where(CableEndpoint.cable_id.in_([c.id for c in cables]))
        )
    ).all()
    visible = await _visible_equipment(db, scope, {port.equipment_id for _e, port, _h, _t in rows})
    by_cable: dict[uuid.UUID, list[EndpointOut]] = {}
    for endpoint, port, hostname, asset_tag in rows:
        if port.equipment_id in visible:
            view = EndpointOut(
                end=endpoint.end_label,
                port=PortRef(
                    port_id=port.id, port_name=port.display_name, media_type=port.media_type, equipment_id=port.equipment_id,
                    equipment_hostname=hostname, equipment_asset_tag=asset_tag,
                ),
            )
        else:
            view = EndpointOut(end=endpoint.end_label, restricted=True)
        by_cable.setdefault(endpoint.cable_id, []).append(view)
    out = []
    for cable in cables:
        item = CableOut.model_validate(cable)
        item.endpoints = sorted(by_cable.get(cable.id, []), key=lambda e: e.end)
        out.append(item)
    return out


async def _one(db: AsyncSession, ctx: AuthContext, cable_id: uuid.UUID) -> CableOut:
    cable = await cable_service.get_cable(db, cable_id)
    out = (await serialize(db, ctx.scope, [cable]))[0]
    if all(e.restricted for e in out.endpoints):
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Cable {cable_id} not found.")
    return out


@router.post("/cables", response_model=CableOut, status_code=201)
async def create_cable(
    body: CableIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> CableOut:
    request_id, correlation_id = _request_ids(request)
    cable = await cable_service.create_cable(
        db, scope=ctx.scope, label=body.label, cable_type=body.cable_type, connector_a=body.connector_a,
        connector_b=body.connector_b, length_m=body.length_m, route_metadata=body.route_metadata, notes=body.notes,
        port_a_id=body.endpoint_a_port_id, port_b_id=body.endpoint_b_port_id, status=body.status,
        installed_at=body.installed_at, source="manual", source_neighbor_id=None, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _one(db, ctx, cable.id)


@router.post("/cables/from-neighbor/{neighbor_id}", response_model=CableOut, status_code=201)
async def create_cable_from_neighbor(
    neighbor_id: uuid.UUID, body: CableFromNeighborIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> CableOut:
    """Explicit operator action: record a physical cable for an adjacency an operator already confirmed."""
    if not ctx.has_permission("discovery:reconcile"):
        raise ForbiddenError("Missing required permission: discovery:reconcile")
    request_id, correlation_id = _request_ids(request)
    cable = await cable_service.create_cable_from_neighbor(
        db, scope=ctx.scope, neighbor_id=neighbor_id, label=body.label, cable_type=body.cable_type,
        connector_a=body.connector_a, connector_b=body.connector_b, length_m=body.length_m,
        route_metadata=body.route_metadata, notes=body.notes, status=body.status, installed_at=body.installed_at,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _one(db, ctx, cable.id)


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/cables", response_model=Page[CableOut])
async def list_cables(
    status: Literal["planned", "installed", "removed"] | None = None, cable_type: CableType | None = None,
    source: Literal["manual", "discovery_confirmed", "import"] | None = None, label: str | None = None,
    port_id: uuid.UUID | None = None, equipment_id: uuid.UUID | None = None, page: Pagination = Depends(pagination_params),
    db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cable:read")),
) -> Page[CableOut]:
    # Direct-ID filters must not become an oracle for a masked far endpoint.
    if equipment_id is not None:
        await ensure_equipment_access(db, ctx.scope, equipment_id)
    if port_id is not None:
        port = await db.get(EquipmentPort, port_id)
        if port is None:
            raise NotFoundError(f"EquipmentPort {port_id} not found.")
        await cable_service.ensure_port_visible(db, ctx.scope, port, what="EquipmentPort", ref=port_id)
    visible_endpoint = (
        select(CableEndpoint.cable_id)
        .join(EquipmentPort, EquipmentPort.id == CableEndpoint.equipment_port_id)
        .where(equipment_visible_clause(ctx.scope, EquipmentPort.equipment_id))
    )
    conditions: list = [Cable.id.in_(visible_endpoint)]
    if status:
        conditions.append(Cable.status == status)
    if cable_type:
        conditions.append(Cable.cable_type == cable_type)
    if source:
        conditions.append(Cable.source == source)
    if label:
        conditions.append(Cable.label.ilike(_like(label), escape="\\"))
    if port_id:
        conditions.append(Cable.id.in_(select(CableEndpoint.cable_id).where(CableEndpoint.equipment_port_id == port_id)))
    if equipment_id:
        conditions.append(
            Cable.id.in_(
                select(CableEndpoint.cable_id)
                .join(EquipmentPort, EquipmentPort.id == CableEndpoint.equipment_port_id)
                .where(EquipmentPort.equipment_id == equipment_id)
            )
        )
    total = (await db.execute(select(func.count()).select_from(Cable).where(*conditions))).scalar_one()
    cables = list(
        (
            await db.execute(
                select(Cable).where(*conditions).order_by(Cable.created_at.desc(), Cable.id).limit(page.limit).offset(page.offset)
            )
        ).scalars()
    )
    return Page[CableOut](items=await serialize(db, ctx.scope, cables), total=total, limit=page.limit, offset=page.offset)


@router.get("/cables/{cable_id}", response_model=CableOut)
async def get_cable(
    cable_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cable:read")),
) -> CableOut:
    return await _one(db, ctx, cable_id)


@router.patch("/cables/{cable_id}", response_model=CableOut)
async def update_cable(
    cable_id: uuid.UUID, body: CablePatchIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> CableOut:
    request_id, correlation_id = _request_ids(request)
    changes = {name: getattr(body, name) for name in body.model_fields_set if name in CablePatchIn.model_fields}
    if "label" in changes and changes["label"] is None or "cable_type" in changes and changes["cable_type"] is None:
        from app.core.errors import ApiError

        raise ApiError(status_code=422, title="Invalid Cable", detail="label and cable_type cannot be cleared.")
    if "route_metadata" in changes and changes["route_metadata"] is None:
        changes["route_metadata"] = {}
    await cable_service.update_cable(
        db, scope=ctx.scope, cable_id=cable_id, changes=changes, expected_version=if_match_version,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _one(db, ctx, cable_id)


@router.post("/cables/{cable_id}/install", response_model=CableOut)
async def install_cable(
    cable_id: uuid.UUID, body: InstallIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> CableOut:
    request_id, correlation_id = _request_ids(request)
    await cable_service.install_cable(
        db, scope=ctx.scope, cable_id=cable_id, installed_at=body.installed_at, expected_version=if_match_version,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _one(db, ctx, cable_id)


@router.post("/cables/{cable_id}/remove", response_model=CableOut)
async def remove_cable(
    cable_id: uuid.UUID, body: RemoveIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> CableOut:
    request_id, correlation_id = _request_ids(request)
    await cable_service.remove_cable(
        db, scope=ctx.scope, cable_id=cable_id, removed_at=body.removed_at, reason=body.reason,
        expected_version=if_match_version, actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return await _one(db, ctx, cable_id)


@router.delete("/cables/{cable_id}", status_code=204)
async def delete_cable(
    cable_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_permission("cable:manage")),
) -> Response:
    request_id, correlation_id = _request_ids(request)
    await cable_service.delete_cable(
        db, scope=ctx.scope, cable_id=cable_id, expected_version=if_match_version, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/topology/ports/{port_id}/trace")
async def trace_port(
    port_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("cable:read")),
) -> dict:
    """device -> port -> cable -> remote port -> remote device, with discovery evidence reported separately."""
    return await trace_service.trace_port(db, scope=ctx.scope, port_id=port_id)


__all__ = ["CABLE_TYPES", "router"]
