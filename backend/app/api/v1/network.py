"""Authoritative network inventory, topology, and modeled-path APIs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.network_graph import GraphInterface, trace_to_core
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import ConflictError, NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.network.models import NetworkConnection, NetworkDevice, NetworkInterface

router = APIRouter(prefix="/network", tags=["network"])


class DeviceOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    lifecycle_status: str
    name: str
    device_type: str
    source: str
    last_observed_at: datetime | None


class InterfaceOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    name: str
    interface_type: str
    description: str | None
    mac_address: str | None
    role: str
    admin_status: str
    oper_status: str
    speed_mbps: int | None
    duplex: str | None
    mtu: int | None
    native_vlan: int | None
    ip_address: str | None
    source: str
    last_observed_at: datetime | None
    model_config = {"from_attributes": True}


class ConnectionOut(BaseModel):
    id: uuid.UUID
    interface_a_id: uuid.UUID
    interface_b_id: uuid.UUID
    cable_label: str | None
    source: str
    is_authoritative: bool
    model_config = {"from_attributes": True}


class ConnectionCreateIn(BaseModel):
    """An operator-declared physical link.

    Provenance and authority intentionally are not request fields: an operator mutation
    is always authoritative and is never permitted to masquerade as collector/import
    inventory.
    """

    interface_a_id: uuid.UUID
    interface_b_id: uuid.UUID
    cable_label: str | None = Field(default=None, max_length=128)


class TopologyOut(BaseModel):
    devices: list[DeviceOut]
    interfaces: list[InterfaceOut]
    connections: list[ConnectionOut]


class TraceHop(BaseModel):
    device_id: uuid.UUID
    device: str
    device_type: str
    interface_id: uuid.UUID | None
    interface: str | None
    vlan: int | None
    operational_state: str | None


class TraceOut(BaseModel):
    state: str
    statement: str
    source_device_id: uuid.UUID
    hops: list[TraceHop]


async def _devices(db: AsyncSession) -> list[DeviceOut]:
    rows = (
        await db.execute(
            select(NetworkDevice, ManagedAsset)
            .join(ManagedAsset, ManagedAsset.id == NetworkDevice.id)
            .order_by(NetworkDevice.name, NetworkDevice.id)
        )
    ).all()
    return [
        DeviceOut(
            id=d.id,
            asset_tag=a.asset_tag,
            lifecycle_status=a.lifecycle_status,
            name=d.name,
            device_type=d.device_type,
            source=d.source,
            last_observed_at=d.last_observed_at,
        )
        for d, a in rows
    ]


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))):
    return await _devices(db)


@router.get("/devices/{device_id}", response_model=DeviceOut)
async def get_device(device_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))):
    result = next((d for d in await _devices(db) if d.id == device_id), None)
    if result is None:
        raise NotFoundError(f"Network device {device_id} not found.")
    return result


@router.get("/interfaces", response_model=list[InterfaceOut])
async def list_interfaces(
    device_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))
):
    stmt = select(NetworkInterface).order_by(NetworkInterface.device_id, NetworkInterface.name, NetworkInterface.id)
    if device_id:
        stmt = stmt.where(NetworkInterface.device_id == device_id)
    return [InterfaceOut.model_validate(row) for row in (await db.execute(stmt)).scalars()]


@router.get("/interfaces/{interface_id}", response_model=InterfaceOut)
async def get_interface(
    interface_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))
):
    row = await db.get(NetworkInterface, interface_id)
    if row is None:
        raise NotFoundError(f"Network interface {interface_id} not found.")
    return InterfaceOut.model_validate(row)


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(
    interface_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))
):
    stmt = select(NetworkConnection).order_by(NetworkConnection.id)
    if interface_id:
        stmt = stmt.where(or_(NetworkConnection.interface_a_id == interface_id, NetworkConnection.interface_b_id == interface_id))
    return [ConnectionOut.model_validate(row) for row in (await db.execute(stmt)).scalars()]


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


async def _locked_interfaces_for_connection(
    db: AsyncSession, interface_a_id: uuid.UUID, interface_b_id: uuid.UUID
) -> tuple[NetworkInterface, NetworkInterface]:
    """Lock endpoints in UUID order before checking occupancy.

    Migration 0015's trigger remains the database backstop for races (including a
    reversed endpoint order).  These locks make ordinary conflicts deterministic and
    actionable before the insert reaches that trigger.
    """
    endpoint_ids = sorted((interface_a_id, interface_b_id), key=str)
    locked = (
        await db.execute(
            select(NetworkInterface)
            .where(NetworkInterface.id.in_(endpoint_ids))
            .order_by(NetworkInterface.id)
            .with_for_update()
        )
    ).scalars().all()
    by_id = {interface.id: interface for interface in locked}
    missing = [str(interface_id) for interface_id in endpoint_ids if interface_id not in by_id]
    if missing:
        raise NotFoundError(f"Network interface {missing[0]} not found.")
    return by_id[interface_a_id], by_id[interface_b_id]


async def _assert_interfaces_available(
    db: AsyncSession, endpoint_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    occupied = (
        await db.execute(
            select(NetworkConnection.id).where(
                or_(
                    NetworkConnection.interface_a_id.in_(endpoint_ids),
                    NetworkConnection.interface_b_id.in_(endpoint_ids),
                )
            )
        )
    ).first()
    if occupied is not None:
        raise ConflictError("One or both selected interfaces are already connected. Disconnect the existing link first.")


@router.post("/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(
    body: ConnectionCreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network:manage")),
) -> ConnectionOut:
    """Create one authoritative physical link with audit/outbox in the same commit."""
    if body.interface_a_id == body.interface_b_id:
        raise ConflictError("A physical connection requires two distinct interfaces.")

    request_id, correlation_id = _request_ids(request)
    try:
        # Same-device links are allowed (e.g. a loopback test cable or a stacking link
        # between two ports on one chassis) — only a literal self-connection (identical
        # interface on both ends, rejected above) and port occupancy are prohibited.
        # Nothing in docs/NETWORK_3D_INCREMENT.md or the migration 0015 domain rules
        # restricts distinct interfaces on the same device from being physically linked.
        await _locked_interfaces_for_connection(db, body.interface_a_id, body.interface_b_id)
        await _assert_interfaces_available(db, (body.interface_a_id, body.interface_b_id))

        connection = NetworkConnection(
            interface_a_id=body.interface_a_id,
            interface_b_id=body.interface_b_id,
            cable_label=body.cable_label,
            source="operator",
            is_authoritative=True,
        )
        db.add(connection)
        await db.flush()
        await write_audit_log(
            db,
            actor_user_id=ctx.user.id,
            action="network.connection.create",
            entity_type="network_connection",
            entity_id=connection.id,
            request_id=request_id,
            correlation_id=correlation_id,
            after={
                "interface_a_id": str(connection.interface_a_id),
                "interface_b_id": str(connection.interface_b_id),
                "cable_label": connection.cable_label,
                "source": connection.source,
                "is_authoritative": connection.is_authoritative,
            },
        )
        await write_outbox_event(
            db,
            event_type="NetworkConnectionCreated",
            aggregate_type="network_connection",
            aggregate_id=connection.id,
            payload={"interface_a_id": str(connection.interface_a_id), "interface_b_id": str(connection.interface_b_id)},
            correlation_id=correlation_id,
        )
        await db.commit()
        return ConnectionOut.model_validate(connection)
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("One or both selected interfaces are already connected. Refresh topology and try again.") from exc
    except Exception:
        await db.rollback()
        raise


@router.delete("/connections/{connection_id}", status_code=204)
async def disconnect_connection(
    connection_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network:manage")),
) -> Response:
    """Remove an operator-authoritative physical link atomically with its audit/outbox
    event.

    Provenance policy: this endpoint is the operator disconnect workflow, not a general
    delete. A connection recorded from collector, import, or demo observation is not
    something an operator physically undid by removing a cable — it is evidence the
    inventory does not yet match reality, and silently deleting that evidence here would
    let a real physical link disappear from the record without anyone reconciling it.
    Only `is_authoritative` (operator-created) connections may be removed through this
    action; anything else is an actionable 409, never a silent no-op or a 404."""
    request_id, correlation_id = _request_ids(request)
    try:
        connection = (
            await db.execute(select(NetworkConnection).where(NetworkConnection.id == connection_id).with_for_update())
        ).scalar_one_or_none()
        if connection is None:
            raise NotFoundError(f"Network connection {connection_id} not found.")
        if not connection.is_authoritative:
            raise ConflictError(
                f"This link was recorded from {connection.source} observation, not an operator action, and is "
                "protected from the operator disconnect workflow. Reconciling or removing an observed link is not "
                "yet supported here."
            )
        before = {
            "interface_a_id": str(connection.interface_a_id),
            "interface_b_id": str(connection.interface_b_id),
            "cable_label": connection.cable_label,
            "source": connection.source,
            "is_authoritative": connection.is_authoritative,
        }
        await db.delete(connection)
        await db.flush()
        await write_audit_log(
            db,
            actor_user_id=ctx.user.id,
            action="network.connection.disconnect",
            entity_type="network_connection",
            entity_id=connection_id,
            request_id=request_id,
            correlation_id=correlation_id,
            before=before,
        )
        await write_outbox_event(
            db,
            event_type="NetworkConnectionDisconnected",
            aggregate_type="network_connection",
            aggregate_id=connection_id,
            payload=before,
            correlation_id=correlation_id,
        )
        await db.commit()
        return Response(status_code=204)
    except Exception:
        await db.rollback()
        raise


@router.get("/topology", response_model=TopologyOut)
async def topology(db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))):
    return TopologyOut(
        devices=await _devices(db),
        interfaces=await list_interfaces(db=db, ctx=ctx),
        connections=await list_connections(db=db, ctx=ctx),
    )


async def _trace(device_id: uuid.UUID, db: AsyncSession) -> TraceOut:
    devices = await _devices(db)
    by_device = {d.id: d for d in devices}
    if device_id not in by_device:
        raise NotFoundError(f"Network endpoint {device_id} not found.")
    interfaces = (await db.execute(select(NetworkInterface))).scalars().all()
    connections = (await db.execute(select(NetworkConnection))).scalars().all()
    state, interface_path = trace_to_core(
        device_id,
        [GraphInterface(i.id, i.device_id, i.name) for i in interfaces],
        [(c.interface_a_id, c.interface_b_id) for c in connections],
        {d.id for d in devices if d.device_type == "core_switch"},
    )
    by_interface = {i.id: i for i in interfaces}
    hops: list[TraceHop] = []
    if not interface_path:
        d = by_device[device_id]
        hops.append(
            TraceHop(
                device_id=d.id,
                device=d.name,
                device_type=d.device_type,
                interface_id=None,
                interface=None,
                vlan=None,
                operational_state=None,
            )
        )
    else:
        ordered = [
            interface_path[0],
            *[
                item
                for index, item in enumerate(interface_path[1:])
                if index == 0 or by_interface[item].device_id != by_interface[interface_path[index]].device_id
            ],
        ]
        for iid in ordered:
            interface = by_interface[iid]
            device = by_device[interface.device_id]
            hops.append(
                TraceHop(
                    device_id=device.id,
                    device=device.name,
                    device_type=device.device_type,
                    interface_id=iid,
                    interface=interface.name,
                    vlan=interface.native_vlan,
                    operational_state=interface.oper_status,
                )
            )
    statement = (
        "Complete modeled physical path to a core device; this is not a live reachability test."
        if state == "complete"
        else "The inventory does not prove a complete modeled path to a core device."
    )
    return TraceOut(state=state, statement=statement, source_device_id=device_id, hops=hops)


@router.get("/trace/{device_id}", response_model=TraceOut)
async def trace(device_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))):
    return await _trace(device_id, db)


@router.get("/equipment/{equipment_id}/context", response_model=TraceOut)
async def equipment_context(
    equipment_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network:read"))
):
    return await _trace(equipment_id, db)
