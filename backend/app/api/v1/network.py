"""Authoritative network inventory, topology, and modeled-path APIs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.network_graph import GraphInterface, trace_to_core
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
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
