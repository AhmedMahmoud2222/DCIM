"""Dashboard summary + exception endpoints (master prompt §14/§15/§16). Every number is
API-derived from real queries — no UI-side aggregation of raw rows (§14: "No UI
scraping. No duplicated frontend operational state."). Filtering (site/building/floor/
room) is applied server-side before aggregation, not client-side after fetching
everything (§15)."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.power_capacity import (
    classify_equipment_redundancy_from_snapshot,
    derive_node_capacity_exceptions,
    derive_node_capacity_exceptions_from_snapshot,
    equipment_power_summary,
    load_equipment_feed_batch,
    load_power_graph_snapshot,
)
from app.application.rbac import require_permission
from app.domain.location.models import Building, Floor, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.power.models import PowerCapacity, PowerNode

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _distinct_equipment_ids(db: AsyncSession) -> list[uuid.UUID]:
    equipment_ids = (
        await db.execute(
            select(PowerNode.owning_asset_id).where(
                PowerNode.node_type == "equipment_power_input", PowerNode.retired_at.is_(None),
                PowerNode.owning_asset_id.isnot(None),
            ).distinct()
        )
    ).scalars().all()
    return [e for e in equipment_ids if e is not None]


async def _build_batch_context(db: AsyncSession, capacity_rows: list[PowerCapacity]):
    """PHASE3_N1_CORRECTION_REPORT.md: builds the one shared `PowerGraphSnapshot` +
    equipment feed batch both dashboard endpoints need, instead of each endpoint
    re-deriving capacity/redundancy per node/per equipment item via its own repeated
    queries. Returns `None` when the snapshot reports `truncated=True` (the active
    graph exceeded `MAX_BATCH_GRAPH_EDGES`) -- callers MUST fall back to the original
    per-node functions for the whole request in that case, never mix the two."""
    snapshot = await load_power_graph_snapshot(db, capacity_rows=capacity_rows)
    if snapshot.truncated:
        return None
    equipment_ids = await _distinct_equipment_ids(db)
    feed_nodes_by_asset, feed_label_by_node = await load_equipment_feed_batch(db, equipment_ids)
    return snapshot, equipment_ids, feed_nodes_by_asset, feed_label_by_node


class SiteSummaryOut(BaseModel):
    sites: int
    buildings: int
    floors: int
    rooms: int
    racks: int
    equipment: int
    power_nodes: int


class CapacitySummaryOut(BaseModel):
    total_configured_kw: float | None
    total_allocated_kw: float | None
    total_available_kw: float | None
    utilization_pct: float | None
    nodes_with_known_capacity: int
    nodes_with_unknown_capacity: int


class RackSummaryOut(BaseModel):
    total_racks: int
    occupied_racks: int
    available_racks: int


class PowerSummaryOut(BaseModel):
    total_power_nodes: int
    overloaded_nodes: int
    near_capacity_nodes: int
    missing_power_path_equipment: int
    redundancy_degraded_equipment: int


class DashboardSummaryOut(BaseModel):
    site_summary: SiteSummaryOut
    capacity_summary: CapacitySummaryOut
    rack_summary: RackSummaryOut
    power_summary: PowerSummaryOut


async def _filtered_room_ids(db: AsyncSession, *, site_id, building_id, floor_id, room_id) -> set[uuid.UUID] | None:
    """Returns `None` when no location filter was requested (no restriction), otherwise
    the set of room IDs the filter resolves to. Applied server-side (§15) before any
    rack/equipment count runs, never fetched unfiltered and trimmed in the browser."""
    if not any([site_id, building_id, floor_id, room_id]):
        return None
    stmt = select(Room.id).join(Floor, Floor.id == Room.floor_id).join(Building, Building.id == Floor.building_id).join(
        Site, Site.id == Building.site_id
    )
    if room_id is not None:
        stmt = stmt.where(Room.id == room_id)
    if floor_id is not None:
        stmt = stmt.where(Floor.id == floor_id)
    if building_id is not None:
        stmt = stmt.where(Building.id == building_id)
    if site_id is not None:
        stmt = stmt.where(Site.id == site_id)
    return set((await db.execute(stmt)).scalars().all())


@router.get("/summary", response_model=DashboardSummaryOut)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    site_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    floor_id: uuid.UUID | None = None,
    room_id: uuid.UUID | None = None,
    ctx=Depends(require_permission("dashboard:read")),
) -> DashboardSummaryOut:
    """Filters by organization are not yet implemented (master prompt §15 lists
    organization as a filter dimension) — deliberately deferred, not silently dropped:
    the location hierarchy already gives site/building/floor/room drill-down, and
    Organization -> Site is a simple one-hop join a future iteration can add without
    changing this endpoint's shape. Disclosed in PHASE3_IMPLEMENTATION_REPORT.md."""
    room_ids = await _filtered_room_ids(db, site_id=site_id, building_id=building_id, floor_id=floor_id, room_id=room_id)

    # --------------------------------------------------------------- site summary
    sites_count = (await db.execute(select(func.count()).select_from(Site))).scalar_one()
    buildings_count = (await db.execute(select(func.count()).select_from(Building))).scalar_one()
    floors_count = (await db.execute(select(func.count()).select_from(Floor))).scalar_one()
    rooms_count = (await db.execute(select(func.count()).select_from(Room))).scalar_one()

    rack_q = select(RackPlacement.rack_id).where(RackPlacement.effective_to.is_(None))
    if room_ids is not None:
        rack_q = rack_q.where(RackPlacement.room_id.in_(room_ids))
    rack_ids_in_scope = set((await db.execute(rack_q)).scalars().all())
    racks_count = len(rack_ids_in_scope) if room_ids is not None else (
        await db.execute(select(func.count()).select_from(Rack))
    ).scalar_one()

    equip_q = select(EquipmentPlacement.equipment_id).where(EquipmentPlacement.effective_to.is_(None))
    if room_ids is not None:
        equip_q = equip_q.where(EquipmentPlacement.room_id.in_(room_ids))
    equipment_ids_in_scope = set((await db.execute(equip_q)).scalars().all())
    equipment_count = len(equipment_ids_in_scope) if room_ids is not None else (
        await db.execute(select(func.count()).select_from(Equipment))
    ).scalar_one()

    power_nodes_stmt = select(func.count()).select_from(PowerNode).where(PowerNode.retired_at.is_(None))
    power_nodes_count = (await db.execute(power_nodes_stmt)).scalar_one()

    site_summary = SiteSummaryOut(
        sites=sites_count, buildings=buildings_count, floors=floors_count, rooms=rooms_count, racks=racks_count,
        equipment=equipment_count, power_nodes=power_nodes_count,
    )

    # --------------------------------------------------------------- rack summary
    total_racks = racks_count
    occupied_rack_ids = set(
        (
            await db.execute(
                select(EquipmentPlacement.rack_id).where(
                    EquipmentPlacement.effective_to.is_(None), EquipmentPlacement.placement_type == "rack_mounted",
                    EquipmentPlacement.rack_id.isnot(None),
                )
            )
        ).scalars().all()
    )
    if room_ids is not None:
        occupied_rack_ids &= rack_ids_in_scope
    occupied_racks = len(occupied_rack_ids)
    rack_summary = RackSummaryOut(
        total_racks=total_racks, occupied_racks=occupied_racks, available_racks=max(total_racks - occupied_racks, 0)
    )

    # --------------------------------------------------------------- capacity summary
    # `total_allocated_kw`/`total_available_kw`/`utilization_pct` are deliberately `None`
    # at this whole-dashboard level: nodes with a capacity record form a tree (a UPS's
    # configured capacity already counts everything below it), so a flat sum across every
    # node with a record would double- or triple-count the same downstream draw at every
    # level it passes through on its way up. A single meaningful "how much capacity is
    # committed" figure requires picking one level of the tree (e.g. "at every
    # top-of-graph node") which this endpoint does not yet do — disclosed here, not
    # silently faked as a number that looks precise but double-counts. Per-node figures
    # (where allocated/available/utilization *are* well-defined, at that one node) are
    # available via GET /power/nodes/{id}/capacity.
    capacity_rows = (
        await db.execute(select(PowerCapacity).where(PowerCapacity.effective_to.is_(None)))
    ).scalars().all()
    total_configured = 0.0
    known = 0
    unknown = 0
    for cap in capacity_rows:
        effective = float(cap.configured_capacity_kw) if cap.configured_capacity_kw is not None else (
            float(cap.rated_capacity_kw) if cap.rated_capacity_kw is not None else None
        )
        if effective is None:
            unknown += 1
            continue
        known += 1
        total_configured += effective
    capacity_summary = CapacitySummaryOut(
        total_configured_kw=total_configured if known else None,
        total_allocated_kw=None,
        total_available_kw=None,
        utilization_pct=None,
        nodes_with_known_capacity=known,
        nodes_with_unknown_capacity=unknown,
    )

    # --------------------------------------------------------------- power summary
    overloaded = 0
    near_capacity = 0
    missing_path = 0
    degraded = 0
    batch_ctx = await _build_batch_context(db, capacity_rows)
    if batch_ctx is not None:
        snapshot, equipment_ids, feed_nodes_by_asset, feed_label_by_node = batch_ctx
        memo: dict = {}
        for cap in capacity_rows:
            for exc in derive_node_capacity_exceptions_from_snapshot(snapshot, cap.power_node_id, "", memo):
                if exc.code == "CAPACITY_OVERLOAD":
                    overloaded += 1
                elif exc.code == "CAPACITY_NEAR_LIMIT":
                    near_capacity += 1
        for equipment_id in equipment_ids:
            summary = classify_equipment_redundancy_from_snapshot(
                snapshot, equipment_id, feed_nodes_by_asset.get(equipment_id, []), feed_label_by_node, memo
            )
            if summary.redundancy_classification == "degraded":
                degraded += 1
            elif any(not f["has_upstream_path"] for f in summary.feed_nodes):
                missing_path += 1
    else:
        # Fallback (PHASE3_N1_CORRECTION_REPORT.md): the active graph exceeded the
        # batch path's whole-graph bound -- preserve the original per-node behavior
        # exactly (including its own per-node MAX_TRAVERSAL_NODES/DEPTH bound) rather
        # than compute anything from an intentionally-incomplete snapshot.
        for cap in capacity_rows:
            for exc in await derive_node_capacity_exceptions(db, cap.power_node_id, ""):
                if exc.code == "CAPACITY_OVERLOAD":
                    overloaded += 1
                elif exc.code == "CAPACITY_NEAR_LIMIT":
                    near_capacity += 1
        equipment_ids = await _distinct_equipment_ids(db)
        for equipment_id in equipment_ids:
            summary = await equipment_power_summary(db, equipment_id)
            if summary.redundancy_classification == "degraded":
                degraded += 1
            elif any(not f["has_upstream_path"] for f in summary.feed_nodes):
                missing_path += 1

    power_summary = PowerSummaryOut(
        total_power_nodes=power_nodes_count, overloaded_nodes=overloaded, near_capacity_nodes=near_capacity,
        missing_power_path_equipment=missing_path, redundancy_degraded_equipment=degraded,
    )

    return DashboardSummaryOut(
        site_summary=site_summary, capacity_summary=capacity_summary, rack_summary=rack_summary, power_summary=power_summary,
    )


class ExceptionItemOut(BaseModel):
    code: str
    severity: str
    object_type: str
    object_id: uuid.UUID
    message: str


@router.get("/exceptions", response_model=list[ExceptionItemOut])
async def get_dashboard_exceptions(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("dashboard:read")),
) -> list[ExceptionItemOut]:
    """Drill-down source for the dashboard's exception cards (§16) — each item names the
    exact object/condition/message a click should navigate to."""
    out: list[ExceptionItemOut] = []
    capacity_rows = (await db.execute(select(PowerCapacity).where(PowerCapacity.effective_to.is_(None)))).scalars().all()

    batch_ctx = await _build_batch_context(db, capacity_rows)
    if batch_ctx is not None:
        snapshot, equipment_ids, feed_nodes_by_asset, feed_label_by_node = batch_ctx
        label_by_node: dict[uuid.UUID, str] = {}
        node_ids = [cap.power_node_id for cap in capacity_rows]
        if node_ids:
            rows = (await db.execute(select(PowerNode.id, PowerNode.label).where(PowerNode.id.in_(node_ids)))).all()
            label_by_node = {nid: lbl for nid, lbl in rows}
        memo: dict = {}
        for cap in capacity_rows:
            label = label_by_node.get(cap.power_node_id, str(cap.power_node_id))
            for exc in derive_node_capacity_exceptions_from_snapshot(snapshot, cap.power_node_id, label, memo):
                out.append(
                    ExceptionItemOut(
                        code=exc.code, severity=exc.severity, object_type="power_node", object_id=exc.power_node_id,
                        message=exc.message,
                    )
                )
        for equipment_id in equipment_ids:
            summary = classify_equipment_redundancy_from_snapshot(
                snapshot, equipment_id, feed_nodes_by_asset.get(equipment_id, []), feed_label_by_node, memo
            )
            if summary.redundancy_classification == "degraded":
                out.append(
                    ExceptionItemOut(
                        code="REDUNDANCY_DEGRADED", severity="warning", object_type="equipment", object_id=equipment_id,
                        message="Redundant feeds share an upstream dependency or one feed is missing its path.",
                    )
                )
            elif summary.redundancy_classification == "single_feed":
                out.append(
                    ExceptionItemOut(
                        code="POWER_PATH_MISSING", severity="info", object_type="equipment", object_id=equipment_id,
                        message="Only a single power feed is modeled for this equipment.",
                    )
                )
        return out

    # Fallback (PHASE3_N1_CORRECTION_REPORT.md): preserve original per-node behavior
    # exactly, same rationale as get_dashboard_summary's own fallback branch.
    for cap in capacity_rows:
        node = await db.get(PowerNode, cap.power_node_id)
        label = node.label if node else str(cap.power_node_id)
        for exc in await derive_node_capacity_exceptions(db, cap.power_node_id, label):
            out.append(
                ExceptionItemOut(
                    code=exc.code, severity=exc.severity, object_type="power_node", object_id=exc.power_node_id,
                    message=exc.message,
                )
            )

    equipment_ids = await _distinct_equipment_ids(db)
    for equipment_id in equipment_ids:
        summary = await equipment_power_summary(db, equipment_id)
        if summary.redundancy_classification == "degraded":
            out.append(
                ExceptionItemOut(
                    code="REDUNDANCY_DEGRADED", severity="warning", object_type="equipment", object_id=equipment_id,
                    message="Redundant feeds share an upstream dependency or one feed is missing its path.",
                )
            )
        elif summary.redundancy_classification == "single_feed":
            out.append(
                ExceptionItemOut(
                    code="POWER_PATH_MISSING", severity="info", object_type="equipment", object_id=equipment_id,
                    message="Only a single power feed is modeled for this equipment.",
                )
            )
    return out
