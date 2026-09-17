# Phase 3 High-Finding Correction — Dashboard N+1 / Capacity Derivation Performance

Correction of the HIGH finding `PHASE3_INDEPENDENT_VALIDATION_REPORT.md` Section 9
raised: `GET /dashboard/summary` and `GET /dashboard/exceptions` issue one fresh,
independent set of queries per capacity-bearing power node and per equipment item,
producing query counts and latency that scale with node count instead of staying
roughly constant. Commit under correction: `58013ef75c68885ba67d26e8a9f13eb8322f0c89`.
No prior finding is re-opened here; F-C1/F-H1/F-H2/F-M1/F-M3 are untouched and their own
regression tests all still pass (Section 11).

## 1. Evidence Read Before Starting

Re-read (already authored earlier in this same engagement, so this was a confirmatory
pass, not a fresh read): `PHASE3_HOSTILE_SELF_AUDIT.md`, `PHASE3_CORRECTION_DESIGN.md`,
`PHASE3_CORRECTION_IMPLEMENTATION.md`, `PHASE3_CORRECTION_HOSTILE_REAUDIT.md` +
its scope companion, `PHASE3_INDEPENDENT_VALIDATION_REPORT.md` (Sections 9, 10, 17 in
particular — the N+1 measurement and its NOT-IMPLEMENTED minimal-fix design). Also
re-read the current, unmodified source of `app/api/v1/dashboard.py`,
`app/application/power_capacity.py`, and `app/application/power_graph.py` before writing
any new code, per the task's own "trace the root cause before implementing" requirement.

## 2. Fresh Baseline Reproduction (EXECUTED — VERIFIED, before any code change)

Methodology: app imported in-process via `httpx.ASGITransport`, exact query count via a
SQLAlchemy `before_cursor_execute` event listener on the same engine `db_session`/
`client` use, 5 repetitions per measurement point, against a dedicated scratch database
(`dcim_n1`, migrated fresh to `0007_correction` head), never `dcim_test`. Topology per
scale point: a 10%-of-n backbone chain with 9 leaves per backbone node, capacity records
on ~20% of leaves (matching the prior gate's "realistic shallow" shape), **plus** one
`equipment_power_input` feed node under each capacity-bearing leaf — a superset of the
prior gate's topology, deliberately added so this baseline exercises **both** dashboard
loops (the capacity-exception loop and the equipment-redundancy loop), not only the
first one.

| Nodes | Capacity records | Equipment nodes | `/summary` queries | `/summary` p50 | `/exceptions` queries | `/exceptions` p50 |
|---|---|---|---|---|---|---|
| 100 | 18 | 18 | 508 | 316.1ms | 516 | 330.4ms |
| 500 | 90 | 90 | 6,084 | 4,041.1ms | 6,164 | 3,767.5ms |
| 1,000 | 180 | 180 | 21,154 | 14,966.2ms | 21,324 | 12,938.9ms |
| 5,000 | 900 | 900 | **CRASH** | **500** | not reached | not reached |

**New finding beyond the prior gate's own measurement**: at n=5,000 the *current* (pre-
fix) `/dashboard/summary` does not merely get slow — it **crashes with an uncaught 500**.
Captured traceback:

```
File ".../app/api/v1/dashboard.py", line 209, in get_dashboard_summary
    total_configured += effective
File ".../app/application/power_capacity.py", line 385, in equipment_power_summary
    feed_nodes = ( ... )
File ".../app/application/power_graph.py", line 127, in get_upstream_node_ids
    return await _traverse(db, root_id, forward=False)
File ".../app/application/power_graph.py", line 153, in _traverse
    raise GraphTraversalBounded(root_id=root_id, limit_kind="depth")
app.application.power_graph.GraphTraversalBounded
```

Root cause (Section 3 below): the equipment loop's `get_upstream_node_ids` call is never
wrapped in a `try/except GraphTraversalBounded` in `dashboard.py` — a per-node bound
(`MAX_TRAVERSAL_DEPTH=500`) that is entirely reasonable for one node's own traversal
becomes an uncaught whole-request failure once the backbone chain (500 nodes at n=5,000
in this topology) reaches it. This is a real, reproducible defect on top of the already-
known N+1 pattern, not a synthetic worst case — this session's topology matches the
prior gate's own "realistic shallow" description and simply reached a larger scale.

Adversarial topology (10-node chain, capacity on **every** chain node plus some leaves,
matching `PHASE3_INDEPENDENT_VALIDATION_REPORT.md` Section 9.2's shape at the same
n=100): this session's reconstruction **completed** in 235.8ms / 264 queries, not the
prior gate's multi-minute stall. The prior report's own adversarial shape was described,
not fully specified (exact branching/nesting beyond "10-chain + 90 leaves, capacity on
chain + some leaves" was not recorded), so an exact byte-for-byte reproduction of that
specific stall was not achieved here — disclosed as a methodological gap, not claimed as
a refutation of the prior finding. The query-count/N+1 pattern this smaller adversarial
run does show (264 queries for 100 nodes / 28 capacity records, worse ratio than the
shallow topology's ~5:1) is consistent with the prior report's root-cause diagnosis
(overlapping-ancestor re-walks compounding when capacity sits on intermediate nodes).

## 3. Root-Cause Trace (documented before implementing, per the task's own requirement)

`dashboard.py`'s two endpoints each contain **two** independent per-item loops:

1. `for cap in capacity_rows: derive_node_capacity_exceptions(db, cap.power_node_id, ...)`.
   `derive_node_capacity_exceptions` → `get_capacity_figures` → `compute_allocated_kw`.
   `compute_allocated_kw` re-runs its own fresh 3-pass, per-call BFS discovery (2
   queries per level in pass 1, 2 more queries in pass 2) **every single call**, with
   zero sharing across different roots — even when two capacity-bearing nodes share
   most of their downstream subtree, that overlap is recomputed from scratch for each.
2. `for equipment_id in equipment_ids: equipment_power_summary(db, equipment_id)`.
   `equipment_power_summary` itself issues, per feed node: one `get_upstream_node_ids`
   traversal (its own fresh per-level queries), one `get_capacity_figures` call (which
   internally re-runs the same `compute_allocated_kw` pattern above), and one
   `feed_label` lookup query. `get_dashboard_exceptions` additionally issues one
   `db.get(PowerNode, ...)` per capacity row purely to fetch a label — a third,
   avoidable per-row query unique to that endpoint.

Neither loop shares any state across iterations. This is the textbook N+1 signature:
cost proportional to the number of capacity-bearing nodes / equipment items scanned,
with no batching and no plateau — confirmed by Section 2's measured, non-linear (worse
than linear once the equipment loop's traversal-length compounding is included) growth.

## 4. Design

Per `PHASE3_INDEPENDENT_VALIDATION_REPORT.md` Section 17's already-approved minimal
design (set-based batch loading, explicitly not a cache/materialized-view/
denormalization), implemented in `app/application/power_capacity.py` as an **additive**
layer alongside the original per-node functions (which remain the fallback and the only
path every non-dashboard caller, e.g. `GET /power/nodes/{id}/capacity`, still uses):

- **`PowerGraphSnapshot`**: `children_of`/`parents_of` (adjacency maps over the whole
  active graph, retirement-filtered exactly like `power_graph._traverse`/
  `compute_allocated_kw` filter their own per-level queries — see Section 6's oracle),
  `capacity_by_node`, `truncated`.
- **`load_power_graph_snapshot(db, capacity_rows=...)`**: 2 queries when the caller
  already has its own `PowerCapacity` rows (`dashboard.py` always does), 3 otherwise —
  one for every active `PowerConnection` edge (bounded by `MAX_BATCH_GRAPH_EDGES=20_000`,
  `.limit(BOUND+1)` so an over-bound graph is *detected*, not partially loaded), one for
  every currently-retired `PowerNode.id`.
- **`_node_value_from_snapshot`** / **`compute_allocated_kw_from_snapshot`**: iterative
  (explicit stack, no Python recursion), memoized over a `memo` dict the caller shares
  across *every* node scanned in one request — this sharing, not any change to the
  aggregation algorithm itself, is what removes the N+1 pattern. Split into two
  functions deliberately: `_node_value_from_snapshot` answers "what does this node
  contribute when it's somebody's child" (shortcuts on the node's own capacity, exactly
  like the original `node_value` closure), while `compute_allocated_kw_from_snapshot`
  answers "what is this node's own top-level allocated_kw" (never shortcuts on its own
  root's capacity — sums the root's direct children's contributions instead), mirroring
  the original function's own two-tier structure exactly. **A real bug was caught and
  fixed here during oracle validation** — see Section 6.
- **`_upstream_ids_from_snapshot`**: in-memory BFS over `parents_of`, equivalent to
  `get_upstream_node_ids`.
- **`classify_equipment_redundancy_from_snapshot`** / **`load_equipment_feed_batch`**:
  batch equivalents of `equipment_power_summary`'s own per-equipment feed/upstream/
  feed-label logic, computing the **full** `EquipmentPowerSummary` (including
  `effective_demand_kw`, even though the dashboard itself never reads that field) —
  chosen over a narrower "only what the dashboard needs" struct so the correctness
  oracle can compare the two paths field-for-field rather than a hand-picked subset,
  favoring provable parity over a marginally smaller implementation.
- **Shared helpers** (`_effective_capacity_of`, `_build_capacity_figures`,
  `_exceptions_from_figures`) were extracted, byte-for-byte, out of the *original*
  functions so both paths run the literal same threshold/precedence/unit logic — never
  two independently-maintained copies that could silently drift apart.
- **`dashboard.py`** rewires both endpoints to call `_build_batch_context` once per
  request; if `load_power_graph_snapshot` reports `truncated=True` (graph exceeds
  `MAX_BATCH_GRAPH_EDGES`), both endpoints fall back to the **original, unmodified**
  per-node loop for that whole request — never a partial/mixed result.

**Documented, deliberate semantic change** (named in advance by
`PHASE3_INDEPENDENT_VALIDATION_REPORT.md` Section 17, not discovered after the fact):
the old per-node bound (`MAX_TRAVERSAL_NODES=5,000`/`MAX_TRAVERSAL_DEPTH=500`, applied
fresh to every single node's own traversal) is replaced, on the batch path only, by one
whole-graph bound (`MAX_BATCH_GRAPH_EDGES=20,000`, applied once per dashboard request).
This is *why* Section 2's n=5,000 crash disappears on the batch path (Section 9) without
weakening any bound — 5,900 edges is comfortably under the new, deliberately generous
whole-graph bound, and the crash was never a "real" limit being hit correctly, it was an
uncaught exception from a bound that was never designed to be request-fatal.

## 5. Correctness Oracle (built and run BEFORE the batch path replaced anything)

`tests/unit/test_power_capacity_batch_oracle.py`, 14 cases (13 required + 1 added after
implementation, covering the highest-risk untested corner — see below), each building one
topology and asserting the snapshot-based batch functions produce results **identical**
to the original per-call functions for the same database state:

1. Linear chain 2. Branching 3. Diamond (multiple upstream sources) 4. A/B redundant
feeds, healthy 5. A/B feeds sharing an upstream ancestor (degraded) 6. Retired
intermediate node 7. Retired leaf (root-exemption) 8. No capacity record at all 9.
Capacity overload 10. Near-limit capacity 11. Disconnected node 12. Bounded-traversal
divergence (documented, not an error — see Section 4) 13. Pre-existing cycle via direct
ORM/SQL bypass (documented divergence: old recursion has no cycle guard and would
eventually raise `RecursionError`; new path returns promptly via an explicit `visiting`
set) 14. **Bonus**: equipment fed only through a retired upstream bridge (Utility →
Retired PDU → equipment feed) — added specifically because it is the one case that
exercises `_upstream_ids_from_snapshot`'s new retirement filtering that isn't already
covered by cases 6/7's downstream-only filtering.

**A real defect was caught by this oracle before anything used the batch path in
production**: the first implementation of `compute_allocated_kw_from_snapshot`
conflated "a node's own effective capacity" with "a node's allocated_kw," applying the
child-perspective capacity shortcut to the *root* itself — this is exactly backwards
from the original function's documented semantics (`allocated_kw` is always the sum of a
node's own *children*, regardless of whether the node itself has a capacity record).
Cases `test_oracle_branching`, `test_oracle_diamond_multiple_upstream_sources`,
`test_oracle_retired_leaf_own_traversal_still_visible`, `test_oracle_capacity_overload`,
`test_oracle_near_limit_capacity`, and `test_oracle_disconnected_node` all failed on the
first run, each showing the exact same divergence pattern (batch path reporting a root's
own capacity as its allocated figure). Fixed by splitting the single conflated function
into `_node_value_from_snapshot` (child-perspective, shortcuts) and
`compute_allocated_kw_from_snapshot` (root-perspective, never shortcuts) — see Section
4. All 14 cases pass after the fix (`13 passed` → after adding the bonus case,
`14 passed`). This is exactly what the oracle-before-implementation ordering was for.

## 6. Implementation

Files changed: `backend/app/application/power_capacity.py` (shared-helper extraction +
new batch functions, additive — every original function's behavior and signature is
unchanged), `backend/app/api/v1/dashboard.py` (both endpoints rewired to the batch path
with a truncation fallback to the original per-node loops), `backend/tests/api/
test_power.py` (new query-count regression-guard test), `backend/tests/unit/
test_power_capacity_batch_oracle.py` (new, 14 cases). No other file touched.

## 7. Query-Count Acceptance (EXECUTED — VERIFIED, after the fix)

Same scratch database, same topology generator, same methodology as Section 2:

| Nodes | `/summary` queries (before → after) | `/exceptions` queries (before → after) | `/summary` p50 (before → after) |
|---|---|---|---|
| 100 | 508 → **18** | 516 → **9** | 316.1ms → **18.2ms** |
| 500 | 6,084 → **18** | 6,164 → **9** | 4,041.1ms → **21.7ms** |
| 1,000 | 21,154 → **18** | 21,324 → **9** | 14,966.2ms → **31.1ms** |
| 5,000 | CRASH → **18** | not reached → **9** | 500 → **202.3ms** |

Query count is **exactly constant** (18 / 9) across every scale point — not merely
bounded, genuinely flat — because the batch path's cost is a small, fixed number of
whole-graph queries per request, independent of how many capacity-bearing nodes or
equipment items exist. The n=5,000 crash is gone: the batch path's whole-graph bound
(20,000 edges) comfortably covers this scale's 5,899 edges, so the request completes
normally instead of hitting the old per-node depth bound uncaught.

## 8. Adversarial Topology Re-Test (EXECUTED — VERIFIED)

Same adversarial topology as Section 2 (10-chain + 90 leaves, capacity on every chain
node): **before** 264 queries / 235.8ms, **after** 16 queries / 42.9ms — both complete
(this session's reconstruction of the adversarial shape did not reproduce the historical
multi-minute stall even before the fix, per Section 2's disclosed methodological gap;
the fix still measurably improves it on every metric captured either way).

## 9. Performance-Regression-Guard Test

`tests/api/test_power.py::test_dashboard_summary_query_count_does_not_grow_linearly_with_scale`
builds a 100-node capacity chain, measures `/dashboard/summary`'s query count, then adds
a 1,000-node chain and measures again — asserting the second count is nowhere near
proportional (`< 3x`, not `< 1.5x` or an exact number, to stay robust to unrelated future
query-count changes elsewhere in the endpoint) to the first, the same bounded-not-linear
property Section 7 measured directly. Passes: `1 passed in 2.34s`.

## 10. Full Regression (complete suite, not partial)

`pytest -q tests/` from `backend/`: **302 passed, 6 warnings, 100.76s** — the original
287 (all Power*/dashboard/auth/concurrency/migration/security tests, unmodified),
14 new oracle cases, 1 new performance-regression-guard test. Zero failures, zero new
warnings beyond the pre-existing Starlette deprecation notices already present before
this correction.

## 11. Migration Decision

**No migration created.** This correction is a pure query-layer/application-layer
change — no table, column, constraint, or index was added, altered, or removed.
`alembic current` remains `0007_correction (head)` throughout.

## 12. Frontend Regression

No frontend file was touched. Both endpoints' Pydantic response models
(`DashboardSummaryOut`, `CapacitySummaryOut`, `PowerSummaryOut`, `ExceptionItemOut`, and
all their nested fields) are byte-for-byte unchanged — confirmed by `git diff` showing
no changes to any `class ...Out` definition in `dashboard.py`. Combined with the oracle
proving the new code path computes identical field values to the old one (Section 5),
the JSON response shape and content the frontend (`frontend/src/features/power/api.ts`'s
`getDashboardSummary`/`getDashboardExceptions`) consumes is unaffected. **Live browser
verification was NOT executed** in this pass (disclosed, not silently skipped) — this
correction is scoped as read-path backend performance work per the task's own "no visual
redesign" framing, and the schema-invariance + oracle-equivalence argument above is
considered sufficient evidence without re-running the browser session from prior tasks.

## 13. Security / Authorization Regression

**Checked specifically for scope-widening, per the task's own flagged concern.**
`capacity_rows` (both endpoints) and the `equipment_ids` query were, before this
correction, **already** unfiltered by site/org/room (confirmed by re-reading the
pre-correction code — `_filtered_room_ids`'s `room_ids` scoping is applied only to the
unrelated site/rack-count section of `get_dashboard_summary`, never to the power/
capacity section, in both the old and new code). The batch snapshot loads the *whole*
active graph into memory as supporting data, which is broader raw data than any single
old per-node query touched — but **it is never returned to the client**: it is used only
to compute `allocated_kw`/`utilization_pct`/redundancy figures for the *same* set of
node/equipment IDs the old code already selected and returned. No new node, capacity
record, or equipment item enters either endpoint's response as a result of this change.
**No scope widening.** (The pre-existing absence of org/site scoping on this endpoint's
power/capacity section is itself unchanged by this correction — neither introduced nor
fixed here, consistent with the task's explicit "no new product functionality" scope.)

## 14. Concurrency Regression

`app/application/power_graph.py` (F-C1's `with_connection_mutation_lock`,
`POWER_TOPOLOGY_MUTATION_LOCK_KEY`, `lock_node_pair_in_canonical_order`) and
`app/api/v1/power.py` (`create_power_connection`) are **untouched** — confirmed by
`git diff --stat` showing no changes to either file. This correction is entirely
read-path (`GET /dashboard/*`); it never opens a write transaction, never acquires the
advisory lock, and cannot interact with it. All F-C1 regression tests in
`tests/api/test_power.py` still pass (Section 10).

## 15. Hostile Self-Review

- **Does the batch path ever return a value the old path wouldn't for a graph within
  both bounds?** No — proven by the 12 non-divergence oracle cases plus the dedicated
  retired-upstream-bridge case.
- **Does the shared `memo` dict ever leak state across separate HTTP requests?** No —
  `memo = {}` is created fresh inside each endpoint call, never module-level or cached.
- **Does the fallback path ever run only *half* the request in batch mode?** No —
  `_build_batch_context` returns `None` (triggering full fallback) or a complete
  4-tuple; there is no code path that uses the snapshot for one loop and the old
  functions for the other within the same request.
- **Does `children_of`/`parents_of`'s retirement filtering correctly implement the
  Model A root-exemption in both directions, not just downstream?** Yes — verified by
  the dedicated bonus oracle case (Section 5) exercising the upstream side specifically.
- **Could `MAX_BATCH_GRAPH_EDGES` silently produce a wrong (not just slow) answer if
  the graph is right at the boundary?** No — it uses `.limit(BOUND+1)` and checks
  `len(rows) > BOUND`, so it can only ever under-detect by being generous (never counts
  short), and on detection returns `truncated=True` (forcing full fallback), never a
  partially-loaded snapshot.
- **Does the new cycle-safety `visiting` set change any answer for the acyclic graphs
  every real topology actually has?** No — it only ever activates when a node is
  reentered while still on the current stack, which cannot happen in an acyclic graph;
  confirmed by all 13 non-cycle oracle cases passing unchanged.
- **Was the query-count regression guard tuned to the current implementation's exact
  numbers (fragile), or to the general property required (robust)?** The latter — it
  asserts `< 3x` growth for `10x` more nodes, not a specific count.
- **Does `get_dashboard_exceptions`'s extra per-row label lookup (unique to that
  endpoint, not present in `/summary`) get properly batched too?** Yes — one
  `IN (...)` query for all capacity rows' node labels, replacing the old per-row
  `db.get(PowerNode, ...)` call.
- **Is there any place the batch path performs a query the old path didn't, for the
  same request?** No new *query shapes* were added beyond what Section 4 lists; the
  fallback path is the literal original code, byte-identical.
- **Did fixing the root/child conflation bug (Section 5) require touching the original,
  already-audited `compute_allocated_kw`?** No — only the new batch functions were
  changed; the original function and its own extracted shared helpers are unmodified
  in behavior (confirmed by the unmodified `tests/unit/test_power_capacity.py` suite
  still passing unchanged).

## 16. Incidental Observations (disclosed, not separately fixed — out of this task's scope)

- The pre-existing inconsistency between `get_dashboard_summary` (counts `single_feed`
  equipment as "missing path" only if that lone feed also lacks an upstream path) and
  `get_dashboard_exceptions` (reports `POWER_PATH_MISSING` for *every* `single_feed`
  equipment unconditionally) is preserved bug-for-bug in the batch rewrite, exactly as
  the task's "identical semantic results to the current implementation" requirement
  demands — not fixed here.
- `tests/unit/test_power_capacity.py`'s existing F-H1 regression test monkeypatches
  `power_graph.MAX_TRAVERSAL_NODES`, which does not affect `power_capacity`'s own
  by-value-imported `MAX_TRAVERSAL_NODES` name — the test still passes today because
  the scenario it builds is naturally "unknown" with or without the bound actually
  firing. Noticed while building this correction's own bound-divergence oracle case
  (which patches `power_capacity.MAX_TRAVERSAL_NODES` directly instead). Not fixed —
  out of this task's scope, and the existing test's assertion is still correct.

## 17. Final Gate

- Root cause removed: **yes** (Section 3 identified, Section 4 removed it).
- Scaling demonstrably bounded (in fact constant): **yes** (Section 7).
- Both topologies (realistic-shallow and adversarial) pass and improve: **yes**
  (Sections 7-8).
- Semantics preserved (oracle equivalence across 14 cases, two documented and justified
  divergences only): **yes** (Section 5).
- Full regression passes: **yes** (Section 10, 302/302).
- No new CRITICAL/HIGH introduced: **yes** — the one behavior change (whole-graph bound
  replacing per-node bound) was pre-approved by name in
  `PHASE3_INDEPENDENT_VALIDATION_REPORT.md` Section 17 and only ever makes previously-
  uncaught-crash cases succeed, never the reverse.

**CORRECTION COMPLETE — READY FOR INDEPENDENT RE-AUDIT**

Phase 3 as a whole is NOT declared closed or approved by this document — this report
closes only the dashboard N+1 HIGH finding, per the task's own scope.
