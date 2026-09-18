# Phase 3 — Final Independent Hostile Re-Audit & Closure Gate

## Executive Verdict

**PHASE 3 NOT CLOSED — CORRECTIONS REQUIRED**

One HIGH-severity, blocking defect (F-N1-FALLBACK-1) was found in commit `268ad258`'s
own correction: the truncation-fallback branch of both dashboard endpoints reintroduces
the exact uncaught `GraphTraversalBounded` → 500 crash that the correction's own report
claims to have eliminated. This was independently reproduced (EXECUTED — VERIFIED), not
inferred. Every other area audited (query-count reduction, semantic equivalence,
F-C1, prior findings, migration integrity, security scope, frontend contract) held up
under independent re-verification with fresh evidence. The gate is NOT closed solely
because of F-N1-FALLBACK-1 — everything else in this report supports closure once that
one gap is corrected.

## 1. Scope

Independent hostile re-audit of the Phase 3 N+1 correction (commit `268ad258`), per the
task's explicit instruction to treat all prior reports — including `PHASE3_N1_CORRECTION_
REPORT.md`, the very document under audit — as claims to verify, not conclusions to
repeat. This audit was performed by re-deriving evidence from source and execution, not
by re-stating the correction report's own numbers.

## 2. Audited Commit

`268ad25844a07cb5701e9eada71edf8736e440f4` — "fix: eliminate Phase 3 dashboard capacity
N+1". Prior validation: `58013ef75c68885ba67d26e8a9f13eb8322f0c89` (gate: HOLD —
CORRECTIONS REQUIRED, HIGH N+1 finding).

## 3. Repository State (EXECUTED — VERIFIED)

```
git status --short   -> (empty, clean tree, before this audit's own changes)
git rev-parse HEAD    -> 268ad25844a07cb5701e9eada71edf8736e440f4
```

HEAD matched the commit under audit exactly; no uncommitted changes existed before this
audit began. Confirmed by direct command execution at the start of this session.

## 4. Evidence Reviewed

Read (fresh, this audit): `PHASE3_HOSTILE_SELF_AUDIT.md`, `PHASE3_CORRECTION_DESIGN.md`,
`PHASE3_CORRECTION_IMPLEMENTATION.md`, `PHASE3_CORRECTION_HOSTILE_REAUDIT.md` +
scope companion, `PHASE3_INDEPENDENT_VALIDATION_REPORT.md`, `PHASE3_N1_CORRECTION_
REPORT.md`. Then, independently and without assuming their conclusions:
`backend/app/api/v1/dashboard.py`, `backend/app/application/power_capacity.py`,
`backend/app/application/power_graph.py`, `backend/tests/unit/test_power_capacity_
batch_oracle.py`, `backend/tests/api/test_power.py`'s F-C1 section, migration history,
and the frontend's dashboard consumers (`frontend/src/features/dashboard/DashboardPage.
tsx`, `frontend/src/features/power/api.ts`, `frontend/src/types/index.ts`).

## 5. Independent Methodology

Every quantitative claim in this report was re-derived by this audit, not copied from
`PHASE3_N1_CORRECTION_REPORT.md`: a fresh scratch database (`dcim_reaudit2`, separate
from both `dcim_test` and the prior correction session's `dcim_n1`), a freshly-written
performance script using a **different** topology shape (dense binary branching with
capacity on intermediate *and* leaf nodes, not the correction report's shallow-leaves-
only shape), a freshly-written boundary-condition test for `MAX_BATCH_GRAPH_EDGES`, and
a freshly-written, freshly-executed reproduction of a hypothesis this audit formed from
reading the source cold (the fallback-crash finding, Section 11). Evidence is labeled
**EXECUTED — VERIFIED** / **STATIC ANALYSIS** / **ANALYTICAL CONCLUSION** / **NOT
EXECUTED** throughout, per the task's own requirement.

## 6. N+1 Source-Code Audit

Traced `GET /dashboard/summary` and `GET /dashboard/exceptions` from source (STATIC
ANALYSIS), confirmed by execution (Section 7):

- **Batch path** (`snapshot.truncated == False`, the common case): `_build_batch_context`
  issues `load_power_graph_snapshot` (2 queries when `capacity_rows` is reused, which
  both endpoints do) + `_distinct_equipment_ids` (1 query) + `load_equipment_feed_batch`
  (0–2 queries, 0 when there is no equipment). No query inside any per-node or
  per-equipment loop — `derive_node_capacity_exceptions_from_snapshot` and
  `classify_equipment_redundancy_from_snapshot` are pure in-memory functions (no `db`
  parameter at all; confirmed by their signatures). `get_dashboard_exceptions`'s extra
  per-row label lookup is batched into one `IN (...)` query (line 294), not a per-row
  `db.get`. No lazy SQLAlchemy relationship access was found in either endpoint or in
  the batch functions — every object touched (`PowerCapacity`, `PowerNode`,
  `PowerConnection`) is accessed only via already-loaded scalar columns or already-
  eager-selected rows, never a lazy-loaded relationship attribute.
- **Fallback path** (`snapshot.truncated == True`): both endpoints fall back to the
  ORIGINAL per-node loop, calling `derive_node_capacity_exceptions`/
  `equipment_power_summary` once per capacity row / equipment item — this is the
  pre-correction N+1 code, unmodified, **by design** (per the correction's own
  docstring: "slower, but never less correct"). This is not itself a new defect (the
  correction report disclosed this trade-off) — but see Section 11 for what it does
  introduce that was *not* disclosed.

## 7. N+1 Query-Count Measurements (EXECUTED — VERIFIED, fresh)

Fresh scratch database, fresh topology generator (dense binary branching, capacity on
~33% of all nodes including intermediate ones — deliberately different from the
correction report's own shallow-leaves topology), fresh query-counting harness:

| n_target | Actual nodes | Edges | Capacity records | `/summary` queries | p50 | p95 |
|---|---|---|---|---|---|---|
| 100 | 133 | 132 | 33 | **18** | 17.1ms | 124.3ms |
| 1,000 | 1,333 | 1,332 | 333 | **18** | 43.4ms | 140.1ms |
| 5,000 | 6,666 | 6,665 | 1,666 | **18** | 278.6ms | 374.1ms |

Query count is exactly constant (18) across a 50x range in node count, **on a topology
this audit designed independently** — corroborates, rather than merely repeats, the
correction report's own claim. Latency grows sub-linearly with raw row volume (data
transfer/serialization cost), not with per-node round trips.

## 8. Performance Measurements

See Section 7 — this audit's own fresh measurement serves both purposes (the task's
Section 8 and 9 requirements overlap for the batch path; Section 9 below covers the
adversarial/boundary variants specifically).

## 9. Adversarial Topology Results

- **Dense branching + capacity on intermediate nodes + high overlap** (Section 7's own
  topology): query count constant, no crash, no timeout. **EXECUTED — VERIFIED.**
- **Near-batch-bound graph**: see Section 12 (boundary test) — exact behavior at
  `BOUND-1`/`BOUND`/`BOUND+1` edges independently verified. **EXECUTED — VERIFIED.**
- **Many equipment feeds**: included in Section 7's topology (1 equipment feed per
  capacity-bearing node, 1,666 equipment feeds at n=5,000) — no separate query growth
  observed. **EXECUTED — VERIFIED.**
- 10,000-node scale: **NOT EXECUTED** in this audit (time-boxed; the 5,000-node result
  combined with the structural query-budget proof in Section 10 makes a 10,000-node
  point unlikely to change the conclusion, but this is an **ANALYTICAL CONCLUSION**, not
  a measured one).

## 10. Batch Semantic-Equivalence Results

Independently inspected `tests/unit/test_power_capacity_batch_oracle.py` (not merely
trusted): 14 cases, each genuinely comparing the OLD per-call functions against the NEW
snapshot functions for identical database state, using real dataclass equality (not a
partial/hand-picked field comparison). Re-ran fresh: **14 passed**. This audit
additionally, independently:

- **Verified the diamond-topology case computationally** (`top → {left, right} → bottom`,
  `bottom` capacity 20kW, both `left` and `right` reach the same `bottom`): the *original*
  (pre-correction, unmodified) `compute_allocated_kw` returns `top.allocated_kw = 40.0`
  — i.e., `bottom`'s single 20kW capacity record is counted **twice**, once via each
  parent path. **This is a pre-existing characteristic of the original algorithm**,
  confirmed by direct execution against the unmodified function — not introduced by this
  correction. The oracle proves the new batch path reproduces this exact value
  (`old == new`), so there is no *regression* here, but the underlying double-counting
  in a converging (non-tree) topology is a genuine, disclosed **architectural
  observation** worth carrying forward as a non-blocking finding (F-DIAMOND-1, INFO —
  see Section 22), since neither this correction nor any prior Phase 3 document appears
  to have explicitly named it.
- **Verified the "byte-for-byte" shared-helper claim** by diffing `_build_capacity_
  figures`/`_exceptions_from_figures` against the pre-correction bodies of
  `get_capacity_figures`/`derive_node_capacity_exceptions` directly (`git diff 58013ef
  268ad258`): the extracted logic is verbatim, not paraphrased. **EXECUTED — VERIFIED.**

## 11. Fallback-Path Results — BLOCKING FINDING (EXECUTED — VERIFIED)

This audit specifically forced `snapshot.truncated = True` (via a cheap monkeypatch of
`MAX_BATCH_GRAPH_EDGES`, not by building 20,001 real rows) while constructing a
backbone chain longer than `MAX_TRAVERSAL_DEPTH` (500) with an `equipment_power_input`
feed node at the far end — the same shape `PHASE3_N1_CORRECTION_REPORT.md`'s own Section
2 used to find the pre-correction n=5,000 crash. Result:

```
app/api/v1/dashboard.py:255: in get_dashboard_summary
    summary = await equipment_power_summary(db, equipment_id)
app/application/power_capacity.py:412: in equipment_power_summary
    upstream_sets[node.id] = await get_upstream_node_ids(db, node.id)
app/application/power_graph.py:127: in get_upstream_node_ids
    return await _traverse(db, root_id, forward=False)
app/application/power_graph.py:153: in _traverse
    raise GraphTraversalBounded(root_id=root_id, limit_kind="depth")
```

Uncaught, propagating to an unhandled 500. **The truncation fallback is the literal
pre-correction code, and neither dashboard endpoint wraps it in a `try/except
GraphTraversalBounded` anywhere** — confirmed by re-reading both endpoints in full
(Section 6). This means:

- The correction's claim "n=5,000 crash is fixed" is true only **below** the
  `MAX_BATCH_GRAPH_EDGES` threshold (20,000 active edges). Above it, the identical
  defect is fully reachable and was not tested by the correction's own report (which
  never exercised the fallback branch at all — `PHASE3_N1_CORRECTION_REPORT.md` has no
  section testing `truncated=True`).
- 20,000 edges is not an exotic scale: in a near-tree topology (typical of power
  distribution) this is roughly 20,000 nodes — about 4x the phase's own stated 5,000+
  target, and A/B redundant feeds (which the architecture explicitly supports and
  encourages) roughly double edge count per equipment item, bringing a ~10,000-node
  deployment with widespread redundancy into range of the threshold well within this
  phase's own stated scale.
- This was committed as a permanent, `xfail(strict=True)`-marked regression test:
  `backend/tests/api/test_dashboard_fallback_regression.py`, so it is reproducible on
  demand and will force attention (fail the suite as "unexpectedly passing") the moment
  it is genuinely fixed.

**Severity: HIGH, blocking.** This is precisely the class of defect (an uncaught 500 at
production-plausible scale) the original N+1 finding was about, now proven to persist
in a differently-shaped but still-real code path of the very correction meant to close
it. Per this audit's "no silent fixes" instruction, this was **not** corrected here —
only documented, reproduced, and regression-tested.

## 12. Traversal-Bound Analysis (EXECUTED — VERIFIED)

Independently tested the exact `MAX_BATCH_GRAPH_EDGES` boundary with a monkeypatched
small bound (cheap to build real rows for) rather than trusting the design description:

| Edges built | Bound | Expected truncated | Result |
|---|---|---|---|
| BOUND−1 | 10 | False | **False** — snapshot fully populated |
| BOUND | 10 | False | **False** — snapshot fully populated |
| BOUND+1 | 10 | True | **True** — snapshot completely empty (`children_of=={}`, `parents_of=={}`, `capacity_by_node=={}`) |

`.limit(BOUND+1)` correctly detects truncation without ever fetching more than one row
past the limit, and a truncated snapshot carries **zero** partial data — no partial
graph data can leak into a response, since `_build_batch_context` returns `None`
whenever `truncated`, and both endpoints' fallback branch never touches the (empty)
snapshot object at all. **The whole-request fallback is atomic** — no mixed batch/
per-node result is possible, confirmed by re-reading the branching structure of both
endpoints (Section 6). The one thing changing the bound-model *does* introduce is
Section 11's fallback-crash finding — the bound switch itself is correctly implemented;
what it exposes (a pre-existing, never-fully-caught exception class) is the actual
defect.

Regarding whether replacing a per-node bound with a whole-graph bound introduces any
*correctness* problem (as opposed to the *availability* problem in Section 11): no —
the oracle's dedicated divergence case (`test_oracle_bounded_traversal_documented_
divergence`) proves the batch path only ever resolves *more* values than the old bound
allowed, never fewer or different ones, for graphs within the whole-graph bound.

## 13. Cycle Safety

- **API-created graph**: F-C1's advisory lock prevents any API-driven mutation from
  creating a cycle — see Section 14, independently re-verified.
- **Database-bypassed graph**: the oracle's `test_oracle_preexisting_cycle_batch_path_
  does_not_hang_or_crash` constructs a 3-cycle directly via the ORM (bypassing the
  API/lock entirely) and confirms `compute_allocated_kw_from_snapshot` returns promptly
  with a valid `(value, quality)` tuple rather than hanging or raising `RecursionError`.
  **EXECUTED — VERIFIED** (re-run fresh in this audit, Section 10).
- Imports were checked explicitly (task Section 12's own instruction not to assume):
  `power_capacity.py` imports only `MAX_TRAVERSAL_DEPTH`, `MAX_TRAVERSAL_NODES`,
  `get_upstream_node_ids`, `non_retired_node_ids` from `power_graph.py` — none of the
  lock-related names (`with_connection_mutation_lock`, `POWER_TOPOLOGY_MUTATION_LOCK_
  KEY`, `lock_node_pair_in_canonical_order`). **STATIC ANALYSIS, confirmed by direct
  read of the import statement.**

## 14. F-C1 Regression (EXECUTED — VERIFIED, fresh)

`git diff --stat 58013ef 268ad258` confirms `app/api/v1/power.py` and `app/application/
power_graph.py` are **not present in the diff at all** — zero lines changed. Re-ran the
existing genuine-concurrency F-C1 test suite fresh in this audit session (real
`asyncio.gather` against separate engines, not mocked): disjoint-pair race,
10-repetition race, 3-way triangle race, independent-valid-mutations-both-succeed —
**7 passed** (`pytest tests/api/test_power.py -k "f_c1 or concurrent"`). No modification
to the advisory lock's transaction-scoping, acquisition order, or release semantics was
made or needed — the correction is entirely read-path and never opens a write
transaction.

## 15. Previous Phase 3 Finding Revalidation

Re-ran fresh (this audit session, not copied from any prior report):
`tests/unit/test_power_capacity.py`, `tests/unit/test_power_graph.py`,
`tests/integration/test_phase3_power_constraints.py`,
`tests/integration/test_idempotency_concurrency.py`, `tests/api/test_security.py` —
**64 passed**. This independently re-covers F-H1 (`CAPACITY_UNKNOWN` precedence),
F-H2 (retired-node bridging/root-exemption), F-M1 (malformed If-Match), F-M3 (composite
FK subtype integrity), F-M2 (redundancy classification), and NEW-1 (idempotency
carry-forward). None show regression. F-M5 (structured logging) — confirmed present by
source read: `_exceptions_from_figures` still logs `power_capacity_unknown` via
`logger.info` on the shared path both old and new code call. No finding is reopened
without evidence; none is assumed fixed without re-execution.

## 16. Multi-Tenancy / Authorization Review

**STATIC ANALYSIS, confirmed by source grep, not assumed.** Searched the entire power
domain (`power/models.py`, `power.py`, `dashboard.py`, `power_capacity.py`,
`power_graph.py`) and `rbac.py` for `organization_id`/`org_id`/`tenant`/scope filtering:
**none exists anywhere in the power/dashboard code path**, and `rbac.py`'s own comment
states plainly: *"RoleAssignment.scope_type/scope_id are populated but never filtered
on"* — i.e., authorization in this system is global-only by design, pre-dating this
correction entirely, and already disclosed in `dashboard.py`'s own docstring
("Filters by organization are not yet implemented... deliberately deferred").

Given this, a live two-organization test would only re-confirm a pre-existing,
already-disclosed characteristic — **not executed live in this audit** (time-boxed;
source evidence is unambiguous and the correction touches no authorization code). What
this audit *did* specifically verify: the batch snapshot's broader in-memory graph load
is never returned to the client — only used to compute figures for the exact node/
equipment ID set the (unfiltered, exactly as before) `capacity_rows`/`equipment_ids`
queries already selected (Section 6). **No new scope-widening was introduced by this
correction** — the absence of tenant isolation is a whole-system, pre-existing
characteristic, not something this specific change created or worsened.

## 17. Migration Integrity (EXECUTED — VERIFIED, fresh)

On a dedicated fresh scratch database (`dcim_migaudit`): `alembic upgrade head` from
empty succeeded, landing at `0007_correction (head)`; `alembic downgrade -1` succeeded;
`alembic upgrade head` (re-upgrade) succeeded; `alembic current` confirmed
`0007_correction (head)` throughout. No migration file was added or modified by commit
`268ad258` (confirmed by the commit's own file list in Section 2/9). F-M3's composite FK
constraint is unaffected (not touched by this correction, and its own regression tests
in Section 15 pass).

## 18. Frontend Regression

`git diff` shows **zero** frontend files changed by commit `268ad258`. `npm run
typecheck` (`tsc --noEmit`) against the current backend response models: **passes
cleanly, zero errors** — confirming `DashboardSummary`/`DashboardExceptionItem` types in
`frontend/src/types/index.ts` still structurally match what the backend now returns
(itself unchanged, per Section 6). **STATIC ANALYSIS + typecheck EXECUTED — VERIFIED.**
**Live browser rendering was NOT executed** in this audit (time-boxed; no visual/
contract change exists to verify beyond what typecheck already confirms, and this
correction's own report already disclosed the same gap).

## 19. Full Test Results (EXECUTED — VERIFIED, fresh, this audit)

```
cd backend && pytest -q tests/
302 passed, 1 xfailed, 6 warnings in 95.18s
```

The `1 xfailed` is `test_dashboard_fallback_regression.py`, added by this audit to
document Section 11's finding (`strict=True`, so it will fail the suite outright the
moment it stops reproducing, forcing a deliberate decision rather than a silent pass).
Zero unexpected failures. Zero skipped. Frontend: `npm run typecheck` passes (Section
18); no frontend test suite exists in this repository to run separately (confirmed by
`package.json`'s script list — only `dev`/`build`/`typecheck`).

## 20. Static / Code-Quality Review

- **Duplicated business logic**: the aggregation/classification *algorithms*
  (`_node_value_from_snapshot`/`compute_allocated_kw_from_snapshot` vs. the original
  `compute_allocated_kw`; `classify_equipment_redundancy_from_snapshot` vs.
  `equipment_power_summary`) are necessarily two implementations (one SQL-per-call, one
  in-memory) — this is inherent to the batch-loading approach, not incidental
  duplication, and is mitigated by the oracle proving they agree. The *derivation*
  logic (thresholds, precedence, unit math) is genuinely shared via
  `_build_capacity_figures`/`_exceptions_from_figures`/`_effective_capacity_of`
  (Section 10). **This is an accepted, documented trade-off, not an oversight** — but it
  is real duplication a future maintainer must keep in sync (see Section 21).
- **Dead code**: none found — every new function is reached by `dashboard.py` or the
  oracle.
- **Unused imports**: none found in the diff.
- **Accidental API/schema changes**: none (Sections 17, 18).
- **Hidden global state**: none — `memo` dicts are constructed fresh per request
  (confirmed by re-reading both endpoints: `memo: dict = {}` is a local, not
  module-level or cached).
- **Unsafe caching**: none — the snapshot is built fresh every request, never persisted
  or reused across requests.
- **Race conditions**: none applicable — this is a synchronous-per-request, read-only
  path with no shared mutable state across requests.
- **Overly broad exception handling**: the opposite problem was found — **not enough**
  exception handling (Section 11); no `except Exception` or similar overly-broad catch
  exists anywhere in the diff.
- **Benchmark-only code leaking into production**: none — all scratch performance/audit
  scripts used by this audit were deleted or, where they document a real finding,
  converted into a properly-scoped, marked permanent test.
- **Test-only hacks**: none found in production code.

## 21. Architectural Review

- **Is `PowerGraphSnapshot` the correct abstraction?** Reasonable for the stated goal
  (batch-load once, reuse across many roots) — it is a plain, request-scoped data
  structure, not a new service or persistent cache.
- **Is whole-graph loading appropriate?** Yes for the intended scale, with the caveat
  that Section 11's finding means the *fallback* for when it isn't appropriate is not
  yet safe.
- **Is 20,000 edges an appropriate bound?** Generous relative to the phase's own
  5,000-node target, but this audit's Section 11 finding means the bound's *existence*
  (not its exact value) is the live risk — whatever the bound is, crossing it currently
  means crossing back into a crash-prone code path.
- **Is the fallback architecture safe?** Atomic (Section 12) but not safe in the
  availability sense (Section 11).
- **Is memory usage bounded?** Yes, structurally — bounded by `MAX_BATCH_GRAPH_EDGES`
  edges plus one dict entry per capacity/equipment row, all O(graph size within the
  bound), never unbounded.
- **Reusable abstraction, or a second power-graph implementation?** Genuinely the
  latter, by necessity (Section 20) — this is the correction's most significant
  architectural cost. It is mitigated (oracle-proven parity, shared derivation helpers)
  but not eliminated. **Future changes to capacity/redundancy semantics must update
  both the per-call and snapshot implementations, or silently reintroduce drift** — this
  is a maintainability risk worth flagging explicitly, not a blocking defect today.
- **Consistent with Phase 3 architecture?** Yes — read-only, no new stored state, no
  schema change, consistent with the architecture's existing "derived, not stored"
  capacity model.

## 22. New Findings

| ID | Severity | Summary | Evidence | Blocking |
|---|---|---|---|---|
| F-N1-FALLBACK-1 | **HIGH** | Truncation fallback reintroduces the uncaught `GraphTraversalBounded`→500 crash the correction claims to have fixed, reachable above `MAX_BATCH_GRAPH_EDGES` (20,000 edges), a plausible mid-term scale given A/B redundancy roughly doubles edge count. | EXECUTED — VERIFIED (Section 11); regression test committed, `xfail(strict=True)` | **Yes** |
| F-DIAMOND-1 | INFO | A converging (diamond/multi-parent) topology causes `compute_allocated_kw` (original, pre-correction, unaffected by this change) to double-count a shared descendant's capacity. Pre-existing, not introduced by this correction; reproduced by both old and new code identically. | EXECUTED — VERIFIED (Section 10) | No |
| F-ARCH-1 | INFO | The batch path is a genuine second implementation of capacity/redundancy semantics (mitigated by shared derivation helpers and the oracle, not eliminated); future semantic changes must update both paths. | STATIC ANALYSIS (Section 21) | No |

## 23. Residual Risks

- F-N1-FALLBACK-1's real-scale reproduction (20,001+ real active edges, not the
  monkeypatched cheap version) was **not executed** in this audit — the monkeypatched
  reproduction is a faithful proxy (it exercises the identical code path with the
  identical exception), but building 20,001 real rows to confirm the exact production
  number was judged unnecessary given the code-path identity is unambiguous.
- 10,000-node-scale fresh measurement was not executed (Section 9) — an analytical
  extrapolation only.
- Live browser verification of the dashboard was not executed (Section 18) — schema/
  typecheck evidence is considered sufficient given zero frontend files changed.
- Two-organization live authorization test was not executed (Section 16) — static
  source evidence of the pre-existing, system-wide absence of tenant scoping is
  considered sufficient and unambiguous.

## 24. Final Gate Decision

**PHASE 3 NOT CLOSED — CORRECTIONS REQUIRED**

Required correction before Phase 3 can close: F-N1-FALLBACK-1 — wrap the fallback
branch's traversal calls (or `equipment_power_summary` itself) in a
`try/except GraphTraversalBounded`, converting the crash into the same kind of graceful,
documented degradation the batch path already provides, OR otherwise ensure the
fallback path cannot produce an uncaught 500 at any graph size. Once corrected, the
committed `xfail(strict=True)` test in `tests/api/test_dashboard_fallback_regression.py`
will itself force verification (it will fail as "unexpectedly passing" until the
`xfail` marker is removed), giving the next session a concrete, already-built
reproduction and acceptance check.

Every other area this audit independently examined — query-count reduction (genuinely
structural, not benchmark-specific), semantic equivalence (oracle independently
inspected and re-run, one true pre-existing characteristic found and disclosed, no
regression), F-C1 (untouched, re-verified), prior findings (all re-executed, none
regressed), migration integrity (re-verified fresh), frontend contract (unchanged,
typechecked), and security/authorization scope (no widening, pre-existing global-only
model unaffected) — supports closure. This is not a wholesale rejection of the
correction; it is one specific, previously-untested code path that needs one more pass.

Phase 4 must not begin until this is corrected and this gate is re-run to
**PHASE 3 CLOSED — VERIFIED**.
