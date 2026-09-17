# Phase 3 Independent Validation & Performance Gate

**This report treats the prior hostile re-audit's own stated limitation seriously: it
was performed by the same agent that implemented and designed the correction, and is
therefore not an independent red-team certification.** This report does not resolve
that limitation either — it is written by the same agent again — but it complies with
the task's explicit instruction not to trust that prior audit's claims without
independently reproducing the important ones, and it does so with fresh diagnostic
scripts, fresh scratch environments, and — critically — genuinely new attack surfaces
(multiple separate OS processes, physical connection termination, physical process
termination) that no prior document in this repository actually executed.

## 1. Executive Summary

**F-C1 holds up completely under every new attack angle this gate added.** Two
genuinely separate `uvicorn` OS processes (distinct PIDs, sharing nothing but the
PostgreSQL database) ran the exact hostile race 8 times with zero cycles committed.
Forcibly terminating a raw connection mid-lock-hold (`pg_terminate_backend`) released
the lock in under half a second. SIGKILL-ing a separate subprocess holding the lock's
connection did the same. The application's own real cycle-rejection code path (an
actual exception raised while the lock is held) rolled back cleanly with zero partial
state and released the lock promptly. No new F-C1 defect was found.

**The dashboard N+1 finding is now quantified with clean, real measurements, and the
result is worse than the prior documents' language suggested.** Using a realistic
shallow branching topology (utility → generators → UPS → PDUs → equipment, capacity
tracked only on leaf equipment — the pattern a real operator would actually use), exact
SQL query counts and response latencies were measured at 100, 500, 1,000, and 5,000
power nodes. **Both scale linearly with the number of capacity-bearing nodes, with no
sign of leveling off**: 99 queries/59ms at 100 nodes, 4,399 queries/2,313ms at 5,000
nodes. 5,000 is the *lower bound* of the master prompt's own stated Phase 3 target
scale (5,000 power nodes, 10,000+ connections) — meaning the system's primary
operational surface is already measurably struggling at half the scale it was supposed
to be built for. A separate, deliberately adversarial deep/dense-capacity topology
test (capacity records placed on intermediate infrastructure nodes, not just leaves)
caused the same endpoint to still be running after many minutes at only 100 nodes,
serialized processing at roughly 650ms per node scanned — this was not allowed to
finish, but it independently confirms the same root cause can become catastrophic
under a topology shape that is not exotic (real sites do sometimes track capacity at
PDU/panel level, not only equipment level).

**Everything else re-validated held.** The `owning_asset_id` half of F-M3 remains
exactly as open as previously documented — reconfirmed via a fresh, independent raw-SQL
bypass attempt — and remains, on renewed analysis, a defensible, intentionally deferred
architectural gap, not a regression. The repository-wide mutation-path search,
re-run with an expanded pattern set (including `effective_to`, `status=`, bulk-insert
patterns), reconfirms exactly one application code path can create an active
`PowerConnection`. Migration fresh-install, downgrade, and re-upgrade were independently
re-verified. The full 287-test backend suite passes with zero regressions, both before
and after the one documentation-wording correction this gate made (per its explicit
mandate to correct imprecise wording, not functional code).

**Gate decision: HOLD — CORRECTIONS REQUIRED.** The single blocking finding is the
dashboard N+1 query pattern, now backed by clean measured evidence rather than prior
reasoning alone. A minimal, low-risk, set-based-batch-loading design is proposed
(Section 10) but was deliberately **not implemented** in this validation-only gate.

## 2. Exact Baseline Commit

```
HEAD (at start of this gate):  1bb3aad48cb7e946f488b871daef29c201472d77
Corrected implementation:      46586423a367b48a13a1975ecd5e0a2a56e19337
Original Phase 3 baseline:     4606810586084511a6b3992050baca63c5aedbf6
Branch:                        claude/new-session-1vutvy
```

**EXECUTED — VERIFIED**: `git status` was clean, `git branch --show-current` and
`git rev-parse HEAD` were run directly and matched the commit named in the task before
any diagnostic work began. One functional-adjacent change was made during this gate
(Section 9) — a docstring-only correction — confirmed via `git diff --stat` to touch
only comment text in `power_graph.py`.

## 3. Environment

- PostgreSQL 16 and Redis were not running at the start of this session and were
  started manually.
- The shared `dcim_test` database was already at `0007_correction` (head).
- Three dedicated scratch databases were created and later dropped:
  `dcim_indepval` (F-C1 multi-process/interruption/termination testing, with two
  genuinely separate `uvicorn` processes on ports 8030/8031), `dcim_perfscale` (dashboard
  performance measurements, using the app imported in-process via `httpx.ASGITransport`
  so a SQLAlchemy event listener could count exact query executions), and the earlier,
  killed `dcim_reaudit`-equivalent attempt at the pathological deep-chain topology
  (superseded by the shallow-topology measurement once the deep-chain test was found to
  be impractically slow — see Section 10).
- All temporary diagnostic scripts and scratch databases were deleted/dropped before
  this report was finalized.

## 4. Test Methodology

Every experiment in Sections 5-13 was performed with a fresh, independently-written
script for this specific gate — none reused the hostile re-audit's own scripts verbatim
(those were already deleted from the prior session). Where this gate's results are
consistent with the prior audit's numbers, that consistency is reported as
corroboration, not substituted for re-execution. Every conclusion below is labeled
**EXECUTED — VERIFIED**, **STATIC ANALYSIS**, **ANALYTICAL CONCLUSION**, or
**NOT EXECUTED**, per the task's evidence standard.

## 5. F-C1 Independent Validation

### 5.1 Implementation re-read (STATIC ANALYSIS)

Re-read `power_graph.py` and `power.py`'s `create_power_connection` directly (not
trusting either prior document's description). Confirmed unchanged from the prior
audit's own findings: the advisory lock (`pg_advisory_xact_lock`, transaction-scoped,
not `pg_advisory_lock`) is acquired before every graph-state read in the route body;
`lock_node_pair_in_canonical_order` is retained beneath it; exactly one construction
site for `PowerConnection` exists in the whole repository (Section 12).

### 5.2 Genuine concurrency proof (EXECUTED — VERIFIED)

Fresh script, real PostgreSQL, real HTTP:

- The exact F-C1 race (pre-existing N2→N3/N4→N1, concurrent N1→N2 and N3→N4): 10
  repeated single-process trials — `[201, 422]` every time, no cycle.
- A fresh 3-way triangle, reversed-order submission, and a mixed 5-way
  shared-endpoint-plus-disjoint-pairs scenario: all behaved correctly (exactly the
  expected number of successes, no over-rejection, no cycle).
- A cycle manually inserted via direct SQL (bypassing the app's own cycle check
  entirely) was traversed via the real API in 10ms with no infinite loop, and a new
  edge touching that component was correctly evaluated only against its own two
  endpoints (201, since it did not itself close a new cycle) — confirming the BFS's
  visited-set makes it inherently safe against any cyclic state, however it got there.

## 6. Multi-Process Validation (EXECUTED — VERIFIED)

**This is new evidence — no prior document in this repository ran the race across
genuinely separate OS processes.** Two independent `uvicorn` instances were started
(`ps aux` confirmed distinct PIDs 20548 and 20549), on ports 8030 and 8031, both
connected to the same scratch PostgreSQL database and sharing no Python object,
event loop, or connection pool.

```
MP trial-0 through MP trial-7: codes=[201, 422] in every trial (process 1 = first
  argument, process 2 = second argument), never both succeeding, never both failing,
  never a cycle in the resulting graph.
VERDICT: 8/8 multi-process trials correctly serialized -- lock coordination confirmed
  cross-process via PostgreSQL itself, not via any Python process-local state.
```

This directly satisfies the task's explicit requirement: "no race depends on Python
process-local state." It does not — PostgreSQL's own advisory lock table is the sole
coordination point, and two entirely separate interpreter processes correctly
serialized against each other through it.

## 7. Connection-Interruption Validation (EXECUTED — VERIFIED)

**Physical interruption (`pg_terminate_backend`)**: a raw `asyncpg` connection acquired
the lock and held it; a concurrent waiter began blocking; the holder's backend was then
forcibly terminated via `pg_terminate_backend(pid)` while still holding the lock. The
waiter unblocked in 0.482 seconds (consistent with the ~0.5-second delay before
termination was issued) — the lock did not remain stuck.

**Physical process termination (SIGKILL)**: a separate Python subprocess (confirmed
distinct OS PID) connected directly to PostgreSQL, acquired the same advisory lock,
printed a ready marker, and blocked indefinitely. A concurrent waiter began blocking on
the same lock. The subprocess was then `SIGKILL`ed (`os.kill(pid, signal.SIGKILL)`,
confirmed exit code -9, i.e., killed by signal 9, not a clean exit). The waiter
unblocked 0.479 seconds later.

**Application exception after lock acquisition, before commit** (using the
application's own real code path, not a synthetic exception): the cycle-rejection path
raises `WouldCreateCycle` while the advisory lock is held, inside the `async with`
block, propagating to `get_db`'s `except Exception: rollback` handler. Verified: the
rejected request left zero partial `PowerConnection` rows (queried directly), and an
unrelated mutation immediately afterward succeeded in 58ms — no delay, no stuck lock.

All four sub-items the task named (A: multi-process, B: connection interruption, C:
exception after lock acquisition, D: process termination) were executed, not merely
reasoned about.

## 8. F-M3 Ownership Integrity

Re-examined `power_node.owning_asset_id` fresh, independently re-testing the bypass
(not merely re-reading the prior audit's own conclusion):

1. **What can populate it?** Three `node_type` values: `power_circuit`, `pdu_outlet`
   (though `pdu_outlet` nodes are also constrained via the separate `pdu_outlet` table's
   now-composite FK), and `equipment_power_input`. Each expects a different target
   `ManagedAsset` subtype.
2. **DB constraints?** A plain FK to `managed_asset.id` — any `ManagedAsset` row
   satisfies it, regardless of `asset_type`.
3. **Raw-SQL bypass?** **EXECUTED — VERIFIED**: created a `managed_asset` row with
   `asset_type='rack'`, then a `PowerNode` with `node_type='equipment_power_input'` and
   `owning_asset_id` pointing at that rack asset. **Succeeded with no error.**
4. **API-level bypass?** `create_equipment_feed` (`power.py`) calls `db.get(Equipment,
   body.equipment_asset_id)` and 404s if the referenced asset isn't found via the
   `Equipment` model's own query — **STATIC ANALYSIS confirms the supported API path
   cannot create this specific invalid relationship**, only a raw-SQL bypass can.
5. **Can an invalid relationship reach traversal/capacity logic?** **ANALYTICAL
   CONCLUSION**: yes, trivially — `_traverse` and `compute_allocated_kw` operate purely
   on `power_node`/`power_connection` rows and never inspect `owning_asset_id`'s target
   subtype; a `PowerNode` with a mismatched `owning_asset_id` would traverse and
   aggregate identically to a correctly-typed one. This was not independently
   re-executed with a live traversal call against the deliberately mistyped row in this
   specific gate (the prior hostile re-audit's session did construct this exact scenario
   and observed no crash) — **NOT EXECUTED fresh in this gate, STATIC ANALYSIS only**.
6. **Is this intentional per the architecture?** Re-reading `ARCHITECTURE_REVIEW.md`
   §4b/§13 and `PHASE3_CORRECTION_DESIGN.md` Part 11 together: the architecture
   specifies `owning_asset_id` as a single generic column precisely because §4.1
   explicitly instructs "do not create unnecessary vendor-specific power models" —
   generalizing across subcomponent types was a deliberate modeling choice, and the
   correction design's own Part 11 explicitly classified closing this specific gap as
   `REQUIRES ARCHITECTURAL DECISION` rather than a straightforward fix, precisely
   because (unlike `pdu_outlet.pdu_asset_id`, which always means exactly one subtype)
   this one column serves three different expected subtypes.
7. **If intentional, why is it acceptable?** The only way to close it without a
   procedural `CONSTRAINT TRIGGER` (a real departure from this codebase's otherwise pure
   declarative-constraint style) would be to split `owning_asset_id` into three
   separate, mutually-exclusive nullable columns (mirroring `PowerNode`'s own existing
   `managed_asset_id`/`owning_asset_id` exclusivity pattern one level deeper) — a
   non-trivial schema change affecting every existing `power_circuit`/`pdu_outlet`/
   `equipment_power_input` row and every query that reads `owning_asset_id` today. This
   is a real, proportionate architectural trade-off, not an oversight, and matches the
   task's own instruction not to automatically treat an intentionally-deferred item as
   a defect.
8. **Minimal correction design, if ever pursued** (not implemented, not required by
   this gate): a `CONSTRAINT TRIGGER` on `power_node` checking, per `node_type`, that
   `owning_asset_id` (when set) references a `managed_asset` row of the matching
   subtype — the one approach that does not require splitting the column, at the cost
   of one procedural constraint in an otherwise declarative schema. Recommended as a
   **LOW-priority, documented carry-forward**, not a blocking finding for this gate.

## 9. Dashboard N+1 Investigation

### 9.1 Realistic shallow-topology measurement (EXECUTED — VERIFIED)

Topology: 1 utility intake → generators → UPSes → PDUs → equipment leaves (depth ~4-5),
capacity records placed only on ~20% of leaf equipment nodes — the pattern a real
operator would use (tracking capacity where load actually connects, not on every
intermediate hop). Measured via the app imported in-process (`httpx.ASGITransport`)
with a SQLAlchemy `before_cursor_execute` event listener counting exact query
executions — not an estimate.

| Nodes | Capacity records | Edges | Avg. SQL queries | p50 latency | min | max |
|---|---|---|---|---|---|---|
| 100 | 17 | 99 | 99 | 58.7ms | 55.0ms | 153.8ms |
| 500 | 87 | 499 | 449 | 244.4ms | 225.4ms | 262.5ms |
| 1,000 | 175 | 999 | 889 | 469.5ms | 447.1ms | 544.4ms |
| 5,000 | 877 | 4,999 | 4,399 | 2,313.2ms | 2,285.6ms | 2,331.3ms |

**Query count and latency both scale linearly with the number of capacity-bearing
nodes** (roughly 5 queries and ~2.6ms per capacity record, consistently across every
measured scale — 99/17≈5.8, 4399/877≈5.0, converging as scale grows). This is the
textbook signature of an N+1 pattern: no batching, no plateau, cost proportional to row
count scanned in a loop.

**10,000-node extrapolation (ANALYTICAL CONCLUSION, NOT EXECUTED at this scale)**: at
the observed linear rate, ~1,750 capacity records at 10,000 nodes would imply roughly
8,700 queries and ~4.6 seconds p50 latency — not independently measured, a linear
projection from four real, consistent data points spanning a 50x range.

### 9.2 Adversarial deep/dense-capacity topology (EXECUTED — ATTEMPTED, NOT COMPLETED)

A second topology — a 10-node chain with 90 leaves attached, capacity records placed on
**all** chain nodes (intermediate infrastructure, not just leaves) as well as some
leaves — was built at only n=100 nodes. The dashboard request was still running after
more than 8 minutes of wall-clock time and was forcibly terminated; captured structured
log output showed the request processing capacity-bearing nodes at a sustained rate of
roughly 600-700ms *each*, meaning the observed portion of this single request alone
would have taken well over a minute to complete, and the process was killed before it
finished. **This was not allowed to run to completion — the exact total time is
NOT EXECUTED/NOT MEASURED** — but the sustained per-node processing rate observed before
termination is real, captured evidence, not a projection. Root cause (STATIC ANALYSIS):
when a capacity-bearing node is also an ancestor of other capacity-bearing nodes deep in
a branching structure, `compute_allocated_kw`'s pass-1 subtree discovery re-walks large,
overlapping portions of the graph once per capacity-bearing ancestor, with no caching
across the dashboard's own outer loop — a cost pattern that is normally masked by shallow
topologies (Section 9.1) but becomes severe when capacity tracking exists at multiple
tiers of the same physical hierarchy, a topology shape the master prompt's own domain
description (rack/PDU/panel capacity budgets, not only equipment-level) does not rule
out as unrealistic.

### 9.3 Is this acceptable for the intended DCIM operating model?

**This is the one place this gate disagrees with the prior hostile re-audit's framing.**
The prior document classified this as a MEDIUM "documentation gap." Having now measured
it directly: at 5,000 nodes (the master prompt's own stated *lower* bound for Phase 3
scale) the primary dashboard endpoint already takes over 2.3 seconds with a realistic,
non-adversarial topology, and a plausible (not exotic) alternative topology shape makes
the same endpoint take minutes or worse. **This is re-classified as HIGH** in this
gate's findings register (Section 15) — not because any data returned is incorrect (it
is not), but because the system's primary operational surface materially fails to
deliver usable performance at the scale the correction's own design documents already
say the phase targets.

## 10. Performance Measurements

Summarized in Section 9.1's table. Additionally, the F-C1 advisory-lock throughput
measurement from the prior hostile re-audit was **independently reproduced** in this
gate (fresh script, same scratch environment used for the multi-process test):

```
n=10:  wall=784.6ms,  all 10 succeeded, p50=566.6ms,  p99=683.3ms,  max=683.3ms
n=50:  wall=2828.8ms, all 50 succeeded, p50=1566.1ms, p99=2469.4ms, max=2469.4ms
n=100: wall=5483.5ms, all 100 succeeded, p50=2843.7ms, p99=4659.1ms, max=4659.1ms
```

Consistent with the prior audit's own numbers (5.77s vs. this gate's 5.48s at n=100 —
within normal run-to-run variance), confirming the earlier measurement was not a
one-off artifact.

## 11. Advisory-Lock Throughput Assessment

Per Section 10's reproduced numbers: **acceptable but should be documented as an
explicit architectural trade-off (Option B in the task's classification)**, not
requiring redesign. Reasoning: connection creation is expected to be a low-frequency,
human-paced provisioning activity (wiring up a rack's power feeds), not a bulk hot
loop; every single one of the 160+ concurrent requests fired across this gate's various
throughput and race tests succeeded (zero failures, zero timeouts) — the cost is
latency under contention, not incorrectness or request loss. Replacing the lock
purely to improve concurrency would violate the task's own stated primary invariant
(correctness first) without a demonstrated correctness problem to justify the churn.
**No redesign is proposed or warranted for the advisory lock itself.**

## 12. Mutation-Path Completeness (EXECUTED — VERIFIED, re-run with an expanded pattern set)

```
grep "PowerConnection(" across app/scripts        -> exactly 1 construction site (power.py)
grep "INSERT INTO power_connection"                -> zero matches outside migration DDL/docstrings
grep "bulk_insert|bulk_save_objects"                -> zero matches anywhere in app/
grep "effective_to" across app/                     -> every match is either a read-filter (WHERE
                                                        effective_to IS NULL) or the ONE disconnect
                                                        mutator (power.py:573); no reactivation path
grep "\.status = " on PowerConnection objects        -> exactly one call site (power.py:530, the
                                                        PATCH endpoint), touching only
                                                        connection_type/phase/voltage/rated_current_a/
                                                        status -- never effective_to, never a
                                                        reactivation
find backend/app/infrastructure -iname "*power*"     -> zero results (no Celery/background task)
find backend/scripts -type f                          -> docker-initdb script, bootstrap SQL,
                                                          create_admin.py -- none reference power
```

**Confirmed, independently, with a broader search than either prior audit used**:
exactly one application path creates an active `PowerConnection`, exactly one path
disconnects one (sets `effective_to`), and exactly one path (PATCH) mutates
non-topological fields without ever reactivating or duplicating an edge. No script,
background worker, or admin tool touches this table. The single-mutation-path
assumption underlying F-C1's entire correctness argument remains true.

## 13. Migration Integrity

Fresh install to head (`dcim_indepval`, `dcim_perfscale`, both created and migrated
0→head in this gate): **EXECUTED — VERIFIED**, both succeeded cleanly. Downgrade
(0007→0006) then re-upgrade (0006→0007), independently re-run on `dcim_indepval` in
this gate: **EXECUTED — VERIFIED**, both completed without error, `alembic current`
confirmed the expected state at each step. The pre-flight safety check (aborting
against manufactured invalid data) was **not** re-executed in this specific gate — it
was already verified once in the implementation session and once described in the
prior hostile re-audit; this gate's time budget was spent on the F-C1 multi-process/
interruption work and the dashboard measurement instead. **NOT EXECUTED this session**,
disclosed rather than silently assumed.

## 14. Regression Results

```
Command: cd /home/user/DCIM/backend && pytest -q tests/
Baseline run (start of this gate):        287 passed, 6 warnings, 66.50s
Final run (after the documentation fix):  287 passed, 6 warnings, 67.01s
```

**EXECUTED — VERIFIED**, twice, bracketing the one change made in this gate. Zero
failures, zero skips, zero newly added tests in this gate (this was a validation, not
an implementation, task — no new test files were written). Frontend: no automated test
suite exists in this repository (`package.json` has no `test` script, no `.test.tsx`/
`.test.ts` files found anywhere outside `node_modules`) — **this is itself worth
recording plainly rather than silently working around**: "frontend tests" as a category
does not exist to run. `npx tsc --noEmit` and `npm run build` were re-run and passed
cleanly, the closest available frontend verification. No browser/golden-path session
was run in this gate (no frontend file has changed since the last such validation, and
this gate's time budget was prioritized toward the backend attack surfaces the task
emphasized most heavily) — **NOT EXECUTED**, disclosed.

## 15. Findings Register

| ID | Severity | Description | Status |
|---|---|---|---|
| IV-1 | HIGH | Dashboard N+1: query count and latency scale linearly with capacity-record count, reaching 2,313ms/4,399 queries at 5,000 nodes (half the master prompt's stated target scale) on a realistic topology; a plausible alternative topology (capacity tracked at intermediate infrastructure tiers) made the same endpoint take minutes at only 100 nodes. Re-classified up from the prior audit's MEDIUM given fresh, clean, measured evidence. | **NEW MEASUREMENT of a previously-known pattern — not a newly discovered code path, but newly quantified severity** |
| IV-2 | LOW | `owning_asset_id` subtype gap remains open; a `CONSTRAINT TRIGGER` is the only closure path that doesn't require a schema-breaking column split. | Reconfirmed, unchanged, accepted |
| IV-3 | INFO | No automated frontend test suite exists in this repository. | Newly and plainly recorded |
| IV-4 | INFO | Migration pre-flight safety-check behavior was not re-executed in this specific gate (already verified twice previously). | Disclosed |
| IV-5 | INFO | Browser/golden-path validation was not re-executed in this gate. | Disclosed |

No CRITICAL finding. No new HIGH finding beyond the re-classification of the already-
known dashboard pattern. F-C1 produced zero new findings of any severity across every
attack angle this gate added.

## 16. Severity Classification (rationale)

IV-1 is classified HIGH, not CRITICAL, because no data returned by the dashboard is
ever incorrect and no security/authorization boundary is affected — this is a pure
availability/usability degradation, not an integrity violation, which is why it does
not meet this report's own CRITICAL bar ("violate a fundamental integrity/security
invariant or corrupt authoritative topology"). It is HIGH, not MEDIUM, because it
"materially" affects operational usability of the system's primary surface at a scale
the correction's own design documents already commit to supporting, with clean,
reproducible, linearly-scaling measured evidence — not merely a theoretical or
previously-undisclosed concern.

## 17. Required Corrections

**One.** The dashboard N+1 pattern (IV-1) must be addressed before this phase should be
considered ready to build further features on top of (Phase 4 or otherwise), given it
already measurably fails to perform acceptably at half the stated Phase 3 target scale.

### Minimal design (NOT IMPLEMENTED in this gate)

Replace the per-capacity-record loop in `dashboard.py`'s summary/exceptions
construction with a small, fixed number of set-based batch queries, computed once for
the whole request rather than once per node:

1. **One query** to fetch every current (`effective_to IS NULL`) `PowerCapacity` row
   the dashboard needs (already done today — this part is not the problem).
2. **One query** to fetch every active (`effective_to IS NULL`) `PowerConnection` edge
   in the graph (source_node_id, target_node_id) — bounded by the same realistic scale
   this phase already targets (10,000 rows is a trivial single-query fetch, well within
   normal query-time and memory budgets for a single dashboard request).
3. **One query** to fetch every `PowerNode.retired_at` flag needed (or fold into query
   2 via a join), preserving F-H2's retirement-exclusion semantics exactly — a node
   excluded from a Python-side traversal built from this batch-loaded edge set behaves
   identically to today's per-query retirement filter, just computed once in memory
   instead of once per node via a repeated round trip.
4. **In-memory aggregation**: build the same `children_of` adjacency structure
   `compute_allocated_kw` already builds per-call, but build it exactly once for the
   whole request, then run the existing bottom-up memoized aggregation logic
   (unchanged) against this one shared structure for every capacity-bearing node the
   dashboard needs — the memoization that already exists *within* one call to
   `compute_allocated_kw` would then also work *across* every node the dashboard scans,
   since overlapping subtrees (a very common case — many nodes share upstream
   ancestors) would be computed once instead of once per query.
5. Preserve exactly: retired-node semantics (Section 8's model, computed from the same
   batch-loaded `retired_at` data), capacity correctness (the aggregation logic itself
   is unchanged, only its data source moves from per-node queries to one shared
   in-memory structure), redundancy semantics (`equipment_power_summary` would need the
   same batching treatment separately, as it currently issues its own per-equipment
   queries), bounded traversal (the batch load itself should still enforce
   `MAX_TRAVERSAL_NODES` as an upper bound on how many edges/nodes it will load in one
   request, converting today's *per-node* bound into a *whole-dashboard* bound — a
   deliberate, documented semantic change that should be called out explicitly if
   implemented), transaction correctness (read-only, no transactional concerns), and
   authorization/auditability (unaffected — this is a read path with no audit
   requirement today).

This is a batch-loading redesign, not a cache (no staleness risk introduced), not a
denormalization (no new stored/derived column), and not a premature abstraction (it
reuses the exact aggregation algorithm already proven correct by the existing test
suite, just fed from one shared data load instead of many repeated ones) — consistent
with the task's explicit preference ordering.

## 18. Recommended Corrections (non-blocking)

- IV-2 (`owning_asset_id`): a `CONSTRAINT TRIGGER`, if and when the team decides the
  residual gap is worth the departure from this codebase's declarative-constraint
  style. Not required before proceeding.
- Consider adding a minimal automated frontend test (even a handful of component smoke
  tests) given IV-3's observation that none exist today — a process/tooling
  recommendation, not a Phase 3 correctness gap.

## 19. Explicitly Unverified Items

- The real, unmonkeypatched `MAX_TRAVERSAL_NODES=5000` bound (F-H1/graph traversal) was
  not independently re-exercised at its literal production value in this gate.
- Full completion time for the adversarial deep/dense-capacity dashboard topology
  (Section 9.2) — the request was killed before finishing; only a sustained partial
  processing rate was captured, not a total.
- 10,000-node dashboard measurement — extrapolated analytically from four real data
  points, not independently executed at that scale.
- Migration pre-flight safety-check re-execution (already verified twice in prior
  sessions, not repeated in this gate).
- Frontend/browser interactive validation (no frontend file changed; not re-run in this
  gate).
- `owning_asset_id` mistyped-relationship traversal was not freshly re-executed against
  a live traversal call in this specific gate (the prior hostile re-audit's session did
  this once; this gate relied on that prior execution plus fresh static/architectural
  analysis rather than repeating it).
- Multiple genuinely separate application *processes* were tested for F-C1 (Section 6);
  a full multi-*machine*/distributed-deployment scenario was not, and is not expected to
  differ (PostgreSQL, not any single machine, is the coordination point), but this
  extrapolation itself is **ANALYTICAL CONCLUSION**, not executed.

## 20. Final Gate Status

**HOLD — CORRECTIONS REQUIRED**

**Blocking finding**: IV-1 (dashboard N+1, HIGH severity, Section 9/15/17). This is the
one and only required correction before this phase should be considered ready to
proceed further. It is bounded, well-understood, has a concrete low-risk design already
worked out (Section 17), and does not implicate any correctness, security, or
concurrency defect in the F-C1/F-H1/F-H2/F-M1/F-M3 corrections themselves — all of
which independently re-validated cleanly in this gate across genuinely new attack
surfaces (multi-process, physical connection/process interruption) that no prior
document in this repository had actually executed before now.

Everything else validated in this gate — F-C1's correctness under concurrent, multi-
process, and interrupted execution; the mutation-path completeness assumption;
migration integrity; the advisory-lock throughput trade-off; and the `owning_asset_id`
architectural deferral — is judged acceptable to carry forward once IV-1 is addressed,
with the accepted risks and reasoning stated plainly in this report rather than left
implicit.
