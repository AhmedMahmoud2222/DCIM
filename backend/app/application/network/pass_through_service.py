"""Authoritative pass-through relationships (patch-panel front <-> rear) for Issue #101.

Writers take the same topology advisory lock as cables and neighbor confirmation, so a path
can never be read half-edited by another writer's check. The database independently refuses a
port in two pass-throughs, members from another equipment, and a pass-through without exactly
two members, so the lock is an ordering guarantee, not the only protection.

Visibility follows the cable rules: a restricted caller must see the equipment, and anything
else is a 404 that names only ids the caller supplied.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match
from app.application.network.cable_service import ensure_port_visible
from app.application.network.topology_lock import acquire_topology_lock
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.network.pass_through_models import PortPassThrough, PortPassThroughMember
from app.domain.physical.ports import EquipmentPort


def _invalid(detail: str) -> ApiError:
    return ApiError(status_code=422, title="Invalid Pass-through", detail=detail, type_="https://dcim.internal/errors/validation")


def snapshot(item: PortPassThrough, ports: tuple[uuid.UUID, ...] = ()) -> dict:
    return {
        "id": str(item.id), "equipment_id": str(item.equipment_id), "label": item.label, "version": item.version,
        "ports": sorted(str(p) for p in ports),
    }


async def member_ports(db: AsyncSession, pass_through_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    rows = (
        await db.execute(
            select(PortPassThroughMember.equipment_port_id)
            .where(PortPassThroughMember.pass_through_id == pass_through_id)
            .order_by(PortPassThroughMember.equipment_port_id)
        )
    ).scalars().all()
    return tuple(rows)


async def _load_visible(
    db: AsyncSession, scope: AccessScope, pass_through_id: uuid.UUID, *, for_update: bool = False,
) -> PortPassThrough:
    stmt = select(PortPassThrough).where(PortPassThrough.id == pass_through_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise NotFoundError(f"PassThrough {pass_through_id} not found.")
    ports = await member_ports(db, item.id)
    if not ports:
        raise NotFoundError(f"PassThrough {pass_through_id} not found.")
    port = await db.get(EquipmentPort, ports[0])
    assert port is not None
    await ensure_port_visible(db, scope, port, what="PassThrough", ref=pass_through_id)
    return item


async def get_pass_through(db: AsyncSession, *, scope: AccessScope, pass_through_id: uuid.UUID) -> PortPassThrough:
    return await _load_visible(db, scope, pass_through_id)


async def create_pass_through(
    db: AsyncSession, *, scope: AccessScope, port_a_id: uuid.UUID, port_b_id: uuid.UUID, label: str | None,
    actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> PortPassThrough:
    if port_a_id == port_b_id:
        raise _invalid("a pass-through joins two different ports.")
    label = label.strip() if label is not None else None
    if label == "":
        raise _invalid("label must not be blank.")
    await acquire_topology_lock(db)
    ports: list[EquipmentPort] = []
    for port_id in (port_a_id, port_b_id):
        port = await db.get(EquipmentPort, port_id)
        if port is None:
            raise NotFoundError(f"EquipmentPort {port_id} not found.")
        await ensure_port_visible(db, scope, port, what="EquipmentPort", ref=port_id)
        ports.append(port)
    if ports[0].equipment_id != ports[1].equipment_id:
        raise _invalid("both ports of a pass-through must belong to the same equipment.")
    taken = (
        await db.execute(select(PortPassThroughMember.equipment_port_id).where(
            PortPassThroughMember.equipment_port_id.in_([port_a_id, port_b_id])))
    ).first()
    if taken is not None:
        raise ConflictError("A port already belongs to a pass-through; delete that one first.")
    item = PortPassThrough(
        id=uuid.uuid4(), equipment_id=ports[0].equipment_id, label=label, created_by_user_id=actor_user_id, version=1,
    )
    db.add(item)
    await db.flush()
    db.add_all([
        PortPassThroughMember(id=uuid.uuid4(), pass_through_id=item.id, equipment_id=item.equipment_id, equipment_port_id=pid)
        for pid in sorted((port_a_id, port_b_id), key=str)
    ])
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="pass_through.create", entity_type="port_pass_through", entity_id=item.id,
        request_id=request_id, correlation_id=correlation_id, after=snapshot(item, (port_a_id, port_b_id)),
    )
    await write_outbox_event(
        db, event_type="PassThroughRecorded", aggregate_type="port_pass_through", aggregate_id=item.id,
        payload={"equipment_id": str(item.equipment_id)}, correlation_id=correlation_id, causation_id=request_id,
    )
    await db.refresh(item)
    return item


async def delete_pass_through(
    db: AsyncSession, *, scope: AccessScope, pass_through_id: uuid.UUID, expected_version: int, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> None:
    await acquire_topology_lock(db)
    item = await _load_visible(db, scope, pass_through_id, for_update=True)
    check_version_match(expected=expected_version, actual=item.version)
    ports = await member_ports(db, item.id)
    before = snapshot(item, ports)
    await db.delete(item)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="pass_through.delete", entity_type="port_pass_through", entity_id=pass_through_id,
        request_id=request_id, correlation_id=correlation_id, before=before,
    )
    await write_outbox_event(
        db, event_type="PassThroughRemoved", aggregate_type="port_pass_through", aggregate_id=pass_through_id,
        payload={"equipment_id": before["equipment_id"]}, correlation_id=correlation_id, causation_id=request_id,
    )
