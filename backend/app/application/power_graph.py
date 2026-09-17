"""Power-graph traversal and topology-mutation safety (ARCHITECTURE_REVIEW.md §13/§13a;
PHASE3_GAP_ANALYSIS.md §6; PHASE3_HOSTILE_SELF_AUDIT.md F-C1/F-H2;
PHASE3_CORRECTION_DESIGN.md Parts 4/6/8). Three concerns live here, deliberately
together since all three guard the same invariant — the power distribution graph is a
DAG, never a cycle, and its operational (as opposed to historical) shape excludes
retired nodes:

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

   F-H2 correction (Model A, PHASE3_CORRECTION_DESIGN.md Part 8): a retired `PowerNode`
   is excluded from the *operational* traversal it would otherwise participate in as a
   bridge. Concretely, once BFS expansion reaches a candidate next-level node, that node
   is dropped (not added to `visited`, never expanded further) if its `retired_at` is
   set — so `Utility -> Retired PDU -> Equipment` correctly stops at the retired PDU
   rather than reporting `Equipment` as still reachable from `Utility`. The traversal
   *root* itself is deliberately NOT subject to this filter (querying a retired node's
   own immediate neighbors directly still returns an honest answer — retirement hides a
   node from *other* nodes' operational calculations, it does not delete or hide the
   node's own historical connections when inspected directly, per the design's explicit
   "preserve historical/audit information" requirement).

2. **Cycle prevention on connection creation** (§13a's H4, explicitly brought back into
   scope by this phase's master prompt §5: "illegal cycles where the architecture
   requires an acyclic distribution graph"). A cycle is a graph-wide property, not a
   single-row fact, so it cannot be a `CHECK` constraint (self-loops *can* be, and are —
   see `power_connection`'s `no_self_loop` constraint — a cycle needs a traversal).

   F-C1 correction (PHASE3_CORRECTION_DESIGN.md Parts 4/6): the original implementation
   relied solely on `lock_node_pair_in_canonical_order` locking the two endpoint
   `PowerNode` rows before the cycle check. PHASE3_HOSTILE_SELF_AUDIT.md's F-C1 proved
   experimentally that this is insufficient — two concurrent transactions creating
   *different* edges on *disjoint* endpoint pairs (e.g. N1->N2 and N3->N4, with
   pre-existing N2->N3 and N4->N1) never contend for the same locked rows, so each can
   pass its own cycle check against a graph that does not yet reflect the other's
   in-flight edge, and both can commit — jointly closing a cycle neither would have
   accepted alone. The correction adds `POWER_TOPOLOGY_MUTATION_LOCK_KEY`, a single
   fixed-key PostgreSQL transaction-scoped advisory lock
   (`pg_advisory_xact_lock`) acquired as the *first* statement of every connection-
   creation transaction, before the canonical node-pair lock and before the cycle
   check. This fully serializes every connection-creation transaction against every
   other one, system-wide, regardless of which nodes they touch — so no cycle check
   can ever run concurrently with another transaction's uncommitted insert, and each
   check is always evaluated against a fully-committed, ground-truth graph. This was
   verified experimentally in PHASE3_CORRECTION_DESIGN.md Part 5 (Experiment 3, 5/5
   trials: no cycle committed, and the losing transaction failed with the ordinary,
   already-handled `WouldCreateCycle` exception rather than a new database-level error
   class). `lock_node_pair_in_canonical_order`'s per-pair locking is retained
   unchanged beneath the advisory lock — it still prevents the classic same-pair
   opposite-order deadlock, and removing it would gain nothing now that the advisory
   lock already provides full serialization.

   Scope of this guarantee, stated precisely (do not overclaim): active
   `PowerConnection` graph acyclicity is an *application-enforced transactional
   invariant*, upheld by every supported application mutation path (today, exactly
   one: `create_power_connection` in `app/api/v1/power.py`, confirmed by a repository-
   wide search — see PHASE3_CORRECTION_IMPLEMENTATION.md). It is not, and cannot be,
   a database-level constraint (a cycle is a whole-graph property no single-row CHECK
   can express), so a direct SQL write that bypasses this transaction protocol
   entirely (an ad hoc `INSERT INTO power_connection`, a future bulk-import script, a
   manual data-repair session) is not prevented by this mechanism and never has been.
   Any new code path that can insert a `PowerConnection` row MUST acquire
   `with_connection_mutation_lock` before its own cycle check, or it silently reopens
   this exact race through the new path — this is an application-discipline
   requirement, not something the database enforces on its behalf."""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.power.models import PowerConnection, PowerNode

logger = get_logger(__name__)

# F-M5 observability (PHASE3_CORRECTION_DESIGN.md Part 12): an advisory-lock wait longer
# than this is logged as a leading indicator of real contention on connection-creation,
# worth watching before it becomes a measured bottleneck -- not itself an error.
_NOTABLE_LOCK_WAIT_SECONDS = 0.5

MAX_TRAVERSAL_DEPTH = 500
MAX_TRAVERSAL_NODES = 5000

# F-C1 correction: a single, fixed 64-bit-safe constant identifying the one lock every
# active-PowerConnection mutation path must acquire (PHASE3_CORRECTION_DESIGN.md Part 6
# item 15). Arbitrary value, chosen once and never reused for any other advisory-lock
# purpose in this codebase.
POWER_TOPOLOGY_MUTATION_LOCK_KEY = 918_273_645


@dataclass
class GraphTraversalBounded(Exception):
    """Raised when a traversal would exceed its depth/node bound. This is a deliberate,
    documented limit — not the same failure mode as an interpreter-level RecursionError,
    and it is always caught by the API layer and turned into a clean 4xx, never a 500
    (master prompt §30: "unexpected graph depth" is a named observability event)."""

    root_id: uuid.UUID
    limit_kind: str  # "depth" | "nodes"


async def get_downstream_node_ids(db: AsyncSession, root_id: uuid.UUID) -> set[uuid.UUID]:
    """All *operationally active* nodes reachable by following active
    (`effective_to IS NULL`) connections *forward* (source -> target) from `root_id`,
    breadth-first, never passing through a retired node. Does not include `root_id`
    itself, regardless of `root_id`'s own retirement status."""
    return await _traverse(db, root_id, forward=True)


async def get_upstream_node_ids(db: AsyncSession, root_id: uuid.UUID) -> set[uuid.UUID]:
    """All operationally active nodes reachable by following active connections
    *backward* (target -> source) from `root_id` — i.e. every node that ultimately
    feeds this one, never passing through a retired node."""
    return await _traverse(db, root_id, forward=False)


async def non_retired_node_ids(db: AsyncSession, node_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """F-H2 correction: the subset of `node_ids` whose `PowerNode.retired_at IS NULL`.
    Used to keep a retired node from acting as an operational traversal bridge — it is
    simply never added to a BFS frontier, so nothing beyond it is ever discovered
    through it, and it never appears in the returned result set itself."""
    if not node_ids:
        return set()
    rows = (
        await db.execute(select(PowerNode.id).where(PowerNode.id.in_(node_ids), PowerNode.retired_at.is_(None)))
    ).scalars().all()
    return set(rows)


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
        candidate_next = {r for r in rows if r not in visited}
        if not candidate_next:
            break
        # F-H2: drop retired candidates before they ever enter `visited` -- they must
        # neither appear in the result nor be expanded past in a later iteration.
        next_frontier = await non_retired_node_ids(db, candidate_next)
        if not next_frontier:
            break
        visited |= next_frontier
        if len(visited) > MAX_TRAVERSAL_NODES:
            raise GraphTraversalBounded(root_id=root_id, limit_kind="nodes")
        frontier = next_frontier

    visited.discard(root_id)
    return visited


@asynccontextmanager
async def with_connection_mutation_lock(db: AsyncSession) -> AsyncIterator[None]:
    """F-C1 correction (PHASE3_CORRECTION_DESIGN.md Parts 4/6). Every application code
    path that can insert an active `PowerConnection` row MUST run its
    lock-canonical-pair -> cycle-check -> insert -> commit sequence inside this context
    manager, as the *first statement of the route's own critical section* (before
    `lock_node_pair_in_canonical_order`, which is retained beneath this for its own
    same-pair deadlock-avoidance guarantee, now redundant for cross-pair correctness but
    harmless to keep). Documentation precision note (PHASE3_INDEPENDENT_VALIDATION_
    REPORT.md, item 10): this is deliberately not phrased as "the first statement of
    the transaction" -- on `create_power_connection`, FastAPI's own dependency chain
    (`require_permission` -> `get_auth_context`) already issues a read-only permission-
    lookup `SELECT` on the same `AsyncSession` before the route body runs, which is what
    actually auto-begins the underlying SQLAlchemy/Postgres transaction. This has no
    functional effect on the lock's guarantee (`pg_advisory_xact_lock`'s scope is tied to
    whichever transaction is current when it is called, regardless of when that
    transaction started, and the preceding read has no side effects to interact with)
    -- but the transaction's *first statement* and this lock's position as the first
    statement *of the code this docstring is actually describing* are two different
    claims, and only the latter is asserted here.

    `pg_advisory_xact_lock` is transaction-scoped: it is acquired for the remainder of
    the current transaction only and is released automatically on COMMIT or ROLLBACK --
    never requiring a manual unlock call, and never at risk of leaking across a pooled
    connection's reuse (session-scoped advisory locks would have exactly that risk,
    which is why this uses the `_xact_` variant, not plain `pg_advisory_lock`)."""
    start = time.monotonic()
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": POWER_TOPOLOGY_MUTATION_LOCK_KEY})
    wait_seconds = time.monotonic() - start
    if wait_seconds >= _NOTABLE_LOCK_WAIT_SECONDS:
        logger.info(
            "power_topology_mutation_lock_contended",
            wait_seconds=round(wait_seconds, 3), lock_key=POWER_TOPOLOGY_MUTATION_LOCK_KEY,
        )
    yield


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
