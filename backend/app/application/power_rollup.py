"""Deterministic capacity rollups (Issue #102, area B).

`compute_rollup` is a pure function over plain inputs, so the A/B rules below are testable without a
database; `load_rollup_inputs` builds those inputs from the one existing power graph, `PowerCapacity`
rows, protection devices and canonical `power_kw` telemetry.

Quantities, never mixed:
  rated_kw      nameplate of the node (protection device: rating x voltage, x sqrt(3) when three-phase)
  capacity_kw   effective limit: configured, else rated (`PowerCapacity`), else the device rating
  load_kw       what flows through the node now: measured where fresh, last-known where stale, nameplate
                estimate where nothing was ever measured
  allocated_kw  nameplate demand placed on the node, independent of measurement
  headroom_kw   capacity_kw - load_kw (negative means overload and is never clamped)

Dual-feed rule (the regression the tests pin): an equipment item has ONE demand `D`, not one per feed.
D is split across its *live* inlets (default 50/50 for two, proportional to inlet capacity when the
inlets are asymmetric) and each share flows up the graph, splitting again over live parents. Racks,
rooms and sites add D once. Summing every feed's full draw (naive A + B) would report 2D.

Scenarios per equipment: normal_dual_feed, asymmetric_feeds, one_feed_failed (a path exists but an
open/tripped/out-of-service device, a retired node or a non-active connection interrupts it),
one_feed_missing (a second inlet exists with no active upstream connection), single_feed, unserved (no
live inlet; its demand is reported as unserved, not as load), no_power_modeled.

Data quality per node: measured, stale, estimated, mixed, missing. `unknown` protection state is never
treated as interrupting and is reported as a data-quality warning on everything downstream of it.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SOURCE_TIER = ("ups", "generator", "power_panel", "pdu")
# Only these may be the top of a live path; a floating breaker, outlet, circuit or inlet is unpowered.
ROOT_TYPES = ("utility_intake", "generator", "ups", "power_panel", "pdu")
FRESHNESS_SECONDS = 900
DEFAULT_WARNING_PCT = 80.0
DEFAULT_CRITICAL_PCT = 95.0


@dataclass(frozen=True)
class NodeIn:
    id: uuid.UUID
    node_type: str
    label: str
    site_id: uuid.UUID | None = None
    room_id: uuid.UUID | None = None
    capacity_kw: float | None = None
    rated_kw: float | None = None
    state: str | None = None  # protection state
    status: str | None = None  # protection status
    warning_pct: float | None = None
    critical_pct: float | None = None
    redundancy_factor: str | None = None


@dataclass(frozen=True)
class EdgeIn:
    source: uuid.UUID
    target: uuid.UUID
    feed_label: str = "single"
    status: str = "active"


@dataclass(frozen=True)
class EquipmentIn:
    id: uuid.UUID
    label: str
    inlets: tuple[tuple[uuid.UUID, float | None], ...]  # (inlet node id, inlet capacity_kw)
    rack_id: uuid.UUID | None = None
    room_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    measured_kw: float | None = None
    measured_at: datetime | None = None


@dataclass
class EquipmentResult:
    id: uuid.UUID
    label: str
    scenario: str
    quality: str  # measured | stale | estimated | missing
    demand_kw: float | None
    allocated_kw: float | None
    served: bool
    live_inlets: int
    inlet_count: int
    asymmetric: bool = False


@dataclass
class NodeResult:
    id: uuid.UUID
    node_type: str
    label: str
    rated_kw: float | None
    capacity_kw: float | None
    load_kw: float
    allocated_kw: float
    headroom_kw: float | None
    allocated_headroom_kw: float | None
    utilization_pct: float | None
    quality: str
    state: str | None
    interrupted: bool
    overloaded: bool
    level: str  # ok | warning | critical | overload | unknown
    measured_count: int = 0
    stale_count: int = 0
    estimated_count: int = 0
    missing_count: int = 0
    warnings: list[str] = field(default_factory=list)
    contributors: tuple[uuid.UUID, ...] = ()


@dataclass
class ScopeResult:
    scope: str  # rack | room | site
    id: uuid.UUID | None
    load_kw: float
    allocated_kw: float
    unserved_kw: float
    capacity_kw: float | None
    capacity_basis: str | None
    headroom_kw: float | None
    utilization_pct: float | None
    quality: str
    equipment_count: int
    # Equipment in scope whose demand is unknown. Such equipment adds nothing to `load_kw`, so a scope with
    # missing_demand_count > 0 reports only a LOWER BOUND on its load; `quality` alone cannot tell "mixed because some
    # demand is missing" from "mixed measured/stale".
    missing_demand_count: int = 0


@dataclass
class RollupResult:
    nodes: dict[uuid.UUID, NodeResult]
    equipment: dict[uuid.UUID, EquipmentResult]
    racks: dict[uuid.UUID, ScopeResult]
    rooms: dict[uuid.UUID, ScopeResult]
    site: ScopeResult
    warnings: list[str]


def _classify_quality(m: int, s: int, e: int, x: int) -> str:
    present = (m > 0) + (s > 0) + (e > 0)
    if present == 0:
        return "missing"
    if x > 0 or present > 1:
        return "mixed"
    if m:
        return "measured"
    return "stale" if s else "estimated"


def _equipment_quality(item: EquipmentIn, now: datetime, freshness: timedelta) -> tuple[str, float | None]:
    if item.measured_kw is not None and item.measured_at is not None:
        if now - item.measured_at <= freshness:
            return "measured", item.measured_kw
        return "stale", item.measured_kw
    return "estimated", None  # caller substitutes the nameplate figure; missing if there is none


def _allocated(item: EquipmentIn, feed_labels: dict[uuid.UUID, str]) -> float | None:
    """Mirrors `equipment_power_summary`: A/B pairs take the max, anything else sums."""
    caps = [c for _, c in item.inlets if c is not None]
    if not caps or len(caps) != len(item.inlets):
        return None
    labels = {feed_labels.get(n) for n, _ in item.inlets}
    return max(caps) if {"A", "B"} <= labels else sum(caps)


def compute_rollup(
    nodes: list[NodeIn], edges: list[EdgeIn], equipment: list[EquipmentIn], *, now: datetime,
    site_id: uuid.UUID | None = None, freshness_seconds: int = FRESHNESS_SECONDS,
) -> RollupResult:
    freshness = timedelta(seconds=freshness_seconds)
    by_id = {n.id: n for n in nodes}
    warnings: list[str] = []

    def interrupting(n: NodeIn) -> bool:
        return n.state in ("open", "tripped") or n.status == "out_of_service"

    parents: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    feed_label_of: dict[uuid.UUID, str] = {}
    live_edges: list[EdgeIn] = []
    for edge in sorted(edges, key=lambda x: (str(x.source), str(x.target), x.feed_label)):
        if edge.source not in by_id or edge.target not in by_id:
            continue
        feed_label_of.setdefault(edge.target, edge.feed_label)
        parents[edge.target].append(edge.source)
        if edge.status == "active":
            live_edges.append(edge)

    # live[n]: n conducts and has a live path to a root (a node with no active upstream edge at all).
    active_parents: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for e in live_edges:
        active_parents[e.target].append(e.source)
    memo: dict[uuid.UUID, bool] = {}

    def is_live(nid: uuid.UUID, trail: frozenset[uuid.UUID] = frozenset()) -> bool:
        if nid in memo:
            return memo[nid]
        if nid in trail:
            return False
        n = by_id[nid]
        if interrupting(n):
            memo[nid] = False
            return False
        ps = active_parents.get(nid, [])
        if not ps:
            # A node whose only upstream edges are inactive is cut off, not a root.
            result = nid not in parents and n.node_type in ROOT_TYPES
        else:
            result = any(is_live(p, trail | {nid}) for p in ps)
        memo[nid] = result
        return result

    # Equipment demand split across live inlets.
    node_load: dict[uuid.UUID, float] = defaultdict(float)
    node_alloc: dict[uuid.UUID, float] = defaultdict(float)
    node_eq: dict[uuid.UUID, list[set[uuid.UUID]]] = defaultdict(lambda: [set(), set(), set(), set()])
    eq_results: dict[uuid.UUID, EquipmentResult] = {}
    for item in sorted(equipment, key=lambda x: str(x.id)):
        allocated = _allocated(item, feed_label_of)
        quality, demand = _equipment_quality(item, now, freshness)
        if demand is None:
            demand, quality = (allocated, "estimated") if allocated is not None else (None, "missing")
        n_in = len(item.inlets)
        if n_in == 0:
            eq_results[item.id] = EquipmentResult(
                item.id, item.label, "no_power_modeled", quality, demand, allocated, False, 0, 0
            )
            continue
        live = [(nid, cap) for nid, cap in item.inlets if nid in by_id and is_live(nid)]
        connected = [nid for nid, _ in item.inlets if nid in by_id and active_parents.get(nid)]
        asymmetric = False
        if not live:
            scenario = "unserved"
        elif n_in == 1:
            scenario = "single_feed"
        elif len(live) == n_in:
            caps = [c for _, c in live]
            asymmetric = all(c is not None for c in caps) and len({round(float(c or 0), 3) for c in caps}) > 1
            scenario = "asymmetric_feeds" if asymmetric else "normal_dual_feed"
        elif len(connected) < n_in and len(live) == len(connected):
            scenario = "one_feed_missing"
        else:
            scenario = "one_feed_failed"
        eq_results[item.id] = EquipmentResult(
            item.id, item.label, scenario, quality, demand, allocated, bool(live), len(live), n_in, asymmetric
        )
        if not live:
            warnings.append(f"{item.label}: no live power path; demand {demand if demand is not None else 'unknown'} kW unserved")
            continue
        if scenario in ("one_feed_failed", "one_feed_missing", "single_feed"):
            warnings.append(f"{item.label}: redundancy lost ({scenario})")
        if demand is None:
            for nid, _ in live:
                node_eq[nid][3].add(item.id)
            continue
        caps = [c for _, c in live]
        if asymmetric and sum(float(c or 0) for c in caps) > 0:
            total = sum(float(c or 0) for c in caps)
            shares = [float(c or 0) / total for c in caps]
        else:
            shares = [1.0 / len(live)] * len(live)
        alloc = allocated or 0.0
        slot = {"measured": 0, "stale": 1, "estimated": 2}[quality]
        for (nid, _), share in zip(live, shares, strict=True):
            node_load[nid] += demand * share
            node_alloc[nid] += alloc * share
            node_eq[nid][slot].add(item.id)

    # Propagate upstream in reverse topological order over live edges; each node splits over live parents.
    live_parents = {n: [p for p in ps if is_live(p) and is_live(n)] for n, ps in active_parents.items()}
    indeg: dict[uuid.UUID, int] = defaultdict(int)
    for ps in live_parents.values():
        for p in ps:
            indeg[p] += 1  # p has one more child to wait for
    for nid in by_id:
        indeg.setdefault(nid, 0)
    ready = sorted((k for k, d in indeg.items() if d == 0), key=str)
    order: list[uuid.UUID] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for p in sorted(live_parents.get(cur, []), key=str):
            indeg[p] -= 1
            if indeg[p] == 0:
                ready.append(p)
                ready.sort(key=str)
    for nid in order:  # children before parents
        ps = live_parents.get(nid, [])
        if not ps:
            continue
        for p in ps:
            node_load[p] += node_load[nid] / len(ps)
            node_alloc[p] += node_alloc[nid] / len(ps)
            for i in range(4):
                node_eq[p][i] |= node_eq[nid][i]

    node_results: dict[uuid.UUID, NodeResult] = {}
    for n in sorted(nodes, key=lambda x: (x.node_type, x.label, str(x.id))):
        load = round(node_load.get(n.id, 0.0), 6)
        alloc = round(node_alloc.get(n.id, 0.0), 6)
        cap = n.capacity_kw
        headroom = None if cap is None else round(cap - load, 6)
        alloc_headroom = None if cap is None else round(cap - alloc, 6)
        util = None if cap is None or cap <= 0 else round(load / cap * 100.0, 4)
        sets = node_eq.get(n.id, [set(), set(), set(), set()])
        m, s_, e_, x = (len(z) for z in sets)
        interrupted = interrupting(n)
        warn = n.warning_pct if n.warning_pct is not None else DEFAULT_WARNING_PCT
        crit = n.critical_pct if n.critical_pct is not None else DEFAULT_CRITICAL_PCT
        if cap is None or (cap <= 0 and load > 0):
            level = "unknown" if cap is None else "overload"
        elif load > cap:
            level = "overload"
        elif util is not None and util >= crit:
            level = "critical"
        elif util is not None and util >= warn:
            level = "warning"
        else:
            level = "ok"
        nr = NodeResult(
            n.id, n.node_type, n.label, n.rated_kw, cap, load, alloc, headroom, alloc_headroom, util,
            _classify_quality(m, s_, e_, x), n.state, interrupted, level == "overload", level, m, s_, e_, x,
        )
        nr.contributors = tuple(sorted(set().union(*sets), key=str))
        if n.state == "unknown":
            nr.warnings.append("protection state unknown; downstream load assumed to flow")
        if n.node_type == "protection_device" and n.status == "out_of_service":
            nr.warnings.append("device out of service")
        node_results[n.id] = nr

    # Rack, room and site scopes add each served equipment demand exactly once.
    def scope_of(scope: str, ident: uuid.UUID | None, items: list[EquipmentIn]) -> ScopeResult:
        load = alloc = unserved = 0.0
        counts = [0, 0, 0, 0]
        for it in items:
            r = eq_results[it.id]
            if r.demand_kw is None:
                counts[3] += 1
                continue
            if r.served:
                load += r.demand_kw
                alloc += r.allocated_kw or 0.0
                counts[{"measured": 0, "stale": 1, "estimated": 2, "missing": 3}[r.quality]] += 1
            else:
                unserved += r.demand_kw
        return ScopeResult(
            scope, ident, round(load, 6), round(alloc, 6), round(unserved, 6), None, None, None, None,
            _classify_quality(*counts), len(items), counts[3],
        )

    racks: dict[uuid.UUID, ScopeResult] = {}
    rooms: dict[uuid.UUID, ScopeResult] = {}
    for rack_id in sorted({e.rack_id for e in equipment if e.rack_id}, key=str):
        racks[rack_id] = scope_of("rack", rack_id, [e for e in equipment if e.rack_id == rack_id])
    room_ids = sorted({e.room_id for e in equipment if e.room_id} | {n.room_id for n in nodes if n.room_id}, key=str)
    for room_id in room_ids:
        sc = scope_of("room", room_id, [e for e in equipment if e.room_id == room_id])
        cap, basis = _tier_capacity([n for n in nodes if n.room_id == room_id])
        _apply_capacity(sc, cap, basis)
        rooms[room_id] = sc
    site_sc = scope_of("site", site_id, list(equipment))
    cap, basis = _tier_capacity(list(nodes))
    _apply_capacity(site_sc, cap, basis)
    return RollupResult(node_results, eq_results, racks, rooms, site_sc, sorted(set(warnings)))


def _tier_capacity(nodes: list[NodeIn]) -> tuple[float | None, str | None]:
    """Scope capacity = sum of the highest tier present (ups, else generator, else panel, else pdu).
    A 2N tier counts half its sum, because either side alone must carry the load."""
    for tier in SOURCE_TIER:
        members = [n for n in nodes if n.node_type == tier and n.capacity_kw is not None]
        if members:
            total = sum(float(n.capacity_kw or 0) for n in members)
            if any(n.redundancy_factor == "2N" for n in members):
                total /= 2.0
            return round(total, 6), tier
    return None, None


def _apply_capacity(sc: ScopeResult, cap: float | None, basis: str | None) -> None:
    sc.capacity_kw, sc.capacity_basis = cap, basis
    if cap is not None:
        sc.headroom_kw = round(cap - sc.load_kw, 6)
        sc.utilization_pct = round(sc.load_kw / cap * 100.0, 4) if cap > 0 else None
