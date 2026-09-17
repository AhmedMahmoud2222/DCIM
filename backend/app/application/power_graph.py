"""Power-graph traversal and topology-mutation safety (ARCHITECTURE_REVIEW.md §13/§13a;
PHASE3_GAP_ANALYSIS.md §6). Two concerns live here, deliberately together since both
guard the same invariant — the power distribution graph is a DAG, never a cycle:

1. **Traversal** (upstream/downstream/ancestors/descendants) is iterative, not recursive
   Python (master prompt §6: "Do not implement recursive Python traversal without a
   deliberate depth/visited-set strategy"). Each call is a breadth-first walk using an
   explicit `frontier`/`visited` set and one batched SQL query per level (never one query
   per node — the N+1 pattern RT-3 already flagged elsewhere in this codebase is not
   repeated here), bounded by both `MAX_TRAVERSAL_DEPTH` (levels) and
   `MAX_TRAVERSAL_NODES` (total nodes visited) so a pathological or maliciously
   constructed graph cannot force unbounded work — the same "iterative, explicitly
   bounded" discipline PHASE2_NESTED_SVG_CORRECTION_REPORT.md established for
   `svg_sanitizer._walk`, applied here to a different traversal.

2. **Cycle prevention on connection creation** (§13a's H4, explicitly brought back into
   scope by this phase's master prompt §5: "illegal cycles where the architecture
   requires an acyclic distribution graph"). A cycle is a graph-wide property, not a
   single-row fact, so it cannot be a `CHECK` constraint (self-loops *can* be, and are —
   see `power_connection`'s `no_self_loop` constraint — a cycle needs a traversal).
   `assert_would_not_create_cycle` performs that traversal, but only after
   `lock_node_pair_in_canonical_order` has taken `SELECT ... FOR UPDATE` on both
   endpoint `PowerNode` rows in a fixed (lower-UUID-first) order — exactly §13a's
   prescription: "both node rows are locked in a canonical order (lower UUID first) to
   prevent a deadlock between two transactions connecting the same pair of nodes in
   opposite order." Locking first means the cycle check itself sees a consistent,
   contention-free view of the graph around those two nodes: a concurrent transaction
   trying to create the reverse edge (or any edge touching either endpoint) blocks until
   this one commits or rolls back, so two concurrent "add an edge" requests can never
   both observe a cycle-free graph and both succeed in creating one."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.power.models import PowerConnection, PowerNode

MAX_TRAVERSAL_DEPTH = 500
MAX_TRAVERSAL_NODES = 5000


@dataclass
class GraphTraversalBounded(Exception):
    """Raised when a traversal would exceed its depth/node bound. This is a deliberate,
    documented limit — not the same failure mode as an interpreter-level RecursionError,
    and it is always caught by the API layer and turned into a clean 4xx, never a 500
    (master prompt §30: "unexpected graph depth" is a named observability event)."""

    root_id: uuid.UUID
    limit_kind: str  # "depth" | "nodes"


async def get_downstream_node_ids(db: AsyncSession, root_id: uuid.UUID) -> set[uuid.UUID]:
    """All nodes reachable by following active (`effective_to IS NULL`) connections
    *forward* (source -> target) from `root_id`, breadth-first. Does not include
    `root_id` itself."""
    return await _traverse(db, root_id, forward=True)


async def get_upstream_node_ids(db: AsyncSession, root_id: uuid.UUID) -> set[uuid.UUID]:
    """All nodes reachable by following active connections *backward* (target -> source)
    from `root_id` — i.e. every node that ultimately feeds this one."""
    return await _traverse(db, root_id, forward=False)


async def _traverse(db: AsyncSession, root_id: uuid.UUID, *, forward: bool) -> set[uuid.UUID]:
    visited: set[uuid.UUID] = {root_id}
    frontier: set[uuid.UUID] = {root_id}
    depth = 0
    walk_col = PowerConnection.source_node_id if forward else PowerConnection.target_node_id
    result_col = PowerConnection.target_node_id if forward else PowerConnection.source_node_id

    while frontier:
        depth += 1
        if depth > MAX_TRAVERSAL_DEPTH:
            raise GraphTraversalBounded(root_id=root_id, limit_kind="depth")

        rows = (
            await db.execute(
                select(result_col).where(walk_col.in_(frontier), PowerConnection.effective_to.is_(None))
            )
        ).scalars().all()
        next_frontier = {r for r in rows if r not in visited}
        if not next_frontier:
            break
        visited |= next_frontier
        if len(visited) > MAX_TRAVERSAL_NODES:
            raise GraphTraversalBounded(root_id=root_id, limit_kind="nodes")
        frontier = next_frontier

    visited.discard(root_id)
    return visited


async def lock_node_pair_in_canonical_order(db: AsyncSession, node_a: uuid.UUID, node_b: uuid.UUID) -> None:
    """§13a: "both node rows are locked in a canonical order (lower UUID first) to
    prevent a deadlock between two transactions connecting the same pair of nodes in
    opposite order." Must be called (and awaited to completion) before any read this
    transaction relies on staying consistent for the rest of the connection-create
    operation — the cycle check and the insert that follows it."""
    first, second = sorted([node_a, node_b], key=str)
    await db.execute(select(PowerNode).where(PowerNode.id == first).with_for_update())
    if second != first:
        await db.execute(select(PowerNode).where(PowerNode.id == second).with_for_update())


@dataclass
class WouldCreateCycle(Exception):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID


async def assert_would_not_create_cycle(db: AsyncSession, *, source_node_id: uuid.UUID, target_node_id: uuid.UUID) -> None:
    """Adding an edge `source -> target` closes a cycle iff `target` can already reach
    `source` through some existing chain of active connections — i.e. `source` is already
    a downstream descendant of `target`. Self-loops (`source == target`) are also
    rejected here for a clean, early 4xx even though the DB `CHECK` constraint would
    catch it too (defense in depth, not the sole mechanism — same principle as every
    other DB-enforced invariant in this codebase)."""
    if source_node_id == target_node_id:
        raise WouldCreateCycle(source_node_id=source_node_id, target_node_id=target_node_id)
    downstream_of_target = await get_downstream_node_ids(db, target_node_id)
    if source_node_id in downstream_of_target:
        raise WouldCreateCycle(source_node_id=source_node_id, target_node_id=target_node_id)
