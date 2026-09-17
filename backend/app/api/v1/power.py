"""Power topology + capacity endpoints (ARCHITECTURE_REVIEW.md §13/§13a/§14;
PHASE3_GAP_ANALYSIS.md; PHASE3_HOSTILE_SELF_AUDIT.md; PHASE3_CORRECTION_DESIGN.md).
Every mutation is audited and outboxed exactly like racks.py/equipment.py;
`PowerConnection` create/disconnect follow §13a's concurrency recipe precisely
(a system-wide advisory lock, then canonical node-lock ordering, then cycle check on
create -- see power_graph.py's own docstring for why the advisory lock is now the
primary safety mechanism, F-C1 correction; `FOR UPDATE` on disconnect); capacity edits
use the same `version`/`If-Match` pattern as `Rack`/`Equipment`'s own narrow-field
edits, via the shared `parse_if_match` helper (F-M1 correction)."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match, parse_if_match, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.power_capacity import (
    derive_node_capacity_exceptions,
    equipment_power_summary,
    get_capacity_figures,
    get_current_capacity,
)
from app.application.power_graph import (
    GraphTraversalBounded,
    WouldCreateCycle,
    assert_would_not_create_cycle,
    get_downstream_node_ids,
    get_upstream_node_ids,
    lock_node_pair_in_canonical_order,
    with_connection_mutation_lock,
)
from app.application.rbac import require_permission
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment
from app.domain.power.models import (
    PDU,
    UPS,
    Generator,
    PDUOutlet,
    PowerCapacity,
    PowerConnection,
    PowerNode,
    PowerPanel,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/power", tags=["power"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# ------------------------------------------------------------------------- PowerNode I/O


class PowerNodeOut(BaseModel):
    id: uuid.UUID
    node_type: str
    managed_asset_id: uuid.UUID | None
    owning_asset_id: uuid.UUID | None
    label: str
    retired_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PowerAssetIn(BaseModel):
    asset_tag: str = Field(max_length=64)
    name: str = Field(max_length=128)


class PDUIn(PowerAssetIn):
    ip_address: str | None = Field(default=None, max_length=45)
    protocol: str = Field(default="none")
    input_voltage: float | None = None
    rated_current_a: float | None = None
    outlet_count: int | None = None


class UPSIn(PowerAssetIn):
    room_id: uuid.UUID
    capacity_kva: float | None = None
    runtime_minutes: int | None = None


class GeneratorIn(PowerAssetIn):
    site_id: uuid.UUID
    capacity_kw: float | None = None
    fuel_type: str | None = Field(default=None, max_length=32)


class PowerPanelIn(PowerAssetIn):
    room_id: uuid.UUID
    capacity_kw: float | None = None


async def _create_power_asset(
    db: AsyncSession, request: Request, ctx, *, asset_type: str, node_type: str, subtype_cls, subtype_fields: dict,
    asset_tag: str, label: str,
) -> tuple[PowerNode, ManagedAsset]:
    asset = ManagedAsset(asset_type=asset_type, asset_tag=asset_tag, lifecycle_status="planned")
    db.add(asset)
    await db.flush()
    subtype = subtype_cls(id=asset.id, **subtype_fields)
    db.add(subtype)
    node = PowerNode(node_type=node_type, managed_asset_id=asset.id, label=label)
    db.add(node)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action=f"power.{node_type}.create", entity_type=node_type, entity_id=asset.id,
        request_id=request_id, correlation_id=correlation_id, after={"asset_tag": asset.asset_tag, "label": label},
    )
    await write_outbox_event(
        db, event_type="PowerNodeCreated", aggregate_type="power_node", aggregate_id=node.id,
        payload={"node_type": node_type, "managed_asset_id": str(asset.id)}, correlation_id=correlation_id,
    )
    return node, asset


@router.post("/pdus", response_model=PowerNodeOut, status_code=201)
async def create_pdu(
    body: PDUIn, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    node, _ = await _create_power_asset(
        db, request, ctx, asset_type="pdu", node_type="pdu", subtype_cls=PDU,
        subtype_fields={
            "name": body.name, "ip_address": body.ip_address, "protocol": body.protocol,
            "input_voltage": body.input_voltage, "rated_current_a": body.rated_current_a,
            "outlet_count": body.outlet_count, "version": 1,
        },
        asset_tag=body.asset_tag, label=body.name,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


@router.post("/upses", response_model=PowerNodeOut, status_code=201)
async def create_ups(
    body: UPSIn, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    node, _ = await _create_power_asset(
        db, request, ctx, asset_type="ups", node_type="ups", subtype_cls=UPS,
        subtype_fields={
            "name": body.name, "room_id": body.room_id, "capacity_kva": body.capacity_kva,
            "runtime_minutes": body.runtime_minutes, "version": 1,
        },
        asset_tag=body.asset_tag, label=body.name,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


@router.post("/generators", response_model=PowerNodeOut, status_code=201)
async def create_generator(
    body: GeneratorIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    node, _ = await _create_power_asset(
        db, request, ctx, asset_type="generator", node_type="generator", subtype_cls=Generator,
        subtype_fields={
            "name": body.name, "site_id": body.site_id, "capacity_kw": body.capacity_kw,
            "fuel_type": body.fuel_type, "version": 1,
        },
        asset_tag=body.asset_tag, label=body.name,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


@router.post("/power-panels", response_model=PowerNodeOut, status_code=201)
async def create_power_panel(
    body: PowerPanelIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    node, _ = await _create_power_asset(
        db, request, ctx, asset_type="power_panel", node_type="power_panel", subtype_cls=PowerPanel,
        subtype_fields={"name": body.name, "room_id": body.room_id, "capacity_kw": body.capacity_kw, "version": 1},
        asset_tag=body.asset_tag, label=body.name,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


class UtilityIntakeIn(BaseModel):
    label: str = Field(max_length=128)


@router.post("/utility-intakes", response_model=PowerNodeOut, status_code=201)
async def create_utility_intake(
    body: UtilityIntakeIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    """The one node type with neither `managed_asset_id` nor `owning_asset_id` — the
    utility company's own feed is not a DC-owned asset (module docstring, and this
    phase's one documented addition beyond ARCHITECTURE_REVIEW.md §13's literal table)."""
    node = PowerNode(node_type="utility_intake", label=body.label)
    db.add(node)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.utility_intake.create", entity_type="power_node",
        entity_id=node.id, request_id=request_id, correlation_id=correlation_id, after={"label": body.label},
    )
    await write_outbox_event(
        db, event_type="PowerNodeCreated", aggregate_type="power_node", aggregate_id=node.id,
        payload={"node_type": "utility_intake"}, correlation_id=correlation_id,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


class PDUOutletIn(BaseModel):
    pdu_asset_id: uuid.UUID
    outlet_number: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=64)


@router.post("/pdu-outlets", response_model=PowerNodeOut, status_code=201)
async def create_pdu_outlet(
    body: PDUOutletIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    pdu = await db.get(PDU, body.pdu_asset_id)
    if pdu is None:
        raise NotFoundError(f"PDU {body.pdu_asset_id} not found.")
    default_label = f"Outlet {body.outlet_number}"
    node = PowerNode(node_type="pdu_outlet", owning_asset_id=body.pdu_asset_id, label=body.label or default_label)
    db.add(node)
    await db.flush()
    outlet = PDUOutlet(power_node_id=node.id, pdu_asset_id=body.pdu_asset_id, outlet_number=body.outlet_number, label=body.label)
    db.add(outlet)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.pdu_outlet.create", entity_type="power_node", entity_id=node.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"pdu_asset_id": str(body.pdu_asset_id), "outlet_number": body.outlet_number},
    )
    await write_outbox_event(
        db, event_type="PowerNodeCreated", aggregate_type="power_node", aggregate_id=node.id,
        payload={"node_type": "pdu_outlet", "owning_asset_id": str(body.pdu_asset_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


class EquipmentFeedIn(BaseModel):
    equipment_asset_id: uuid.UUID
    label: str = Field(max_length=128)


@router.post("/equipment-feeds", response_model=PowerNodeOut, status_code=201)
async def create_equipment_feed(
    body: EquipmentFeedIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    """§13: "a dual-corded server has two such PowerNode rows (feed A, feed B)" — call
    this twice (once per feed) to model redundant power; the *feed_label* itself is
    recorded on the `PowerConnection` that supplies this node (module docstring), not
    here, since a node's identity doesn't change if it's later re-fed from a different
    upstream source under the same physical connector."""
    equipment = await db.get(Equipment, body.equipment_asset_id)
    if equipment is None:
        raise NotFoundError(f"Equipment {body.equipment_asset_id} not found.")
    node = PowerNode(node_type="equipment_power_input", owning_asset_id=body.equipment_asset_id, label=body.label)
    db.add(node)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.equipment_feed.create", entity_type="power_node", entity_id=node.id,
        request_id=request_id, correlation_id=correlation_id, after={"equipment_asset_id": str(body.equipment_asset_id)},
    )
    await write_outbox_event(
        db, event_type="PowerNodeCreated", aggregate_type="power_node", aggregate_id=node.id,
        payload={"node_type": "equipment_power_input", "owning_asset_id": str(body.equipment_asset_id)},
        correlation_id=correlation_id,
    )
    await db.commit()
    return PowerNodeOut.model_validate(node)


@router.get("/nodes", response_model=Page[PowerNodeOut])
async def list_power_nodes(
    db: AsyncSession = Depends(get_db), pagination: Pagination = Depends(pagination_params),
    node_type: str | None = None, ctx=Depends(require_permission("power:read")),
) -> Page:
    stmt = select(PowerNode)
    if node_type is not None:
        stmt = stmt.where(PowerNode.node_type == node_type)
    total_stmt = select(PowerNode.id)
    if node_type is not None:
        total_stmt = total_stmt.where(PowerNode.node_type == node_type)
    total = len((await db.execute(total_stmt)).all())
    rows = (await db.execute(stmt.order_by(PowerNode.label).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    items = [PowerNodeOut.model_validate(r) for r in rows]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/nodes/{node_id}", response_model=PowerNodeOut)
async def get_power_node(
    node_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:read")),
) -> PowerNodeOut:
    node = await db.get(PowerNode, node_id)
    if node is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")
    return PowerNodeOut.model_validate(node)


@router.post("/nodes/{node_id}/retire", response_model=PowerNodeOut)
async def retire_power_node(
    node_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerNodeOut:
    node = await db.get(PowerNode, node_id)
    if node is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")
    if node.retired_at is None:
        node.retired_at = datetime.now(UTC)
        await db.flush()
        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=ctx.user.id, action="power.node.retire", entity_type="power_node", entity_id=node_id,
            request_id=request_id, correlation_id=correlation_id,
        )
        await write_outbox_event(
            db, event_type="PowerNodeRetired", aggregate_type="power_node", aggregate_id=node_id, payload={},
            correlation_id=correlation_id,
        )
    await db.commit()
    return PowerNodeOut.model_validate(node)


# --------------------------------------------------------------------- PowerConnection


class PowerConnectionIn(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    connection_type: str = Field(default="feed")
    feed_label: str = Field(default="single")
    phase: str | None = None
    voltage: float | None = None
    rated_current_a: float | None = None
    status: str = Field(default="active")


class PowerConnectionUpdate(BaseModel):
    connection_type: str | None = None
    phase: str | None = None
    voltage: float | None = None
    rated_current_a: float | None = None
    status: str | None = None


class PowerConnectionOut(BaseModel):
    id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    connection_type: str
    feed_label: str
    phase: str | None
    voltage: float | None
    rated_current_a: float | None
    status: str
    version: int
    effective_from: datetime
    effective_to: datetime | None

    model_config = {"from_attributes": True}


@router.post("/connections", response_model=PowerConnectionOut, status_code=201)
async def create_power_connection(
    body: PowerConnectionIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerConnectionOut:
    """F-C1 correction (PHASE3_CORRECTION_DESIGN.md Part 6): every statement below the
    advisory-lock acquisition runs while holding
    `power_graph.POWER_TOPOLOGY_MUTATION_LOCK_KEY` for the remainder of this
    transaction -- no other connection-creation transaction, anywhere in the graph, can
    be inside this same critical section concurrently. This is what makes the cycle
    check below trustworthy even for two edges on completely disjoint node pairs (the
    exact race PHASE3_HOSTILE_SELF_AUDIT.md's F-C1 demonstrated the old per-pair-only
    locking could not prevent)."""
    request_id, correlation_id = _request_ids(request)
    try:
        async with with_connection_mutation_lock(db):
            source = await db.get(PowerNode, body.source_node_id)
            target = await db.get(PowerNode, body.target_node_id)
            if source is None:
                raise NotFoundError(f"PowerNode {body.source_node_id} not found.")
            if target is None:
                raise NotFoundError(f"PowerNode {body.target_node_id} not found.")
            if source.retired_at is not None or target.retired_at is not None:
                raise ApiError(status_code=422, title="Node Retired", detail="Cannot connect a retired PowerNode.")

            # Retained beneath the advisory lock for its own same-pair deadlock-avoidance
            # guarantee (§13a); no longer the sole mechanism protecting cross-pair
            # cycles, since the advisory lock above already fully serializes this
            # critical section against every other connection-creation transaction.
            await lock_node_pair_in_canonical_order(db, body.source_node_id, body.target_node_id)

            try:
                await assert_would_not_create_cycle(
                    db, source_node_id=body.source_node_id, target_node_id=body.target_node_id
                )
            except WouldCreateCycle as exc:
                logger.info(
                    "power_connection_cycle_rejected",
                    source_node_id=str(exc.source_node_id), target_node_id=str(exc.target_node_id),
                    request_id=request_id, correlation_id=correlation_id,
                )
                raise ApiError(
                    status_code=422, title="Invalid Topology",
                    detail=(
                        f"Connecting {exc.source_node_id} -> {exc.target_node_id} would create a cycle "
                        "in the power graph."
                    ),
                ) from exc
            except GraphTraversalBounded as exc:
                logger.warning(
                    "power_graph_traversal_bound_exceeded",
                    root_id=str(exc.root_id), limit_kind=exc.limit_kind, direction="downstream",
                    request_id=request_id, correlation_id=correlation_id,
                )
                raise ApiError(
                    status_code=422, title="Graph Too Large",
                    detail=f"Cycle check aborted: traversal from {exc.root_id} exceeded its {exc.limit_kind} bound.",
                ) from exc

            existing = (
                await db.execute(
                    select(PowerConnection.id).where(
                        PowerConnection.source_node_id == body.source_node_id,
                        PowerConnection.target_node_id == body.target_node_id,
                        PowerConnection.feed_label == body.feed_label,
                        PowerConnection.effective_to.is_(None),
                    )
                )
            ).first()
            if existing is not None:
                raise ConflictError(
                    detail=f"An active {body.feed_label!r} connection already exists between these two nodes."
                )

            connection = PowerConnection(
                source_node_id=body.source_node_id, target_node_id=body.target_node_id,
                connection_type=body.connection_type, feed_label=body.feed_label, phase=body.phase,
                voltage=body.voltage, rated_current_a=body.rated_current_a, status=body.status, version=1,
                effective_from=datetime.now(UTC),
            )
            db.add(connection)
            await db.flush()

            await write_audit_log(
                db, actor_user_id=ctx.user.id, action="power.connection.create", entity_type="power_connection",
                entity_id=connection.id, request_id=request_id, correlation_id=correlation_id,
                after={
                    "source_node_id": str(body.source_node_id), "target_node_id": str(body.target_node_id),
                    "feed_label": body.feed_label,
                },
            )
            await write_outbox_event(
                db, event_type="PowerConnectionChanged", aggregate_type="power_connection",
                aggregate_id=connection.id,
                payload={"source_node_id": str(body.source_node_id), "target_node_id": str(body.target_node_id)},
                correlation_id=correlation_id,
            )
            await db.commit()
            return PowerConnectionOut.model_validate(connection)
    except OperationalError as exc:
        # F-C1 correction (PHASE3_CORRECTION_DESIGN.md Part 6, item 3): the chosen
        # advisory-lock design does not require automatic retry for its own expected
        # path (it fully serializes rather than racing and aborting), so this is a
        # defensive net for an unrelated PostgreSQL-level failure (e.g. a genuine
        # deadlock, sqlstate 40P01, from some other lock interaction) rather than a
        # routine occurrence -- logged and surfaced as a clean, retryable 503 rather
        # than an unhandled 500, never silently retried automatically.
        sqlstate = getattr(exc.orig, "sqlstate", None)
        logger.warning(
            "power_connection_create_db_operational_error",
            sqlstate=sqlstate, request_id=request_id, correlation_id=correlation_id,
        )
        raise ApiError(
            status_code=503, title="Service Unavailable",
            detail="A transient database contention error occurred while creating this connection. Retry the request.",
        ) from exc


@router.patch("/connections/{connection_id}", response_model=PowerConnectionOut)
async def update_power_connection(
    connection_id: uuid.UUID, body: PowerConnectionUpdate, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("power:manage")),
) -> PowerConnectionOut:
    connection = await db.get(PowerConnection, connection_id)
    if connection is None:
        raise NotFoundError(f"PowerConnection {connection_id} not found.")
    check_version_match(expected=if_match_version, actual=connection.version)

    before = {
        "connection_type": connection.connection_type, "phase": connection.phase, "voltage": connection.voltage,
        "rated_current_a": connection.rated_current_a, "status": connection.status, "version": connection.version,
    }
    if body.connection_type is not None:
        connection.connection_type = body.connection_type
    if body.phase is not None:
        connection.phase = body.phase
    if body.voltage is not None:
        connection.voltage = body.voltage
    if body.rated_current_a is not None:
        connection.rated_current_a = body.rated_current_a
    if body.status is not None:
        connection.status = body.status
    connection.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.connection.update", entity_type="power_connection",
        entity_id=connection_id, request_id=request_id, correlation_id=correlation_id, before=before,
        after={
            "connection_type": connection.connection_type, "phase": connection.phase, "voltage": connection.voltage,
            "rated_current_a": connection.rated_current_a, "status": connection.status, "version": connection.version,
        },
    )
    await write_outbox_event(
        db, event_type="PowerConnectionChanged", aggregate_type="power_connection", aggregate_id=connection_id,
        payload={"version": connection.version}, correlation_id=correlation_id,
    )
    await db.commit()
    return PowerConnectionOut.model_validate(connection)


@router.post("/connections/{connection_id}/disconnect", response_model=PowerConnectionOut)
async def disconnect_power_connection(
    connection_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> PowerConnectionOut:
    """§13a: the row is locked before either the update or the close, and whichever
    transaction commits first wins; the second, on unblocking, finds `effective_to`
    already set (for a disconnect race) and returns 409, never a silent overwrite."""
    connection = await db.get(PowerConnection, connection_id)
    if connection is None:
        raise NotFoundError(f"PowerConnection {connection_id} not found.")
    locked = (
        await db.execute(
            select(PowerConnection)
            .where(PowerConnection.id == connection_id, PowerConnection.effective_to.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        await db.rollback()
        raise ConflictError(detail="This connection was already disconnected by another request.")

    locked.effective_to = datetime.now(UTC)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.connection.disconnect", entity_type="power_connection",
        entity_id=connection_id, request_id=request_id, correlation_id=correlation_id,
    )
    await write_outbox_event(
        db, event_type="PowerConnectionChanged", aggregate_type="power_connection", aggregate_id=connection_id,
        payload={"disconnected": True}, correlation_id=correlation_id,
    )
    await db.commit()
    return PowerConnectionOut.model_validate(locked)


@router.get("/connections", response_model=Page[PowerConnectionOut])
async def list_power_connections(
    db: AsyncSession = Depends(get_db), pagination: Pagination = Depends(pagination_params),
    node_id: uuid.UUID | None = None, active_only: bool = True, ctx=Depends(require_permission("power:read")),
) -> Page:
    stmt = select(PowerConnection)
    if node_id is not None:
        stmt = stmt.where((PowerConnection.source_node_id == node_id) | (PowerConnection.target_node_id == node_id))
    if active_only:
        stmt = stmt.where(PowerConnection.effective_to.is_(None))
    total = len((await db.execute(stmt.with_only_columns(PowerConnection.id))).all())
    rows = (
        await db.execute(stmt.order_by(PowerConnection.effective_from.desc()).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    items = [PowerConnectionOut.model_validate(r) for r in rows]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


# ------------------------------------------------------------------------------ Topology


class TopologyNodeOut(BaseModel):
    node_id: uuid.UUID
    label: str
    node_type: str


@router.get("/nodes/{node_id}/upstream", response_model=list[TopologyNodeOut])
async def get_node_upstream(
    node_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:read")),
) -> list[TopologyNodeOut]:
    if await db.get(PowerNode, node_id) is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")
    try:
        ids = await get_upstream_node_ids(db, node_id)
    except GraphTraversalBounded as exc:
        request_id, correlation_id = _request_ids(request)
        logger.warning(
            "power_graph_traversal_bound_exceeded",
            root_id=str(exc.root_id), limit_kind=exc.limit_kind, direction="upstream",
            request_id=request_id, correlation_id=correlation_id,
        )
        raise ApiError(
            status_code=422, title="Graph Too Large",
            detail=f"Upstream traversal from {exc.root_id} exceeded its {exc.limit_kind} bound.",
        ) from exc
    if not ids:
        return []
    rows = (await db.execute(select(PowerNode).where(PowerNode.id.in_(ids)))).scalars().all()
    return [TopologyNodeOut(node_id=r.id, label=r.label, node_type=r.node_type) for r in rows]


@router.get("/nodes/{node_id}/downstream", response_model=list[TopologyNodeOut])
async def get_node_downstream(
    node_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:read")),
) -> list[TopologyNodeOut]:
    if await db.get(PowerNode, node_id) is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")
    try:
        ids = await get_downstream_node_ids(db, node_id)
    except GraphTraversalBounded as exc:
        request_id, correlation_id = _request_ids(request)
        logger.warning(
            "power_graph_traversal_bound_exceeded",
            root_id=str(exc.root_id), limit_kind=exc.limit_kind, direction="downstream",
            request_id=request_id, correlation_id=correlation_id,
        )
        raise ApiError(
            status_code=422, title="Graph Too Large",
            detail=f"Downstream traversal from {exc.root_id} exceeded its {exc.limit_kind} bound.",
        ) from exc
    if not ids:
        return []
    rows = (await db.execute(select(PowerNode).where(PowerNode.id.in_(ids)))).scalars().all()
    return [TopologyNodeOut(node_id=r.id, label=r.label, node_type=r.node_type) for r in rows]


# ------------------------------------------------------------------------------ Capacity


class CapacityIn(BaseModel):
    rated_capacity_kw: float | None = None
    configured_capacity_kw: float | None = None
    warning_threshold_pct: float | None = None
    critical_threshold_pct: float | None = None
    redundancy_factor: str | None = None


class CapacityOut(BaseModel):
    power_node_id: uuid.UUID
    rated_capacity_kw: float | None
    configured_capacity_kw: float | None
    effective_capacity_kw: float | None
    allocated_kw: float | None
    available_kw: float | None
    utilization_pct: float | None
    measured_load_kw: float | None
    data_quality: str
    redundancy_factor: str | None
    warning_threshold_pct: float | None
    critical_threshold_pct: float | None
    version: int | None


@router.get("/nodes/{node_id}/capacity", response_model=CapacityOut)
async def get_node_capacity(
    node_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("capacity:read")),
) -> CapacityOut:
    if await db.get(PowerNode, node_id) is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")
    figures = await get_capacity_figures(db, node_id)
    current = await get_current_capacity(db, node_id)
    return CapacityOut(
        power_node_id=node_id, rated_capacity_kw=figures.rated_capacity_kw, configured_capacity_kw=figures.configured_capacity_kw,
        effective_capacity_kw=figures.effective_capacity_kw, allocated_kw=figures.allocated_kw, available_kw=figures.available_kw,
        utilization_pct=figures.utilization_pct, measured_load_kw=figures.measured_load_kw, data_quality=figures.data_quality,
        redundancy_factor=current.redundancy_factor if current else None,
        warning_threshold_pct=(
            float(current.warning_threshold_pct) if current and current.warning_threshold_pct is not None else None
        ),
        critical_threshold_pct=(
            float(current.critical_threshold_pct) if current and current.critical_threshold_pct is not None else None
        ),
        version=current.version if current else None,
    )


@router.put("/nodes/{node_id}/capacity", response_model=CapacityOut)
async def set_node_capacity(
    node_id: uuid.UUID, body: CapacityIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"), ctx=Depends(require_permission("capacity:manage")),
) -> CapacityOut:
    """Close-then-open, same temporal discipline as RackPlacement/EquipmentPlacement —
    a capacity record is versioned history, not an in-place-edited row. `If-Match` is
    required whenever a current record already exists (first-time creation for a node
    with none yet needs no precondition, matching create_rack's own optional-placement
    convention).

    F-M1 correction (PHASE3_CORRECTION_DESIGN.md Part 9): `If-Match` is parsed via the
    shared `parse_if_match` helper (`app/application/concurrency.py`) rather than an ad
    hoc `int(...)` call, so a malformed value raises the same clean `400 Bad Request`
    every other If-Match-consuming endpoint already produces, instead of an unhandled
    `ValueError` surfacing as a generic `500`."""
    if await db.get(PowerNode, node_id) is None:
        raise NotFoundError(f"PowerNode {node_id} not found.")

    current = (
        await db.execute(
            select(PowerCapacity)
            .where(PowerCapacity.power_node_id == node_id, PowerCapacity.effective_to.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()

    if current is not None:
        if_match_version = parse_if_match(if_match)
        if if_match_version is None:
            raise ApiError(
                status_code=428, title="Precondition Required",
                detail="This node already has a capacity record; If-Match is required to replace it.",
            )
        check_version_match(expected=if_match_version, actual=current.version)
        current.effective_to = datetime.now(UTC)
        next_version = current.version + 1
    else:
        next_version = 1

    new_record = PowerCapacity(
        power_node_id=node_id, rated_capacity_kw=body.rated_capacity_kw, configured_capacity_kw=body.configured_capacity_kw,
        warning_threshold_pct=body.warning_threshold_pct, critical_threshold_pct=body.critical_threshold_pct,
        redundancy_factor=body.redundancy_factor, version=next_version, effective_from=datetime.now(UTC),
    )
    db.add(new_record)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.capacity.set", entity_type="power_capacity", entity_id=new_record.id,
        request_id=request_id, correlation_id=correlation_id,
        after={
            "power_node_id": str(node_id), "rated_capacity_kw": body.rated_capacity_kw,
            "configured_capacity_kw": body.configured_capacity_kw,
        },
    )
    await write_outbox_event(
        db, event_type="PowerCapacityChanged", aggregate_type="power_node", aggregate_id=node_id,
        payload={"version": next_version}, correlation_id=correlation_id,
    )
    await db.commit()
    return await get_node_capacity(node_id, db=db, ctx=ctx)


# --------------------------------------------------------------------- Capacity exceptions


class CapacityExceptionOut(BaseModel):
    code: str
    severity: str
    power_node_id: uuid.UUID
    label: str
    message: str
    observed_value: float | None
    threshold: float | None


@router.get("/capacity-exceptions", response_model=list[CapacityExceptionOut])
async def list_capacity_exceptions(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("capacity:read")),
) -> list[CapacityExceptionOut]:
    """Deterministic, on-demand derivation (§13) — not a stored alarm (§36: "do not
    implement the Phase 6 alarm/event engine"). Scans every non-retired PowerNode that
    carries a current capacity record."""
    nodes = (
        await db.execute(
            select(PowerNode.id, PowerNode.label).join(
                PowerCapacity, PowerCapacity.power_node_id == PowerNode.id
            ).where(PowerCapacity.effective_to.is_(None), PowerNode.retired_at.is_(None))
        )
    ).all()
    out: list[CapacityExceptionOut] = []
    for node_id, label in nodes:
        for exc in await derive_node_capacity_exceptions(db, node_id, label):
            out.append(
                CapacityExceptionOut(
                    code=exc.code, severity=exc.severity, power_node_id=exc.power_node_id, label=label,
                    message=exc.message, observed_value=exc.observed_value, threshold=exc.threshold,
                )
            )

    # Redundancy exceptions: every equipment_power_input node's owning equipment,
    # deduplicated (two feed nodes for one equipment item must only be checked once).
    equipment_ids = (
        await db.execute(
            select(PowerNode.owning_asset_id).where(
                PowerNode.node_type == "equipment_power_input", PowerNode.retired_at.is_(None),
                PowerNode.owning_asset_id.isnot(None),
            ).distinct()
        )
    ).scalars().all()
    equipment_ids = [e for e in equipment_ids if e is not None]
    for equipment_id in equipment_ids:
        if equipment_id is None:
            continue
        summary = await equipment_power_summary(db, equipment_id)
        if summary.redundancy_classification == "degraded":
            out.append(
                CapacityExceptionOut(
                    code="REDUNDANCY_DEGRADED", severity="warning", power_node_id=equipment_id,
                    label=str(equipment_id),
                    message="Equipment's redundant power feeds share an upstream dependency or one feed has no active path.",
                    observed_value=None, threshold=None,
                )
            )
        elif summary.redundancy_classification == "single_feed":
            out.append(
                CapacityExceptionOut(
                    code="POWER_PATH_MISSING", severity="info", power_node_id=equipment_id, label=str(equipment_id),
                    message="Equipment has only a single power feed modeled (no redundancy).",
                    observed_value=None, threshold=None,
                )
            )
    return out


class EquipmentPowerSummaryOut(BaseModel):
    equipment_asset_id: uuid.UUID
    feed_nodes: list[dict]
    redundancy_classification: str
    effective_demand_kw: float | None
    data_quality: str


@router.get("/equipment/{equipment_asset_id}/power-summary", response_model=EquipmentPowerSummaryOut)
async def get_equipment_power_summary(
    equipment_asset_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:read")),
) -> EquipmentPowerSummaryOut:
    if await db.get(Equipment, equipment_asset_id) is None:
        raise NotFoundError(f"Equipment {equipment_asset_id} not found.")
    summary = await equipment_power_summary(db, equipment_asset_id)
    return EquipmentPowerSummaryOut(
        equipment_asset_id=summary.equipment_asset_id, feed_nodes=summary.feed_nodes,
        redundancy_classification=summary.redundancy_classification, effective_demand_kw=summary.effective_demand_kw,
        data_quality=summary.data_quality,
    )
