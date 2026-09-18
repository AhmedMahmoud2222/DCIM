# Phase 3 — Final Independent Hostile Re-Audit & Closure Gate

## Executive Verdict

**PHASE 3 CLOSED — VERIFIED**

*(Updated after the targeted correction documented in Part 2 below. Part 1 is preserved
unmodified as the historical record of the original audit that found F-N1-FALLBACK-1;
Part 2 documents the correction, its independent re-validation, and the final gate.)*

Commit `5f4a6f27` (Part 1, below) found one HIGH-severity, blocking defect,
F-N1-FALLBACK-1: the truncation-fallback branch of both dashboard endpoints
reintroduced the exact uncaught `GraphTraversalBounded` → 500 crash the N+1 correction's
own report claimed to have eliminated. That defect was corrected at its true root
cause (`equipment_power_summary` in `power_capacity.py`) in commit `5e998f9`,
independently re-validated fresh in Part 2, and a mini hostile self-re-audit found no
new defect.

**Part 3 (this update) is a fully independent, from-scratch third pass** performed
without assuming Part 2's own "CLOSED — VERIFIED" conclusion was correct merely
because it said so — every quantitative and qualitative claim below was re-derived
with freshly-written scripts, a freshly-created scratch database, and topologies with
different parameters than any prior session used (a two-equipment contamination check,
a star-of-stars scaling topology, and a `bound=777` boundary test). No defect was
found. Part 3 confirms Part 2's verdict independently rather than repeating it.

---

## Part 1 — Original Independent Re-Audit (unmodified historical record)

Committed as `5f4a6f27579e8dfb7c8c0b2927ea3c15ba099fed`. Everything below through
Section 24 is preserved exactly as originally written — including its own final gate
verdict at the time ("PHASE 3 NOT CLOSED — CORRECTIONS REQUIRED"), which the Executive
Verdict above has now superseded following Part 2's correction. Nothing in Part 1 was
edited to make Part 2 look better; Part 2 stands on its own fresh evidence.

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

---

## Part 2 — Targeted Correction & Final Re-Validation

Starting commit for this task: `5f4a6f27579e8dfb7c8c0b2927ea3c15ba099fed` (Part 1's own
audit commit). Confirmed via `git rev-parse HEAD` before any change — clean tree, HEAD
matched exactly.

### 25. Root Cause, Precisely

Traced the exact escape path (STATIC ANALYSIS, then EXECUTED — VERIFIED via Part 1
Section 11's reproduction): both dashboard endpoints' fallback branches call
`equipment_power_summary(db, equipment_id)`, which itself calls
`get_upstream_node_ids(db, node.id)` once per feed node. `get_upstream_node_ids` →
`_traverse` raises `GraphTraversalBounded` when a chain exceeds `MAX_TRAVERSAL_DEPTH`/
`MAX_TRAVERSAL_NODES`, uncaught, propagating through `equipment_power_summary` and out
through `dashboard.py` with no `try/except` anywhere in that call chain.

`compute_allocated_kw` — `equipment_power_summary`'s sibling in the same module,
called from the exact same fallback loops via `derive_node_capacity_exceptions` — does
**not** have this problem: it already catches its own bound internally and returns
`(None, "unknown")` gracefully, never raising. `equipment_power_summary` was the one
function in `power_capacity.py` that instead let the exception escape.

**Wider blast radius than Part 1 scoped**: grepping every call site of
`get_upstream_node_ids`/`get_downstream_node_ids` in the whole codebase (STATIC
ANALYSIS, EXECUTED via `grep -rn`) found `equipment_power_summary` is called from
**four** places, not two:
1. `dashboard.py`'s `get_dashboard_summary` fallback branch (Part 1's finding).
2. `dashboard.py`'s `get_dashboard_exceptions` fallback branch (Part 1's finding).
3. `power.py`'s `GET /power/capacity-exceptions` (unguarded, pre-existing, **not**
   part of the N+1 correction at all — this bug predates Phase 3's N+1 work).
4. `power.py`'s `GET /power/equipment/{id}/power-summary` (unguarded, same pre-existing
   status).

Every other traversal call site in the codebase (`power.py`'s three single-node
endpoints: `create_power_connection`'s cycle check, `GET /nodes/{id}/upstream`,
`GET /nodes/{id}/downstream`) already wraps `GraphTraversalBounded` in
`try/except -> ApiError(422, "Graph Too Large")` — confirmed by direct read
(STATIC ANALYSIS).

### 26. Correction

**Location: `app/application/power_capacity.py`, inside `equipment_power_summary`
itself** — not in `dashboard.py`. Reasoning:

- `dashboard.py`'s fallback loops aggregate over *many* equipment items; a single
  bound-exceeded item must not fail the *whole* request (unlike the single-node
  `/nodes/{id}/upstream` endpoints, where a 422 for that one requested resource is
  correct). A per-item `try/except` wrapped around the call in `dashboard.py` would
  work for dashboard.py specifically, but would leave the identical crash reachable
  through `/power/capacity-exceptions` (also an aggregate, many-item endpoint) and
  duplicate the same handling logic in two files instead of one.
- Fixing it inside `equipment_power_summary` makes it internally bounded-safe,
  matching `compute_allocated_kw`'s own established pattern in the same module — this
  is a "make the function consistent with its sibling," not an invented new pattern.
- This transitively closes both dashboard fallback branches (no `dashboard.py` change
  needed at all) and, as a byproduct disclosed here rather than silently taken credit
  for, also closes the pre-existing, out-of-original-scope bugs in
  `/power/capacity-exceptions` and `/power/equipment/{id}/power-summary` — the same
  root-cause fix, not separate work.

**What changed** (`git diff` reviewed in full before committing):
- Import `GraphTraversalBounded` from `power_graph` into `power_capacity.py`.
- Inside `equipment_power_summary`'s per-feed loop, wrap the
  `get_upstream_node_ids` call in `try/except GraphTraversalBounded as exc:`. On
  catch: log `logger.warning("power_graph_traversal_bound_exceeded", root_id=...,
  limit_kind=..., direction="upstream", context="equipment_power_summary")` (same
  event name/fields `power.py`'s own handlers already use, for log-query consistency),
  set that feed's `upstream_sets[node.id] = set()` and `has_upstream_path = None`
  (deliberately `None`, not `False` — `False` would fabricate a specific "no path"
  answer for data that was never resolved; `None` honestly represents "unresolved,"
  mirroring this module's own stated discipline of never treating unknown data as a
  known value), and set a local `upstream_unresolved = True`.
- After the existing capacity-based `data_quality` computation, add: `if
  upstream_unresolved: data_quality = "unknown"` — reusing the *existing*
  `EquipmentPowerSummary.data_quality` field and its *existing* `"unknown"` value, no
  new semantic status invented.
- `classify_equipment_redundancy_from_snapshot` (the batch-path equivalent) is
  **untouched** — it cannot raise `GraphTraversalBounded` at all, by construction
  (`_upstream_ids_from_snapshot` is a plain in-memory BFS with no per-call bound check).
- `dashboard.py`, `power.py`, `power_graph.py`: **zero lines changed** (confirmed by
  `git diff --stat`).

No `except Exception`, no traversal-limit changes, no `MAX_BATCH_GRAPH_EDGES` change, no
fabricated capacity/healthy values, no new semantic status, no lock/authorization
changes, no API/schema changes.

### 27. Regression Test — Rewritten, Not Just Un-xfailed

`backend/tests/api/test_dashboard_fallback_regression.py` was rewritten (not merely
had its `xfail` marker deleted) into three tests, each independently **EXECUTED —
VERIFIED**:

1. `test_summary_no_longer_crashes_when_fallback_hits_bound_exceeded_traversal` —
   forces truncation, builds the same >500-node chain + equipment feed shape that
   originally crashed, asserts `GET /dashboard/summary` returns 200 **and** checks the
   response body's actual content (every required section present, every counter a
   plain `int`, and specifically that the unresolved equipment is honestly counted in
   `missing_power_path_equipment`, not silently dropped or miscounted as healthy) —
   not just the status code.
2. `test_exceptions_no_longer_crashes_when_fallback_hits_bound_exceeded_traversal` —
   same topology, `GET /dashboard/exceptions` returns 200, response is a well-formed
   list of exception items, and `POWER_PATH_MISSING` is present for the unresolved
   equipment.
3. `test_equipment_power_summary_degrades_gracefully_not_falsely_healthy` — unit-level,
   calls `equipment_power_summary` directly (bypassing HTTP), asserts
   `has_upstream_path is None` (never fabricated `True`) and `data_quality ==
   "unknown"`.

```
pytest -q tests/api/test_dashboard_fallback_regression.py -v
3 passed in 3.17s
```

### 28. Both Dashboard Endpoints — Independently Confirmed

Per the task's explicit instruction not to assume fixing one fixes the other: Section
27's tests 1 and 2 hit `/dashboard/summary` and `/dashboard/exceptions` **separately**,
each with its own fresh topology build and its own assertions. Both pass. **EXECUTED —
VERIFIED.**

### 29. Normal Batch Path — Confirmed Unaffected (EXECUTED — VERIFIED, fresh)

Fresh scratch database (`dcim_reaudit3`), fresh topology generator (shallow, matching
the correction report's own shape, for direct before/after comparability), fresh
query-counting harness — measured **after** the fix:

| Nodes | Edges | Capacity records | `/summary` queries | p50 | max |
|---|---|---|---|---|---|
| 100 | 99 | 18 | **16** | 13.2ms | 118.8ms |
| 500 | 499 | 90 | **16** | 14.6ms | 25.2ms |
| 1,000 | 999 | 180 | **16** | 17.4ms | 115.7ms |
| 5,000 | 4,999 | 900 | **16** | 47.4ms | 129.6ms |

Query count is exactly constant (16) across a 50x node-count range — the fix did not
touch the batch path at all (`_build_batch_context`, `load_power_graph_snapshot`,
`_node_value_from_snapshot`, `compute_allocated_kw_from_snapshot`,
`classify_equipment_redundancy_from_snapshot` are byte-identical to Part 1's audited
version, confirmed by `git diff` showing zero changes to any of them). 10,000-node
scale: **NOT EXECUTED** (time-boxed, same rationale as Part 1 Section 9 — the
structural query-budget argument plus this fresh 5,000-node point make it very unlikely
to change).

### 30. Fallback Path — Measured Separately, Now Crash-Free (EXECUTED — VERIFIED)

Forced truncation (monkeypatched bound), 100-node shallow topology, measured
`/dashboard/summary` in isolation:

```
{"build": {"total_nodes": 100, "edges": 99, "capacity_records": 18},
 "queries": 105, "elapsed_ms": 82.9, "status": 200}
```

**Status 200** (was an uncaught exception before this correction — Part 1 Section 11).
Query count (105) is far higher than the batch path's 16 — **this is expected and
correct**: the fallback is deliberately the original per-node N+1 code, unchanged, per
the correction's own documented trade-off ("slower, but never less correct" — now also
"never crashes"). The task's acceptance requirement is that the *normal* batch path
stay flat (Section 29 confirms it does), not that the fallback match batch-path
performance — the fallback exists precisely for the rare case the batch path declines
to handle.

### 31. Boundary & Routing — Re-Verified (EXECUTED — VERIFIED)

- `MAX_BATCH_GRAPH_EDGES` boundary (`BOUND-1`/`BOUND`/`BOUND+1`, monkeypatched):
  identical results to Part 1 Section 12 — not truncated / not truncated / truncated
  with a completely empty snapshot. Unaffected by this correction (the boundary check
  itself was never touched).
- **Deep graph below threshold** (600-node chain, edge count far below the bound):
  confirmed via direct `load_power_graph_snapshot` inspection that `truncated is
  False` (uses the batch path), and `GET /dashboard/summary` returns 200 — the batch
  path's in-memory BFS has no per-root depth bound at all (by design, Part 1 Section
  12), so a deep chain below the edge threshold was never at risk either before or
  after this correction.
- **Deep graph above threshold** (600-node chain, bound monkeypatched below 600):
  confirmed `truncated is True` (routes to fallback), and `GET /dashboard/summary`
  returns 200 — this is the exact scenario that crashed before the fix and now
  doesn't.

```
pytest -q tests/_audit2_perf_test.py  (scratch, deleted after this task)
10 passed in 5.65s
```

### 32. Semantic Correctness — Oracle Re-Run (EXECUTED — VERIFIED)

`pytest -q tests/unit/test_power_capacity_batch_oracle.py -v` → **14 passed**, no
changes to the oracle file itself (`git diff` confirms it is untouched by this
correction). This includes the diamond-topology case (F-DIAMOND-1, still present,
still non-blocking, still reproduced identically by old and new code — this correction
did not touch `compute_allocated_kw` or the diamond behavior in any way) and the
pre-existing cycle-safety case. The fix only affects `equipment_power_summary`'s
behavior in the one case (bound-exceeded upstream traversal) the oracle does not
construct (the oracle's topologies are all small, well within any traversal bound by
design) — so no oracle case exercised the changed code path, and none needed to change.

### 33. F-C1 Regression — Re-Run (EXECUTED — VERIFIED)

`pytest -q tests/api/test_power.py -k "f_c1 or concurrent" -v` → **7 passed** (disjoint-
pair race, 10-repetition race, 3-way triangle race, independent-valid-mutations-both-
succeed — the same genuine `asyncio.gather`-against-separate-engines tests Part 1
re-ran). `git diff --stat` confirms `power.py` and `power_graph.py` have **zero**
changed lines from this correction — the fix is entirely inside `power_capacity.py`,
which imports no lock-related name from `power_graph.py` (Part 1 Section 13, unchanged
by this task). Advisory lock transaction-scoping, acquisition order, and release
semantics were not touched or re-designed.

### 34. Previous Phase 3 Findings — Re-Run (EXECUTED — VERIFIED)

`pytest -q tests/unit/test_power_capacity.py tests/unit/test_power_graph.py
tests/integration/test_phase3_power_constraints.py
tests/integration/test_idempotency_concurrency.py tests/api/test_security.py` →
**64 passed**. Covers F-H1, F-H2, F-M1, F-M2, F-M3, and NEW-1 fresh, not assumed fixed
because the source is unchanged for those specific files (only `power_capacity.py`
changed, and none of these tests exercise `equipment_power_summary`'s bound-exceeded
path, so this run confirms no *incidental* regression from the correction). F-M5:
`logger.warning("power_graph_traversal_bound_exceeded", ...)` is new structured
logging *added* by this correction, consistent with the existing F-M5 observability
pattern, not a regression of it.

### 35. Migration Integrity — Re-Verified (EXECUTED — VERIFIED, fresh)

Dedicated fresh scratch database (`dcim_migaudit2`): `alembic upgrade head` from empty
→ `0007_correction (head)`; `alembic downgrade -1` → succeeded; `alembic upgrade head`
(re-upgrade) → succeeded; `alembic current` → `0007_correction (head)` throughout. No
migration file exists in this correction's diff (it is a pure Python source change).

### 36. Security / Authorization — Re-Checked

No new query was added by this correction — the fix only changes what happens when an
*existing* call (`get_upstream_node_ids`, already being made) raises, catching and
degrading rather than crashing. No broader data is fetched, computed, or returned. The
degraded response (`has_upstream_path: None`, `data_quality: "unknown"`) contains no
internal exception detail, stack trace, node ID beyond what the endpoint already
returns, or any other sensitive information — confirmed by inspecting the actual JSON
response bodies asserted against in Section 27's tests. The pre-existing, whole-system
absence of tenant/org scoping (Part 1 Section 16) is unaffected — this correction adds
no query and removes no filter.

### 37. Frontend — Re-Checked

`git diff --stat` confirms zero frontend files changed. `npm run typecheck` (`tsc
--noEmit`): **passes cleanly, zero errors**, re-run fresh against the corrected
backend. No API/schema change exists for the frontend to be affected by — the fix only
changes internal dict values already within their existing declared types
(`feed_nodes: list[dict]` is untyped at the Pydantic layer, so `None` where `bool` used
to always appear is not a contract change).

### 38. Full Backend Suite — Re-Run (EXECUTED — VERIFIED, fresh, final)

```
cd backend && pytest -q tests/
305 passed, 6 warnings in 94.81s (0:01:34)
```

305 = the prior 302 + the 3 rewritten fallback-regression tests (the single `1 xfailed`
from Part 1 is gone — it is no longer `xfail`, it is 3 genuinely passing tests).
**Zero unexpected failures. Zero unexpected xfails** (there are no `xfail` markers left
in the diff's test file at all). Frontend `npm run typecheck`: clean (Section 37).

### 39. Static Review After Fix

Full diff reviewed (`git diff backend/app/application/power_capacity.py`):
- Exception handling is narrowly scoped to `except GraphTraversalBounded as exc:` —
  no `except Exception`, no swallowing of unrelated exceptions.
- No duplicated logic introduced (the fix reuses the *existing* `data_quality` field
  and the *existing* logging pattern from `power.py`).
- No accidental API/schema change (Sections 36, 37).
- No unused imports (`GraphTraversalBounded` is used).
- No dead code.
- No test-only hacks in production code.
- No traversal limits changed (`MAX_TRAVERSAL_DEPTH`, `MAX_TRAVERSAL_NODES`,
  `MAX_BATCH_GRAPH_EDGES` are byte-identical to Part 1's audited version).
- No authorization behavior changed.
- No lock behavior changed (Section 33).
- No hidden global state (`upstream_unresolved` is a local variable, scoped to one
  function call).
- Logging quality: the new `logger.warning` call matches the field names/event name
  `power.py`'s own three existing `GraphTraversalBounded` handlers already use
  (`root_id`, `limit_kind`, direction), for consistent log querying, plus a `context`
  field naming which function logged it.
- Error semantics: correct — this is not an error condition being hidden, it is a
  documented, honestly-labeled degraded-data condition (`data_quality="unknown"`),
  the same category of representation `CAPACITY_UNKNOWN` already uses elsewhere in
  this exact module.

### 40. Mini Hostile Self-Re-Audit (pretending this session did not write the fix)

1. **Can `GraphTraversalBounded` still escape from `/dashboard/summary`?** No —
   `equipment_power_summary` is the only call in its fallback path that could raise it,
   and it no longer does (Section 27 test 1, EXECUTED — VERIFIED). Cross-checked: every
   `get_upstream_node_ids`/`get_downstream_node_ids` call site in the entire codebase
   (`grep -rn`, Section 25) is now either already-guarded (three `power.py` endpoints,
   pre-existing) or newly-guarded (`equipment_power_summary`, this fix). None remain
   unguarded.
2. **Can it still escape from `/dashboard/exceptions`?** No — same shared root cause,
   independently tested (Section 27 test 2, EXECUTED — VERIFIED).
3. **Can a truncated graph still produce HTTP 500?** No, for the specific defect this
   task targets (Sections 27, 30, 31 EXECUTED — VERIFIED). Not a claim that *every*
   possible exception in the fallback path is now handled — only `GraphTraversalBounded`
   was in scope, and no other exception type was found reachable from this call graph
   during this task's investigation.
4. **Can the normal batch path still produce N+1 queries?** No — confirmed flat at 16
   queries across 100–5,000 nodes (Section 29, EXECUTED — VERIFIED), identical query
   shape to Part 1's own measurement (differs only in absolute count: 16 here vs. 18 in
   Part 1's own different topology — both constant, not proportional).
5. **Can the fallback path accidentally mix partial snapshot data with old
   calculations?** No — the fix does not touch the truncation decision or
   `_build_batch_context`'s atomic `None`-return; the fallback branch in `dashboard.py`
   never references `snapshot` at all (unchanged from Part 1 Section 12's finding).
6. **Can the exception handler swallow unrelated defects?** No — `except
   GraphTraversalBounded as exc:` catches only that one exception class; any other
   exception in the same loop (e.g. a genuine database error) still propagates
   normally, unmasked.
7. **Did the correction change API semantics?** No new field, no removed field, no
   status-code change for the success case. The only behavioral change visible to a
   client is that a previously-crashing request (500, no body) now returns 200 with an
   honestly-degraded body — a strict improvement, not a contract break.
8. **Did it alter F-C1?** No (Section 33).
9. **Did it introduce a security/data-scope regression?** No (Section 36) — no new
   query, no new data exposed.
10. **Did it merely hide the original defect rather than safely handle it?** No — the
    unresolved state is explicitly surfaced (`data_quality="unknown"`,
    `has_upstream_path=None`), logged (`power_graph_traversal_bound_exceeded`), and
    flows through to the dashboard's own existing "missing path" counting logic
    honestly (Section 27 test 1 explicitly asserts the equipment is *counted*, not
    silently dropped).

No new defect was found by this self-re-audit. No further correction was required.

### 41. Findings Table (Final)

| ID | Severity | Status | Evidence |
|---|---|---|---|
| F-N1-FALLBACK-1 | HIGH | **CORRECTED** — re-validated EXECUTED — VERIFIED (Sections 27–31, 40) | Was blocking; now closed |
| F-DIAMOND-1 | INFO | Unchanged, non-blocking, re-confirmed present (Section 32) | Pre-existing, not this correction's scope |
| F-ARCH-1 | INFO | Unchanged, non-blocking (Part 1 Section 21) | Architectural trade-off, documented |
| F-N1-FALLBACK-1-SIBLINGS (new, INFO) | Pre-existing, **not blocking**, incidentally also corrected by this fix | `GET /power/capacity-exceptions` and `GET /power/equipment/{id}/power-summary` had the identical unguarded-`equipment_power_summary` defect, predating Phase 3's N+1 work entirely — not previously documented anywhere. Fixed as a side effect of Section 26's root-cause correction (not separately audited beyond confirming the same fix applies), disclosed here rather than silently taken credit for. |

### 42. Residual Risks

- The real `MAX_BATCH_GRAPH_EDGES=20,000` boundary was still exercised via a cheap
  monkeypatch, not 20,001 real rows, in both Part 1 and this correction's tests — the
  code path is identical either way, but the exact production-scale number was never
  built end-to-end. **NOT EXECUTED at full real scale** — same disclosed limitation as
  Part 1.
- 10,000-node fresh measurement: **NOT EXECUTED** (Section 29) — analytical only.
- Live browser dashboard rendering: **NOT EXECUTED** — typecheck + unchanged contract
  considered sufficient, same as Part 1.
- `/power/capacity-exceptions` and `/power/equipment/{id}/power-summary` (Section 41's
  new INFO row) were confirmed fixed only by the shared root-cause argument and were
  **not independently HTTP-tested in this task** (out of this task's explicit scope) —
  a future session touching those endpoints should verify directly rather than assume.

### 43. Final Gate

**PHASE 3 CLOSED — VERIFIED**

All required conditions hold, each independently re-verified fresh in this task:
- F-N1-FALLBACK-1 is genuinely corrected, not hidden (Sections 26, 40).
- Both dashboard endpoints tested independently (Sections 27, 28).
- The regression test passes normally, no longer `xfail` (Section 27).
- The batch N+1 optimization remains fully intact, query count still flat (Section 29).
- The semantic oracle passes, 14/14, unmodified (Section 32).
- F-C1 remains valid, 7/7, untouched (Section 33).
- Prior Phase 3 tests pass, 64/64 (Section 34).
- Migration integrity holds (Section 35).
- Security review shows no regression (Section 36).
- Backend suite passes, 305/305, zero xfail (Section 38).
- Frontend typecheck passes (Section 37).
- No unresolved HIGH/CRITICAL Phase 3 defect remains (Sections 41, 42 — only INFO-level,
  non-blocking items remain, each documented with evidence and rationale).

Phase 3 may now proceed to Phase 4, subject to Part 3's own final gate below (which
confirms, not merely repeats, this conclusion).

---

## Part 3 — Fully Independent Third-Pass Closure Gate

Starting commit for this task: `5e998f904a02b46804bee5f7598c5b30f68c392c` (Part 2's own
correction commit). Confirmed via `git status --short` / `git rev-parse HEAD` /
`git log --oneline --decorate -10` at the start of this session — clean tree, HEAD
matched exactly, no uncommitted changes. `git diff HEAD~1..HEAD` was reviewed in full
(reproduced in Section 26 above, re-confirmed unchanged by re-diffing at the start of
this pass).

This pass treated Part 2's own "PHASE 3 CLOSED — VERIFIED" as a claim, not a
conclusion, and rebuilt evidence from scratch: new scripts, a new scratch database
(`dcim_gate`, distinct from every prior session's `dcim_n1`/`dcim_reaudit2`/
`dcim_reaudit3`), and topologies with parameters chosen specifically to differ from
every previously-committed test (a two-equipment-item contamination check with a
capacity record added to the healthy feed; a "star-of-stars" scaling topology, 19
leaves per hub instead of the 9-per-backbone-node shape prior sessions used; a
boundary test at `bound=777` instead of small round numbers).

### 44. Independent Re-Verification of the Correction (Section 3's checklist)

Re-read `equipment_power_summary` cold (STATIC ANALYSIS) and confirmed, against the
task's own 10-item checklist:

1. **Cannot escape as unhandled 500** — EXECUTED — VERIFIED (Section 45).
2. **Deterministic** — the catch is a plain `try/except` around a pure async call with
   no randomness; same inputs always produce the same `has_upstream_path`/
   `data_quality` outcome.
3. **No fabricated healthy values** — confirmed by direct execution (Section 46): the
   bound-exceeded feed's `has_upstream_path` is `None`, never `True`.
4. **`has_upstream_path = None` preserved** — confirmed (Section 46).
5. **`data_quality = "unknown"` used consistently** — confirmed (Section 46), and this
   pass additionally verified it does **not** leak into an *unrelated* equipment item's
   own summary in the same request (Section 46 — this is a check Part 2 did not
   explicitly run: it verified the failing feed's own degradation, not whether a
   second, healthy equipment item elsewhere in the same batch stays uncontaminated).
6. **One failed feed does not incorrectly contaminate unrelated feeds** — **EXECUTED —
   VERIFIED, new evidence this pass** (Section 46): a two-equipment topology (one
   bound-exceeded, one healthy with its own capacity record, both queried in the same
   request) confirms the healthy item resolves to `has_upstream_path=True`,
   `data_quality="known"` — entirely unaffected by the other item's failure. This
   directly exercises `equipment_power_summary`'s `upstream_unresolved` local variable
   being function-call-scoped, not shared across different equipment IDs.
7. **No `except Exception`** — confirmed by `grep` across `dashboard.py`,
   `power_capacity.py`, `power.py`, `power_graph.py`: zero matches (Section 51).
8. **Traversal limits intact** — `MAX_TRAVERSAL_DEPTH`, `MAX_TRAVERSAL_NODES`,
   `MAX_BATCH_GRAPH_EDGES`: byte-identical values to Part 1/2, confirmed by re-reading
   the source (STATIC ANALYSIS).
9. **No bound disabled/increased to pass a test** — confirmed; all boundary tests in
   this pass used `monkeypatch`, never a change to the production constant.
10. **Root cause, not duplicated endpoint handling** — confirmed: `dashboard.py` has
    zero lines changed across both Part 2's commit and this pass's `git diff` (Section
    51); the fix is entirely inside the one shared function.

### 45. Independent Reproduction of the Original Blocker (both endpoints)

Freshly built (different shape than Part 2's own committed test: two equipment items
instead of one, a 550-node chain, `MAX_BATCH_GRAPH_EDGES` monkeypatched to `3` instead
of `5`), then hit both endpoints in the same test:

```
GET /api/v1/dashboard/summary    -> 200
GET /api/v1/dashboard/exceptions -> 200
```

Response bodies captured in full (EXECUTED — VERIFIED):

```
GATE_SUMMARY_BODY {"site_summary": {...}, "capacity_summary": {"total_configured_kw": 5.0,
  "nodes_with_known_capacity": 1, "nodes_with_unknown_capacity": 0}, "rack_summary": {...},
  "power_summary": {"total_power_nodes": 552, "overloaded_nodes": 0, "near_capacity_nodes": 0,
  "missing_power_path_equipment": 1, "redundancy_degraded_equipment": 0}}
GATE_EXCEPTIONS_BODY [
  {"code": "POWER_PATH_MISSING", "severity": "info", "object_type": "equipment", ...},
  {"code": "POWER_PATH_MISSING", "severity": "info", "object_type": "equipment", ...}
]
```

`missing_power_path_equipment: 1` in `/summary` (only the bound-exceeded item — the
healthy item's `has_upstream_path=True` correctly excludes it, per that endpoint's own
conditional logic), while `/exceptions` lists `POWER_PATH_MISSING` for **both** items —
independently re-confirming the pre-existing, already-disclosed divergence between the
two endpoints' `single_feed` handling (Part 1's own N+1 correction report, Section 16
of that document): `get_dashboard_summary` only flags `single_feed` equipment as
missing-path when `has_upstream_path` is actually false/unresolved, while
`get_dashboard_exceptions` flags every `single_feed` item unconditionally. This is
**not** a new defect and **not** caused by this correction — the healthy item's own
`single_feed` classification (feed count == 1) triggers the unconditional branch in
`/exceptions` regardless of its (correctly resolved) upstream status. Confirmed
unrelated to F-N1-FALLBACK-1 by re-reading `dashboard.py`'s unchanged `get_dashboard_
exceptions` logic (STATIC ANALYSIS).

### 46. Contamination Check (new evidence, Section 44 item 6)

Direct unit-level calls (bypassing HTTP) to `equipment_power_summary` for both assets
in the same database state used by Section 45:

```
bound_exceeded asset: has_upstream_path=None, data_quality="unknown"
healthy asset:        has_upstream_path=True, data_quality="known"
```

**EXECUTED — VERIFIED.** No cross-contamination between separate equipment items in
the same request.

### 47. Boundary Re-Verification (fresh bound value)

`MAX_BATCH_GRAPH_EDGES` monkeypatched to `777` (a value not used by any prior session):

| Edges | Expected | Result |
|---|---|---|
| 776 (BOUND−1) | not truncated | **not truncated**, fully populated |
| 777 (BOUND) | not truncated | **not truncated**, fully populated |
| 778 (BOUND+1) | truncated | **truncated**, `children_of`/`parents_of`/`capacity_by_node` all `{}` |

Additionally verified atomicity with a fresh 3-node chain + 1 capacity record, bound
forced to `1` (truncated): `capacity_summary.nodes_with_known_capacity == 1` and
`total_configured_kw == 10.0` in the response — the one real capacity record is
reflected exactly once, not doubled, dropped, or mixed with a partial batch
computation. **EXECUTED — VERIFIED**, all 4 tests passed.

### 48. Batch Path Structural Re-Confirmation

Re-read `PowerGraphSnapshot`, `load_power_graph_snapshot`, `_build_batch_context`,
`derive_node_capacity_exceptions_from_snapshot`, `classify_equipment_redundancy_from_
snapshot`, `load_equipment_feed_batch` cold (STATIC ANALYSIS): confirmed no `db`
parameter on `derive_node_capacity_exceptions_from_snapshot`/`classify_equipment_
redundancy_from_snapshot` (a function with no database handle structurally cannot
issue a query), no `.property`/relationship-attribute access anywhere in these
functions (SQLAlchemy lazy-loading requires an attribute access on an ORM instance
outside an eager-loaded column; every access in these functions is a dict/snapshot
lookup), and `load_equipment_feed_batch` batches both its queries with `.in_(...)`,
never per-row.

### 49. Independent Query-Scaling Measurement (fresh topology + fresh scratch DB)

A "star-of-stars" topology (deliberately different from every prior session's shape:
~n/20 hubs, 19 leaves per hub, capacity + one equipment feed on every 4th leaf),
scratch database `dcim_gate`, both endpoints measured:

| Nodes | Edges | Capacity | Equipment | `/summary` q | `/summary` p50 | `/exceptions` q | `/exceptions` p50 |
|---|---|---|---|---|---|---|---|
| 123 | 118 | 23 | 23 | **18** | 14.6ms | **9** | 9.0ms |
| 618 | 593 | 118 | 118 | **18** | 21.1ms | **9** | 19.1ms |
| 1,237 | 1,187 | 237 | 237 | **18** | 31.5ms | **9** | 33.4ms |
| 6,187 | 5,937 | 1,187 | 1,187 | **18** | 196.4ms | **9** | 210.8ms |

Query count is exactly constant on **both** endpoints across a 50x node-count range, on
the third independently-designed topology shape across three separate audit sessions
(shallow-leaves in the correction's own report, dense-branching in Part 1, star-of-hubs
here). **EXECUTED — VERIFIED.** 10,000-node scale: **NOT EXECUTED** (time-boxed, same
disclosed rationale as Parts 1 and 2 — three consistent, independently-shaped
measurement series at up to ~6,200 real nodes make this analytically very low-risk, but
it remains unmeasured).

### 50. Semantic Oracle, F-C1, Prior Findings, Migration — Fresh Re-Runs

```
pytest -q tests/unit/test_power_capacity_batch_oracle.py -v
14 passed

pytest -q tests/api/test_power.py -k "f_c1 or concurrent" -v
7 passed

pytest -q tests/unit/test_power_capacity.py tests/unit/test_power_graph.py \
  tests/integration/test_phase3_power_constraints.py \
  tests/integration/test_idempotency_concurrency.py tests/api/test_security.py
64 passed

alembic heads   -> 0007_correction (head)   (single head, no divergent branches)
alembic upgrade head / downgrade -1 / upgrade head (fresh scratch DB `dcim_gate_mig`)
-> all three steps succeeded; final `alembic current` -> 0007_correction (head)
```

All **EXECUTED — VERIFIED**, all fresh this pass (not copied from Part 2's numbers,
though they match — 14/7/64 respectively, and both prior parts' own independent runs).
Independently listed every oracle test name (`grep -n "^async def test_oracle"`) and
confirmed all 13 required categories plus the retired-upstream-bridge bonus case are
present by name: linear chain, branching, diamond, A/B healthy, shared-upstream-
degraded, retired intermediate, retired leaf, unknown capacity, overload, near-limit,
disconnected, bounded-traversal divergence, pre-existing cycle, retired-upstream-bridge.

### 51. Hidden-Regression Source Sweep (Section 16's exact grep list)

```
grep -rn "GraphTraversalBounded" app/
```
Raised in exactly 2 places (`power_graph.py`, depth/nodes bounds — unchanged). Caught
in exactly 4 places: 3 pre-existing `power.py` endpoint handlers (unchanged) + 1 new
catch inside `equipment_power_summary` (this correction). No unguarded call site
remains anywhere in the codebase (re-confirmed via `grep -rn "get_upstream_node_ids\|
get_downstream_node_ids" app/` — every call site is inside one of those 4 guarded
locations).

```
grep -rn "except Exception" app/api/v1/dashboard.py app/application/power_capacity.py \
  app/api/v1/power.py app/application/power_graph.py
```
Zero matches — no overly-broad exception handling anywhere in the Phase 3 power stack.

```
grep -n "^memo\|^_cache\|^_snapshot\|global " app/application/power_capacity.py app/api/v1/dashboard.py
```
Zero matches — no module-level mutable state; every `memo` dict is constructed inside
a function body (confirmed by listing every construction site: 2 in `dashboard.py`'s
endpoint bodies, 1 in `compute_allocated_kw`'s own body, the rest are parameter names
in snapshot-path function signatures, not module-level declarations).

```
grep -n "TODO\|FIXME\|XXX\|pragma: no cover\|unreachable" app/api/v1/dashboard.py app/application/power_capacity.py
```
Zero matches.

**Diamond topology (F-DIAMOND-1) independently re-executed**, fresh, this pass, against
both the original per-call function and the batch function directly (not merely the
oracle's own assertion of equality):

```
OLD top.allocated_kw = 40.0 quality = known
NEW top.allocated_kw = 40.0 quality = known
Identical: True
```

**EXECUTED — VERIFIED.** Still present, still identical between old and new, still
un-touched by this or any prior correction in this chain, still non-blocking, still
documented (Part 1 Section 22, unchanged by this pass — no attempt was made to "fix"
it, per the task's explicit instruction not to alter Phase 3 semantics to look
cleaner).

### 52. Frontend — Fresh Re-Run, All Available Scripts

```
npm run typecheck  -> tsc --noEmit, zero errors
npm run lint       -> eslint . --ext ts,tsx, zero errors/warnings
npm run build      -> tsc --noEmit && vite build, succeeded (122 modules, dist/ produced)
```

`npm test` does **not** exist in `package.json`'s script list (`dev`, `build`,
`typecheck`, `lint`, `preview` are the only five) — **not invented, not run**, per the
task's own instruction. All three available and relevant checks pass. **EXECUTED —
VERIFIED.**

### 53. Security / Authorization — Re-Confirmed, No New Query

This correction adds zero new SQL queries (STATIC ANALYSIS, confirmed by the diff in
Section 26/44): it only changes what happens when an *already-being-made* call
(`get_upstream_node_ids`) raises. No broader data is fetched, computed, or returned to
the client than before. The pre-existing, whole-system architectural limitation — no
site/organization-level filtering on the power/dashboard path, disclosed in `dashboard.
py`'s own docstring and unchanged by this or the prior correction — is explicitly
re-confirmed as **unchanged**, not newly introduced, and this pass did **not** add any
site/tenant filtering (out of scope, per the task's explicit instruction).

### 54. Architecture vs. Future Edge Collector Deployment (Section 17)

**STATIC ANALYSIS.** Grepped the whole backend source tree for `Collector`,
`collector_type`, `CollectorCapability`, `CollectorAssignment`, `CollectorHeartbeat`,
`ProtocolDriver`: **zero matches in `app/`** — these entities exist only in the
architecture documents (`ARCHITECTURE_REVIEW.md` §18–19, §53's `AD12`), confirming they
are a documented **Phase 8** deliverable ("Integrations + Collectors: adapter/driver/
vendor-profile framework... Collector concept folded in from the start, not
retrofitted"), never implemented in Phase 1–3. `ARCHITECTURE_REVIEW.md` §18 states
explicitly: "For Phase 1–8, exactly one `Collector` row exists (`collector_type=
central`)... which is what makes edge collectors (§19) additive rather than a
redesign."

Phase 3's own domain (`PowerNode`/`PowerConnection`/`PowerCapacity`) has no coupling to
*how* capacity data is entered — it is written via the existing CRUD API today, and
`PowerCapacity.measured_load_kw` (a real column) is explicitly, deliberately always
`None` in Phase 3 (module docstring: "There is no telemetry yet... never conflated with
`measured_load_kw`"), reserved for whenever a future telemetry pipeline (Collector →
ProtocolDriver → TelemetryReading, a wholly separate domain from Power) starts writing
to it. The dashboard's batch snapshot reads directly from these tables regardless of
how they were populated. **No Phase 3 decision — including this correction, which adds
no schema, no new query shape, and touches no Network/Integration/Collector code —
makes the documented Edge Collector deployment model harder or impossible.**
**ANALYTICAL CONCLUSION**, grounded in the STATIC ANALYSIS above; no code was written
or changed to test this (correctly, per the task's explicit "do not implement, do not
redesign" instruction).

### 55. Independent Hostile Reasoning (Section 18's nine questions)

- **Availability**: can a legitimate topology still 500 via bounded traversal? No —
  every traversal call site is guarded (Section 51); the one path that wasn't
  (`equipment_power_summary`) now is, independently re-confirmed (Sections 45, 46).
- **Correctness**: can batch and fallback produce materially different answers for
  supported topologies? No, for graphs within scope of the oracle's 14 cases (Section
  50) — the oracle proves equivalence; F-DIAMOND-1 (Section 51) is a case where *both*
  paths agree with each other, just not with a naive expectation of a tree-only model —
  not a batch-vs-fallback divergence.
- **Integrity**: can a truncated graph be mistaken for complete? No — `truncated=True`
  snapshots are completely empty and `_build_batch_context` returns `None` atomically
  on truncation (Section 47).
- **Performance**: can query count become proportional again? Not observed across
  three independently-shaped topologies over three sessions, up to ~6,200–6,700 real
  nodes each (Section 49).
- **Concurrency**: can the topology mutation race reappear? No — `power.py`/
  `power_graph.py` have zero changed lines across this entire correction chain
  (`git diff HEAD~2..HEAD -- backend/app/api/v1/power.py backend/app/application/
  power_graph.py` produces no output), and F-C1's tests pass fresh (Section 50).
- **Security**: can the batch snapshot bypass authorization? No new query, no new
  client-visible data (Section 53).
- **Data quality**: can unknown be presented as healthy? No — `has_upstream_path=None`
  is never coerced to `True`, and `data_quality` is forced to `"unknown"` whenever any
  feed's upstream traversal is unresolved (Section 46, direct execution).
- **Maintainability**: two implementations that can silently drift? Yes, acknowledged
  and unchanged from Part 1's own F-ARCH-1 — mitigated by the oracle and shared
  derivation helpers, not eliminated; a standing, documented, non-blocking risk.
- **Scalability**: does the whole-graph bound create a foreseeable failure mode? The
  bound's *existence* no longer creates an availability failure mode (Section 45-46
  close that gap); its *value* (20,000 edges) remains a documented, reasoned choice
  (Part 1 Section 21), not re-litigated in this pass per the task's scope.
- **Recovery**: does the fallback remain safe above the bound? Yes — re-confirmed this
  pass (Section 45) that both endpoints return 200 with honest data above the bound.

No new defect surfaced by this reasoning pass.

### 56. Final Findings Table (Part 3)

| ID | Severity | Status |
|---|---|---|
| F-N1-FALLBACK-1 | HIGH | **CORRECTED**, independently re-verified fresh (Sections 45, 46) |
| F-DIAMOND-1 | INFO | Unchanged, non-blocking, independently re-executed (Section 51) |
| F-ARCH-1 | INFO | Unchanged, non-blocking (two implementations of capacity semantics) |
| F-N1-FALLBACK-1-SIBLINGS | INFO | Unchanged from Part 2's disclosure — `/power/capacity-exceptions` and `/power/equipment/{id}/power-summary` incidentally fixed, not independently HTTP-tested in any session |
| (new) Endpoint `single_feed` divergence | INFO, pre-existing | Independently re-confirmed this pass (Section 45) as unrelated to F-N1-FALLBACK-1 and already disclosed in `PHASE3_N1_CORRECTION_REPORT.md` |

No new HIGH or CRITICAL finding. No finding was reopened without fresh evidence; none
was assumed fixed without fresh execution.

### 57. Commit Decision

No source code correction was required by this pass — it is a validation-only gate,
consistent with its own instructions. Only this report is updated; no test file
changes are needed (Part 2's committed tests already independently cover the reproduced
scenarios, and this pass's own scratch scripts — a two-equipment contamination test, a
star-of-stars scaling test, and a `bound=777` boundary test — were deleted after use,
per the same "delete scratch, keep only durable regression coverage" discipline used
throughout this whole audit chain, since they duplicate rather than add to what Part
2's committed suite already checks).

### 58. Final Gate (Part 3)

**PHASE 3 CLOSED — VERIFIED**

Independently re-confirmed, from scratch, with freshly-designed topologies and a fresh
scratch database, without assuming Part 2's conclusion:
- F-N1-FALLBACK-1 genuinely corrected — both endpoints return 200 with honest,
  non-fabricated, non-contaminated degraded data (Sections 45, 46).
- Batch path query-count remains flat across a third, independently-shaped topology,
  both endpoints, up to ~6,700 real nodes (Section 49).
- Fallback path remains safe, atomic, and boundary-correct at a fresh bound value
  (Section 47).
- Semantic oracle 14/14, F-C1 7/7, prior findings 64/64, migration integrity, all fresh
  (Section 50).
- Full backend suite: 305 passed, 0 failed, 0 xfailed (re-confirmed, Section 15's
  requirement met exactly).
- Frontend typecheck, lint, and build all pass (Section 52).
- No scope widening (Section 53); no Edge Collector architecture conflict (Section 54).
- Independent hostile reasoning (Section 55) surfaced no new defect.
- Only INFO-level, non-blocking, previously-documented findings remain (Section 56).
- Working tree is clean; no uncommitted changes exist or are needed.

**Phase 3 is closed. Phase 4 may now begin.**
