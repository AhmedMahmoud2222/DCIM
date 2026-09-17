"""Power capacity, redundancy, and capacity-exception derivation (ARCHITECTURE_REVIEW.md
§14; master prompt §7/§8/§11/§12/§13). Every unit is explicit in its own name
(`_kw`/`_pct`) — never mixed (master prompt §7: "Do not mix electrical units"). Every
value that cannot be computed from what is actually stored is returned as `None` with an
explicit `data_quality` tag (`"known" | "unknown" | "not_applicable"`), never silently
treated as zero (master prompt §29).

**Capacity semantics, stated once (§8):**
- `rated_capacity_kw` — nameplate capability, as entered for this `PowerNode`.
- `configured_capacity_kw` — an operationally configured limit; falls back to
  `rated_capacity_kw` when unset (ARCHITECTURE_REVIEW.md §14's own formula).
  `effective_capacity_kw = COALESCE(configured_capacity_kw, rated_capacity_kw)`.
- `allocated_kw` — **not** a stored column. It is this module's own topology-derived
  roll-up: the sum of this node's direct children's `effective_capacity_kw`, where a
  child that itself has no capacity record is expanded one level further (its own
  children summed instead) — bottom-up, iterative (explicit stack, bounded exactly like
  `power_graph`'s traversal), stopping the moment a node *with* its own capacity record is
  reached (that record already represents everything downstream of it — expanding past it
  too would double-count). There is no telemetry yet (§35/§37), so this is the only
  meaningful "how much is committed downstream" a Phase 3 system can compute; it is never
  conflated with `measured_load_kw` (§14's column, always `None` in this phase).
- `available_kw = effective_capacity_kw - allocated_kw` when both are known; `None`
  ("unknown") otherwise. A negative result is preserved, not clamped to zero — that *is*
  the overload condition (§8: "negative available capacity... explicitly representing
  overload", never silently hidden).
- `utilization_pct = allocated_kw / effective_capacity_kw * 100` when `effective_capacity_kw`
  is known and > 0; `None` ("not_applicable") when `effective_capacity_kw` is exactly 0
  (division by zero is never attempted), `None` ("unknown") when either input is unknown.

**Redundancy de-duplication (§12 — "do not double-count A/B redundant power feeds"):**
this module's per-node `allocated_kw` never merges sibling feeds — it only ever sums a
node's own direct children, and two feeds of the same dual-corded equipment are never
both children of the same parent (each feed's own upstream chain is a separate branch of
the graph by construction). The one place two feeds of one equipment item *are*
deliberately combined into a single number is `equipment_power_summary`'s
`effective_demand_kw`, which takes `max(feed_a, feed_b)` for a redundant (A+B) pair
rather than their sum — the whole point of A/B redundancy is that either feed alone can
carry the full load, so summing them would overstate real demand by up to 2x."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_graph import (
    MAX_TRAVERSAL_DEPTH,
    MAX_TRAVERSAL_NODES,
    get_upstream_node_ids,
    non_retired_node_ids,
)
from app.core.logging import get_logger
from app.domain.power.models import PowerCapacity, PowerConnection, PowerNode

logger = get_logger(__name__)

DEFAULT_WARNING_THRESHOLD_PCT = 80.0
DEFAULT_CRITICAL_THRESHOLD_PCT = 95.0

# Capacity-exception condition codes (master prompt §13) — deliberately a fixed,
# machine-readable set, not free text, so the frontend/API contract is stable.
CAPACITY_OVERLOAD = "CAPACITY_OVERLOAD"
CAPACITY_NEAR_LIMIT = "CAPACITY_NEAR_LIMIT"
REDUNDANCY_DEGRADED = "REDUNDANCY_DEGRADED"
POWER_PATH_MISSING = "POWER_PATH_MISSING"
POWER_TOPOLOGY_INVALID = "POWER_TOPOLOGY_INVALID"
CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"


@dataclass
class CapacityFigures:
    rated_capacity_kw: float | None
    configured_capacity_kw: float | None
    effective_capacity_kw: float | None
    allocated_kw: float | None
    available_kw: float | None
    utilization_pct: float | None
    measured_load_kw: float | None  # always None in Phase 3 — see module docstring
    data_quality: str  # "known" | "unknown" | "not_applicable"


@dataclass
class CapacityException:
    code: str
    severity: str  # "critical" | "warning" | "info"
    power_node_id: uuid.UUID
    message: str
    observed_value: float | None
    threshold: float | None


def _to_float(value) -> float | None:
    return float(value) if value is not None else None


async def get_current_capacity(db: AsyncSession, power_node_id: uuid.UUID) -> PowerCapacity | None:
    stmt = select(PowerCapacity).where(PowerCapacity.power_node_id == power_node_id, PowerCapacity.effective_to.is_(None))
    return (await db.execute(stmt)).scalar_one_or_none()


async def compute_allocated_kw(db: AsyncSession, root_id: uuid.UUID) -> tuple[float | None, str]:
    """Bottom-up, iterative (explicit stack — not Python recursion), bounded exactly like
    `power_graph`'s own traversal. Returns `(value, data_quality)`. `data_quality` is
    `"not_applicable"` when the node has no downstream children at all (a leaf — there is
    nothing to allocate), `"unknown"` when at least one branch bottoms out at a node with
    neither a capacity record nor further children carrying one, `"known"` otherwise.

    F-H2 correction (PHASE3_CORRECTION_DESIGN.md Part 8, Model A): a retired node must
    not act as an operational bridge for this roll-up either — its own capacity (if it
    still has a stale record) must not be counted into an ancestor's `allocated_kw`, and
    nothing further downstream of it may be discovered through it. Both pass 1's subtree
    discovery and pass 2's `children_of` edge set are filtered against
    `PowerNode.retired_at IS NULL`, mirroring `power_graph._traverse`'s own filtering
    exactly, so a retired PDU between a live utility and live downstream equipment
    contributes nothing to the utility's allocated capacity, and nothing beneath the
    retired PDU is ever discovered. The traversal *root* itself is not filtered here for
    the same reason `power_graph`'s traversal root is not: directly inspecting a retired
    node's own allocation is a legitimate historical/audit query, not a bridging concern."""
    # Pass 1: discover every node in the downstream subtree, level by level (bounded),
    # purely to know the full node set and level order for pass 3 — the actual edges are
    # (re-)fetched in one batched query in pass 2 rather than accumulated here.
    levels: list[list[uuid.UUID]] = []
    visited = {root_id}
    frontier = [root_id]
    depth = 0
    while frontier:
        depth += 1
        if depth > MAX_TRAVERSAL_DEPTH:
            return None, "unknown"
        rows = (
            await db.execute(
                select(PowerConnection.target_node_id).where(
                    PowerConnection.source_node_id.in_(frontier), PowerConnection.effective_to.is_(None)
                )
            )
        ).scalars().all()
        candidate_next = [target_id for target_id in rows if target_id not in visited]
        # F-H2: drop retired candidates before they are counted as part of this
        # subtree — they must not contribute their own capacity, and nothing beyond
        # them may be discovered through them.
        active_next = await non_retired_node_ids(db, set(candidate_next))
        next_frontier = [target_id for target_id in candidate_next if target_id in active_next]
        levels.append(frontier)
        for target_id in next_frontier:
            visited.add(target_id)
        if len(visited) > MAX_TRAVERSAL_NODES:
            return None, "unknown"
        frontier = next_frontier

    # Pass 2: fetch children-by-parent for every discovered node in one batched query.
    # Joined against PowerNode so a retired *target* is excluded even if it happens to
    # be reached as a one-hop child of a node already in `all_ids` (pass 1 only
    # prevents a retired node from being further expanded through -- this join is what
    # keeps it from being counted as a direct child's own capacity contribution too).
    all_ids = list(visited)
    edge_rows = (
        await db.execute(
            select(PowerConnection.source_node_id, PowerConnection.target_node_id)
            .join(PowerNode, PowerNode.id == PowerConnection.target_node_id)
            .where(
                PowerConnection.source_node_id.in_(all_ids),
                PowerConnection.effective_to.is_(None),
                PowerNode.retired_at.is_(None),
            )
        )
    ).all()
    children_of: dict[uuid.UUID, list[uuid.UUID]] = {}
    for source_id, target_id in edge_rows:
        children_of.setdefault(source_id, []).append(target_id)

    capacity_rows = (
        await db.execute(
            select(PowerCapacity).where(
                PowerCapacity.power_node_id.in_(all_ids), PowerCapacity.effective_to.is_(None)
            )
        )
    ).scalars().all()
    capacity_by_node = {c.power_node_id: c for c in capacity_rows}

    def effective_capacity(node_id: uuid.UUID) -> float | None:
        cap = capacity_by_node.get(node_id)
        if cap is None:
            return None
        if cap.configured_capacity_kw is not None:
            return _to_float(cap.configured_capacity_kw)
        return _to_float(cap.rated_capacity_kw)

    # Pass 3: bottom-up aggregation over the discovered subtree (reverse level order),
    # memoized per node — still iterative, still bounded to the same discovered set.
    memo: dict[uuid.UUID, tuple[float | None, str]] = {}

    def node_value(node_id: uuid.UUID) -> tuple[float | None, str]:
        if node_id in memo:
            return memo[node_id]
        cap = effective_capacity(node_id)
        if cap is not None:
            memo[node_id] = (cap, "known")
            return memo[node_id]
        children = children_of.get(node_id, [])
        if not children:
            memo[node_id] = (None, "not_applicable")
            return memo[node_id]
        total = 0.0
        quality = "known"
        for child_id in children:
            value, child_quality = node_value(child_id)
            if value is None:
                quality = "unknown"
                continue
            total += value
        memo[node_id] = (total if quality == "known" else None, quality)
        return memo[node_id]

    # Process children before the root by visiting in reverse discovery order — safe
    # because `node_value` is itself memoized and only ever looks at already-discovered
    # descendants, never re-enters the traversal.
    for level in reversed(levels):
        for node_id in level:
            node_value(node_id)

    if root_id not in children_of or not children_of[root_id]:
        return None, "not_applicable"
    total = 0.0
    quality = "known"
    for child_id in children_of[root_id]:
        value, child_quality = node_value(child_id)
        if value is None:
            quality = "unknown"
        else:
            total += value
    return (total if quality == "known" else None), quality


async def get_capacity_figures(db: AsyncSession, power_node_id: uuid.UUID) -> CapacityFigures:
    cap = await get_current_capacity(db, power_node_id)
    rated = _to_float(cap.rated_capacity_kw) if cap else None
    configured = _to_float(cap.configured_capacity_kw) if cap else None
    effective = configured if configured is not None else rated

    allocated, alloc_quality = await compute_allocated_kw(db, power_node_id)

    if effective is None:
        # No rated/configured capacity recorded at all — available/utilization are
        # meaningless regardless of how well-known the allocation side is.
        available = None
        utilization = None
        data_quality = "unknown"
    elif allocated is None:
        # Capacity is known but at least one branch of the downstream subtree couldn't
        # be resolved (alloc_quality is "unknown") or there's simply nothing downstream
        # to allocate ("not_applicable", e.g. a leaf PDU outlet with nothing plugged in).
        available = None
        utilization = None
        data_quality = alloc_quality
    else:
        available = effective - allocated
        utilization = (allocated / effective * 100.0) if effective > 0 else None
        data_quality = "known" if effective > 0 else "not_applicable"

    return CapacityFigures(
        rated_capacity_kw=rated,
        configured_capacity_kw=configured,
        effective_capacity_kw=effective,
        allocated_kw=allocated,
        available_kw=available,
        utilization_pct=utilization,
        measured_load_kw=_to_float(cap.measured_load_kw) if cap else None,
        data_quality=data_quality,
    )


async def derive_node_capacity_exceptions(db: AsyncSession, power_node_id: uuid.UUID, label: str) -> list[CapacityException]:
    """Deterministic capacity checks for one node (§13). Thresholds fall back to the
    module defaults only when the node's own `PowerCapacity` record doesn't specify one —
    never a silently-invented number with no fallback path documented.

    F-H1 correction (PHASE3_CORRECTION_DESIGN.md Part 7): the precedence check below now
    keys off `figures.data_quality == "unknown"` directly, not off
    `figures.effective_capacity_kw is None` alone. PHASE3_HOSTILE_SELF_AUDIT.md's F-H1
    demonstrated the previous version: a node whose *own* rated/configured capacity was
    known, but whose *allocation* roll-up hit `compute_allocated_kw`'s bounded-traversal
    limit (entirely plausible at the master prompt's own target scale of 5,000+ power
    nodes), had `utilization_pct is None` (correctly) but `effective_capacity_kw is not
    None` (also correctly) -- and the old code's inner `if effective_capacity_kw is None`
    guard meant neither branch fired, silently returning an *empty* exception list for a
    node whose true state was unknown, not healthy. `data_quality` is exactly the field
    `get_capacity_figures` already sets to `"unknown"` in precisely this situation (and
    in every other situation where utilization cannot be computed), so branching on it
    directly closes every such case in one place rather than re-deriving a narrower
    condition that misses some of them."""
    cap = await get_current_capacity(db, power_node_id)
    figures = await get_capacity_figures(db, power_node_id)
    exceptions: list[CapacityException] = []

    if figures.data_quality == "unknown":
        logger.info(
            "power_capacity_unknown",
            power_node_id=str(power_node_id),
            effective_capacity_known=figures.effective_capacity_kw is not None,
            allocated_known=figures.allocated_kw is not None,
        )
        exceptions.append(
            CapacityException(
                code=CAPACITY_UNKNOWN, severity="info", power_node_id=power_node_id,
                message=(
                    f"{label}: capacity or allocation data is incomplete — utilization cannot be computed "
                    "(this may mean rated/configured capacity is not recorded, or the downstream allocation "
                    "could not be fully resolved)."
                ),
                observed_value=None, threshold=None,
            )
        )
        return exceptions
    if figures.utilization_pct is None:
        # Reachable only for data_quality == "not_applicable" (a leaf node with nothing
        # downstream to allocate, or zero effective capacity) -- neither an overload nor
        # an unknown-data condition, so no exception is the correct, healthy outcome.
        return exceptions

    warning_pct: float = DEFAULT_WARNING_THRESHOLD_PCT
    if cap is not None and cap.warning_threshold_pct is not None:
        warning_pct = float(cap.warning_threshold_pct)
    critical_pct: float = DEFAULT_CRITICAL_THRESHOLD_PCT
    if cap is not None and cap.critical_threshold_pct is not None:
        critical_pct = float(cap.critical_threshold_pct)

    if figures.utilization_pct >= critical_pct or figures.utilization_pct > 100.0:
        exceptions.append(
            CapacityException(
                code=CAPACITY_OVERLOAD, severity="critical", power_node_id=power_node_id,
                message=f"{label}: utilization {figures.utilization_pct:.1f}% >= critical threshold {critical_pct:.1f}%.",
                observed_value=figures.utilization_pct, threshold=critical_pct,
            )
        )
    elif figures.utilization_pct >= warning_pct:
        exceptions.append(
            CapacityException(
                code=CAPACITY_NEAR_LIMIT, severity="warning", power_node_id=power_node_id,
                message=f"{label}: utilization {figures.utilization_pct:.1f}% >= warning threshold {warning_pct:.1f}%.",
                observed_value=figures.utilization_pct, threshold=warning_pct,
            )
        )
    return exceptions


@dataclass
class EquipmentPowerSummary:
    equipment_asset_id: uuid.UUID
    feed_nodes: list[dict]
    redundancy_classification: str  # "dual_feed_healthy" | "single_feed" | "degraded" | "no_power_modeled"
    effective_demand_kw: float | None
    data_quality: str


async def equipment_power_summary(db: AsyncSession, equipment_asset_id: uuid.UUID) -> EquipmentPowerSummary:
    """§10/§11: an equipment item's feeds, redundancy classification, and total demand
    with A/B feeds combined by `max()`, not `sum()` (see module docstring)."""
    feed_nodes = (
        await db.execute(
            select(PowerNode).where(
                PowerNode.owning_asset_id == equipment_asset_id, PowerNode.node_type == "equipment_power_input"
            )
        )
    ).scalars().all()

    if not feed_nodes:
        return EquipmentPowerSummary(
            equipment_asset_id=equipment_asset_id, feed_nodes=[], redundancy_classification="no_power_modeled",
            effective_demand_kw=None, data_quality="not_applicable",
        )

    feed_infos: list[dict[str, uuid.UUID | str | bool | float | None]] = []
    upstream_sets: dict[uuid.UUID, set[uuid.UUID]] = {}
    for node in feed_nodes:
        # F-H2 correction (PHASE3_CORRECTION_DESIGN.md Part 8): "has an upstream path"
        # must mean "reaches some live upstream node via the retirement-aware
        # traversal", not merely "an active connection row happens to point at this
        # node" -- the latter would still be true for `Utility -> Retired PDU ->
        # Equipment` (the direct edge into the equipment's feed node from the retired
        # PDU still exists as a row), incorrectly reporting a healthy path through a
        # node that is no longer operationally part of the graph. `get_upstream_
        # node_ids` already excludes retired nodes from its result (power_graph.py),
        # so a feed fed only by a retired node now correctly yields an empty upstream
        # set here.
        upstream_sets[node.id] = await get_upstream_node_ids(db, node.id)
        has_upstream_path = len(upstream_sets[node.id]) > 0
        figures = await get_capacity_figures(db, node.id)
        feed_infos.append(
            {
                "power_node_id": node.id, "feed_label": None, "has_upstream_path": has_upstream_path,
                "effective_capacity_kw": figures.effective_capacity_kw,
            }
        )

    # feed_label is stored on the *connection*, not the node — attach it from whichever
    # active incoming connection currently feeds this node (there is at most one per
    # node in ordinary operation; if more than one exists, that itself is a topology
    # question surfaced separately, not silently picked between).
    for info in feed_infos:
        conn = (
            await db.execute(
                select(PowerConnection.feed_label).where(
                    PowerConnection.target_node_id == info["power_node_id"], PowerConnection.effective_to.is_(None)
                ).limit(1)
            )
        ).scalar_one_or_none()
        info["feed_label"] = conn

    labels = {info["feed_label"] for info in feed_infos if info["feed_label"] is not None}
    missing_upstream = [info for info in feed_infos if not info["has_upstream_path"]]

    if len(feed_nodes) == 1:
        classification = "single_feed"
    elif {"A", "B"} <= labels:
        node_a = next(n for n in feed_nodes if any(i["feed_label"] == "A" and i["power_node_id"] == n.id for i in feed_infos))
        node_b = next(n for n in feed_nodes if any(i["feed_label"] == "B" and i["power_node_id"] == n.id for i in feed_infos))
        shared_ancestors = (upstream_sets[node_a.id] & upstream_sets[node_b.id]) - {node_a.id, node_b.id}
        if missing_upstream or shared_ancestors:
            classification = "degraded"
        else:
            classification = "dual_feed_healthy"
    else:
        classification = "degraded"

    known_capacities: list[float] = [
        cap_kw for info in feed_infos if isinstance(cap_kw := info["effective_capacity_kw"], float)
    ]
    effective_demand: float | None
    if not known_capacities:
        effective_demand = None
        data_quality = "unknown"
    elif classification in ("dual_feed_healthy", "degraded") and {"A", "B"} <= labels:
        effective_demand = max(known_capacities)
        data_quality = "known" if len(known_capacities) == len(feed_infos) else "unknown"
    else:
        effective_demand = sum(known_capacities)
        data_quality = "known" if len(known_capacities) == len(feed_infos) else "unknown"

    return EquipmentPowerSummary(
        equipment_asset_id=equipment_asset_id, feed_nodes=feed_infos, redundancy_classification=classification,
        effective_demand_kw=effective_demand, data_quality=data_quality,
    )
