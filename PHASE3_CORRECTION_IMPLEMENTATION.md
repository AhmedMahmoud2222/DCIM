# Phase 3 Correction Implementation

**Status: IMPLEMENTATION COMPLETE — AWAITING HOSTILE RE-AUDIT.** This document does
NOT declare Phase 3 approved, does NOT declare an independent red-team pass, and does
NOT certify anything. It records what was implemented, exactly how it was verified, and
what remains open.

## Executive Summary

This implements the approved `PHASE3_CORRECTION_DESIGN.md` against the Phase 3
implementation at commit `4606810586084511a6b3992050baca63c5aedbf6`. All eight
corrections in scope (F-C1, F-H1, F-H2, F-M1, F-M2, F-M3, F-M5, and the NEW-1
interaction check) are implemented per the approved design with **zero deviations** —
every design decision (global advisory lock over SERIALIZABLE, Model A retirement
semantics, documentation-only for F-M2, composite-FK-only for the `pdu_outlet` half of
F-M3) was carried through exactly as designed.

287/287 backend tests pass (269 pre-existing + 18 new), including a genuine, real-
PostgreSQL reproduction of the exact hostile-audit F-C1 race proving the correction
closes it, repeated across 10 independent trials plus a 3-way triangle variant. Fresh
install, upgrade-with-representative-data, downgrade, and re-upgrade were all validated
for the new migration, including a deliberate test that the migration's own pre-flight
safety check correctly refuses to apply the new constraint over manufactured invalid
data rather than silently allowing or discarding it.

## Starting Commit

`4606810586084511a6b3992050baca63c5aedbf6` — confirmed via `git rev-parse HEAD^` at the
start of this implementation session, matching the commit named in all four upstream
documents.

## Correction Design Used

`PHASE3_CORRECTION_DESIGN.md` (approved) and its companion
`PHASE3_CORRECTION_DESIGN_SCOPE.md`, both produced in the prior design-only session.
Findings sourced from `PHASE3_HOSTILE_SELF_AUDIT.md` / `PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md`.

## Repository-Wide Mutation-Path Search (Required Before F-C1 Implementation)

Per the task's explicit instruction not to assume the API POST is the only
`PowerConnection` mutation path, a repository-wide search was run before writing any
code:

```
grep -rn "PowerConnection(" backend/app backend/scripts
  -> backend/app/api/v1/power.py:430 (the one and only construction site)
  -> backend/app/domain/power/models.py (the class definition itself)
grep -rln "power_connection" backend/app
  -> app/api/v1/power.py, app/application/power_graph.py, app/domain/power/models.py
find backend/app/infrastructure -iname "*power*"  -> (no results)
find backend/scripts -iname "*power*"             -> (no results)
```

**Confirmed: exactly one application code path can insert an active `PowerConnection`
row** — `create_power_connection` in `backend/app/api/v1/power.py`. No Celery task, no
bulk-import script, no background job, no admin tool touches this table. This matches
the correction design's own stated assumption; no architectural substitution was
required.

## Changes Implemented

### F-C1 — Global Power Graph Acyclicity

**File:** `backend/app/application/power_graph.py`.

- Added `POWER_TOPOLOGY_MUTATION_LOCK_KEY = 918_273_645`, a single fixed constant.
- Added `with_connection_mutation_lock(db)`, an `@asynccontextmanager` wrapping
  `SELECT pg_advisory_xact_lock(:key)` — transaction-scoped (not session-scoped), so it
  self-releases on COMMIT, ROLLBACK, or connection loss with no manual unlock call and
  no risk of leaking across pooled-connection reuse.
- Added timing instrumentation: if lock acquisition takes ≥0.5s, an `info`-level
  structured log line (`power_topology_mutation_lock_contended`) is emitted — a leading
  indicator of real contention, per F-M5.

**File:** `backend/app/api/v1/power.py` — `create_power_connection` now runs its entire
`lock → canonical-pair-lock → cycle-check → duplicate-check → insert → audit →
outbox → commit` sequence inside `async with with_connection_mutation_lock(db):`, as
the very first statement of the transaction. `lock_node_pair_in_canonical_order` is
retained unchanged beneath it (still prevents the classic same-pair opposite-order
deadlock; no longer the sole cross-pair safety mechanism). A defensive
`except OperationalError` wraps the whole block, mapping any genuine PostgreSQL-level
failure (e.g. an unrelated deadlock, `40P01`) to a clean `503 Service Unavailable`
rather than an unhandled `500` — logged at `warning` with the SQLSTATE. This design
does not require automatic retry for its expected path (Part 3 of the design explicitly
anticipated this and it is confirmed true — see the F-C1 Proof section below).

**Explicitly NOT changed:** the per-pair canonical-order locking, the cycle-check
algorithm itself, the duplicate-active-edge pre-check, or the DB-level self-loop/
duplicate-edge constraints — none of these were broken, so none were touched.

### F-C1 Concurrency Tests

**File:** `backend/tests/api/test_power.py` (new tests, real PostgreSQL, real
concurrent HTTP requests through the actual FastAPI app via `httpx.ASGITransport`, each
request on its own dedicated `AsyncSession` from a separate engine — never a shared
single session, which cannot represent genuine concurrency):

- `test_hostile_f_c1_independent_pair_race_cannot_create_cycle` — the exact hostile-
  audit reproduction (pre-existing N2→N3, N4→N1; concurrent N1→N2 and N3→N4). Asserts
  exactly one `201` and one `422`, then independently re-verifies the committed graph
  contains neither the N1→N2 edge AND the reachability that would together prove a
  cycle.
- `test_hostile_f_c1_race_repeated_ten_times` — the same race across 10 independent
  node quadruples in one run, to catch timing-dependent flakiness a single trial could
  miss.
- `test_hostile_f_c1_three_concurrent_writers_cannot_close_a_triangle` — 3+ concurrent
  writers (A→B, B→C, C→A simultaneously): exactly 2 of 3 may commit, never all 3.
- `test_concurrent_independent_valid_mutations_both_succeed` — proves the lock does not
  over-reject: two completely unrelated, individually valid mutations on disjoint node
  pairs both succeed when run concurrently.
- `test_f_c1_rollback_on_rejected_cycle_leaves_no_partial_edge` — a rejected
  cycle-creation attempt leaves no partially-committed row behind.
- The pre-existing `test_concurrent_connection_creation_between_same_pair_only_one_wins`
  (same-pair race) was re-run unchanged and still passes, confirming the correction did
  not regress the case that already worked.

### F-C1 Failure/Retry Semantics

Per Part 6 of the approved design, the chosen mechanism (global advisory lock) does
**not require automatic retry for its expected path** — a losing request already
receives the *correct* answer (the edge really would create a cycle, evaluated against
the true, fully-committed graph), so there is nothing to retry. This is confirmed, not
merely assumed: every F-C1 test above observed exactly one clean success and the rest
cleanly rejected via the pre-existing `WouldCreateCycle` → 422 path, with zero
serialization failures or retries needed across all 10+ repeated trials. The one
defensive addition (`except OperationalError` → 503) exists for a genuinely unrelated
failure mode (e.g. an operational deadlock from some other, unrelated lock
interaction), not for this mechanism's own expected behavior, and is never automatically
retried — it surfaces as a clean, client-retryable 503.

### F-H1 — Capacity Unknown

**File:** `backend/app/application/power_capacity.py` — `derive_node_capacity_exceptions`
now branches on `figures.data_quality == "unknown"` first (emitting `CAPACITY_UNKNOWN`
and returning immediately), before ever checking `utilization_pct`/`effective_capacity_kw`
individually. This closes the exact masking case the hostile audit found: a node whose
own rated/configured capacity is known but whose allocation roll-up hit the bounded-
traversal limit (`data_quality == "unknown"` via `alloc_quality`) now correctly produces
`CAPACITY_UNKNOWN` instead of an empty exception list. A structured `info`-level log
line (`power_capacity_unknown`) was added alongside it, recording whether the capacity
side or the allocation side (or both) was the unknown component (F-M5).

**Interaction verified:**
- `CAPACITY_OVERLOAD`/`CAPACITY_NEAR_LIMIT`: mutually exclusive with `CAPACITY_UNKNOWN`
  by construction (the `unknown` branch returns immediately; the two threshold checks
  are only ever reached once `utilization_pct` is a real number) — confirmed by
  `test_capacity_unknown_does_not_also_report_overload_or_near_limit`, which uses tiny
  thresholds specifically to prove a fabricated overload conclusion is never drawn from
  incomplete data.
- `POWER_PATH_MISSING`: unaffected — it is derived independently, from
  `equipment_power_summary`'s redundancy classification, not from
  `derive_node_capacity_exceptions` — confirmed unchanged by the full existing test
  suite passing.
- Dashboard / `/power/capacity-exceptions`: no response-shape change — `CAPACITY_UNKNOWN`
  was already a defined, documented condition code; this fix makes it reachable in a
  new situation, not a new code.

### F-H2 — Retired PowerNode Semantics (Model A)

**Files:** `backend/app/application/power_graph.py`, `backend/app/application/power_capacity.py`.

Model A implemented exactly as designed: retired nodes are excluded from operational
traversal and capacity roll-up as a *bridge*, but a retired node's own direct traversal
(querying it as the root) is deliberately not filtered, preserving historical/audit
inspection.

- **`power_graph.py`:** added `non_retired_node_ids(db, node_ids)`, returning the
  subset of a candidate set whose `PowerNode.retired_at IS NULL`. `_traverse`'s BFS now
  filters every candidate *next-level* node through this before adding it to `visited`
  — a retired node is neither included in the result nor expanded past, so
  `Utility -> Retired PDU -> Equipment` correctly stops at the retired PDU. The
  traversal *root* is exempt (its own direct neighbors are still returned when queried
  directly — a documented, deliberate historical/audit exception, not an oversight).
- **`power_capacity.py`:** `compute_allocated_kw`'s pass-1 subtree discovery applies the
  same filter to its own candidate next-level nodes. Pass-2's `children_of` edge query
  now joins `PowerNode` and filters `retired_at IS NULL` on the *target* side — this
  second filter is necessary in addition to the first: without it, a retired node that
  happens to be a one-hop child of an already-discovered live node would still appear
  as a phantom "not_applicable" child in the parent's aggregation, incorrectly
  downgrading the parent's own `data_quality` to `"unknown"` (confirmed by the new test
  `test_retired_intermediate_does_not_poison_ancestor_with_unknown_quality`, which
  specifically constructs this scenario and asserts the parent's quality stays
  `"known"`).
- **`equipment_power_summary`'s `has_upstream_path`:** previously a shallow "does *any*
  active connection row point at this node" check — replaced with
  `len(await get_upstream_node_ids(db, node.id)) > 0`, so it now reflects the
  retirement-aware traversal rather than mere row existence. This is what makes the
  task's explicit example (Utility → Retired PDU → Equipment must show a broken path,
  not "Utility → Equipment") hold at the equipment level, not just in raw traversal
  calls — confirmed end-to-end via
  `test_retired_intermediate_node_produces_power_path_missing_for_equipment` (real HTTP,
  real equipment/PDU/utility chain, retire the PDU mid-test, assert `has_upstream_path`
  flips from `True` to `False`).

**Tested:** retired intermediate (downstream and upstream directions), retired
traversal root (documented exemption, not a broken case), retired leaf (excluded from
results), a live sibling branch unaffected by a retired branch, retired PDU's capacity
excluded from an ancestor's `allocated_kw`, a retired node with no capacity/children not
poisoning its parent's data quality, and the full equipment-level `has_upstream_path`
flip via real HTTP. Not separately re-tested per literal node subtype (retired panel/
UPS/generator specifically) beyond the retired-PDU and retired-generic-node cases above
— the traversal/allocation logic is identical regardless of which `ManagedAsset` subtype
backs a `PowerNode`, so this is not believed to be a gap, but it was not independently
re-verified for every subtype name.

### F-M1 — If-Match

**File:** `backend/app/api/v1/power.py` — `set_node_capacity` now calls the shared
`parse_if_match` (from `app.application.concurrency`, already used correctly by
`PowerConnection` PATCH) instead of the ad hoc `int(if_match.strip().strip('"'))`.
Confirmed: `test_malformed_if_match_on_capacity_put_is_a_clean_400_not_a_500` — a
malformed `If-Match: not-a-number` now returns a clean `400`, never an unhandled
exception.

| Input | Behavior (verified) |
|---|---|
| Missing, no current record | Proceeds (unchanged) |
| Missing, current record exists | `428` (unchanged) |
| Valid integer, matches | Proceeds (unchanged) |
| Valid integer, stale | `409` (unchanged, existing test coverage) |
| Malformed (non-integer) | **`400`, corrected — new test** |

Negative/zero/quoted values were not independently re-tested in this pass beyond what
`parse_if_match`'s own existing behavior already covers (it strips quotes and parses any
valid integer, including negative/zero, which then simply fails the ordinary version
match as any wrong version would) — reusing an already-tested shared helper rather than
re-deriving its correctness here.

### F-M2 — Redundancy

**No logic change**, per the approved design's explicit classification
(`DOCUMENTATION ONLY`). Confirmed by re-reading `equipment_power_summary` unchanged
except for the `has_upstream_path` computation change described under F-H2 above (which
is an F-H2 fix, not an F-M2 change — the shared-ancestor/SPOF-detection logic itself was
not touched). No new `degraded_reason` field was added (correctly deferred per the
design's `REQUIRES ARCHITECTURAL DECISION` classification for that specific addition).

### F-M3 — Subtype Integrity

**New migration:** `backend/migrations/versions/0007_correction_pdu_outlet_subtype_integrity.py`.

Implements the `pdu_outlet.pdu_asset_id` half exactly as designed (composite FK via a
generated column), and explicitly does **not** attempt the `power_node.owning_asset_id`
half, per the design's own `REQUIRES ARCHITECTURAL DECISION` classification for that
harder case (a single column shared across three different expected subtypes does not
admit the same fixed-literal trick).

- `managed_asset` gains `UNIQUE (id, asset_type)` — harmless in addition to the
  existing PK.
- `pdu_outlet` gains `pdu_asset_expected_type TEXT GENERATED ALWAYS AS ('pdu') STORED
  NOT NULL` — PostgreSQL computes and stores it; the application never writes to it.
- `pdu_outlet.pdu_asset_id`'s plain FK is replaced with a composite FK on
  `(pdu_asset_id, pdu_asset_expected_type)` referencing `managed_asset (id, asset_type)`,
  satisfiable only when the referenced asset's own `asset_type` is literally `'pdu'`.
- **Pre-flight safety check, exactly per the task's instruction not to assume existing
  data is clean:** `upgrade()` runs a `LEFT JOIN`-based count of any existing
  `pdu_outlet` row that would violate the new constraint, and **raises `RuntimeError`,
  aborting the migration**, if any are found — never silently deleting, coercing, or
  applying the constraint over bad data. **This was deliberately tested** (see Migration
  Validation below): a scratch database with a manufactured violating row correctly
  aborted the migration and remained at `0006_phase3`, not silently advanced.
- The ORM model (`backend/app/domain/power/models.py`, `PDUOutlet`) was updated to match
  (`Computed("'pdu'", persisted=True)` column, `ForeignKeyConstraint` in `__table_args__`).

### F-M5 — Observability

Six structured log points added (all via `app.core.logging.get_logger`, the project's
existing `structlog` convention — not stdlib `logging`, confirmed by checking
`core/errors.py`'s own pattern before adding these):

| Event | File | Level | Fields |
|---|---|---|---|
| Cycle rejected | `power.py` | `info` | `source_node_id`, `target_node_id`, `request_id`, `correlation_id` |
| Traversal bound exceeded (create, upstream, downstream endpoints) | `power.py` | `warning` | `root_id`, `limit_kind`, `direction`, `request_id`, `correlation_id` |
| DB operational error (deadlock/serialization safety net) | `power.py` | `warning` | `sqlstate`, `request_id`, `correlation_id` |
| Advisory-lock contention (≥0.5s wait) | `power_graph.py` | `info` | `wait_seconds`, `lock_key` |
| Capacity unknown (F-H1's new path) | `power_capacity.py` | `info` | `power_node_id`, `effective_capacity_known`, `allocated_known` |

Not added: a distinct log point for "invalid topology" beyond the cycle-rejection line
above (they are the same event in this implementation — a rejected cycle *is* the
invalid-topology detection); no log point for "graph mutation rejection" beyond cycle
rejection and traversal-bound, which are the two ways a mutation can be rejected by this
subsystem today.

### NEW-1 Interaction

**Re-verified, not implemented (per the task's explicit instruction not to implement
NEW-1 unless the design required it — it did not).** `grep` across the entire diff and
`app/api/v1/power.py` confirms `Idempotency-Key` is not handled by any Phase 3 endpoint,
before or after this correction. The F-C1 fix introduces no automatic retry loop (see
above), so there is no new "same idempotency key + transaction retry" interaction to
test — the design's own Part 13 anticipated this outcome, and it is confirmed true, not
merely assumed. `tests/integration/test_idempotency_concurrency.py` and
`tests/unit/test_idempotency.py` (Phase 1's own suite) were re-run as part of the full
suite and are unaffected (5 + remaining unit tests, all passing, unchanged from before
this correction).

## Files Changed

```
 backend/app/api/v1/power.py                                          | modified
 backend/app/application/power_capacity.py                            | modified
 backend/app/application/power_graph.py                                | modified
 backend/app/domain/power/models.py                                    | modified
 backend/migrations/versions/0007_correction_pdu_outlet_subtype_integrity.py | new
 backend/tests/api/test_power.py                                       | modified (+7 tests)
 backend/tests/integration/test_phase3_power_constraints.py            | modified (+2 tests)
 backend/tests/unit/test_power_capacity.py                             | modified (+4 tests)
 backend/tests/unit/test_power_graph.py                                | modified (+5 tests)
```

8 files changed, 1 new migration. No test was removed, weakened, skipped, or had its
assertions loosened. No Docker, CI, or configuration file was touched. No frontend file
was touched (confirmed unnecessary — see Browser/Frontend section).

## Database Changes

One new migration, `0007_correction`, fully additive except for one FK replacement
(the old plain FK on `pdu_outlet.pdu_asset_id` is dropped and replaced by the composite
one — no column is dropped from any existing row, no data is altered). See F-M3 above
for full detail. `downgrade()` reverses all three steps in the correct dependency order
(drop the composite FK and restore the plain one, drop the generated column, drop the
new unique constraint) and was verified to execute cleanly against data that existed
under the new constraint.

## Concurrency Design (Final)

Every `POST /power/connections` request now acquires
`pg_advisory_xact_lock(918273645)` as the first statement of its transaction, before
`lock_node_pair_in_canonical_order` and before the cycle check. This single, fixed,
transaction-scoped lock serializes every connection-creation transaction against every
other one, system-wide, regardless of which nodes they touch. The lock is released
automatically at COMMIT or ROLLBACK; no manual unlock call exists anywhere in the code,
and none is needed. The pre-existing canonical-order locking and cycle-check algorithm
are unchanged beneath it.

## F-C1 Proof

**Claim:** no interleaving of two or more concurrent `create_power_connection`
transactions can result in a committed graph containing a cycle none of them would have
individually accepted.

**Argument:** the protected section (lock → canonical-pair-lock → cycle-check → insert
→ commit) executes under a system-wide mutual-exclusion lock. By the definition of
mutual exclusion, no two invocations of this section are ever concurrently active.
Therefore, every cycle check the section performs is evaluated against a graph state
that reflects every previously-committed connection creation and none that are still
in flight — i.e., the *true*, fully-committed state at the moment of the check. The
pre-existing `assert_would_not_create_cycle` logic was never disputed as correct when
evaluated against a true, static graph snapshot (only its exposure to a *stale*,
concurrently-invalidated snapshot was the defect) — so with staleness eliminated by the
lock, its existing single-transaction correctness is now sufficient for the whole
system, not just for one transaction in isolation.

**This is not merely asserted — it was verified experimentally**, both in the prior
design session (5/5 trials of the exact minimal race) and again in this implementation
against the real, corrected API (`test_hostile_f_c1_independent_pair_race_cannot_create_cycle`,
repeated 10 times via `test_hostile_f_c1_race_repeated_ten_times`, and extended to three
concurrent writers via `test_hostile_f_c1_three_concurrent_writers_cannot_close_a_triangle`)
— EXECUTED — VERIFIED, zero cycles committed across every trial in every run.

**Scope of the claim, stated precisely (not overclaimed):** this is an
application-enforced transactional invariant upheld by every *supported application
mutation path* (confirmed to be exactly one, by repository-wide search, above). It does
not and cannot prevent a direct SQL bypass of the entire transaction protocol (no
database-level mechanism can express "the whole graph must remain acyclic" as a
constraint) — this was true before the correction and remains true after it; the
correction closes the *concurrent-application-mutation* race, not a hypothetical
direct-database-access threat, which was never in scope for this or any other
Phase 3 concurrency mechanism.

## Tests

**EXECUTED — VERIFIED** (real PostgreSQL 16, real Redis, no mocked database layer):

```
287 passed, 6 warnings in 75.80s
```

269 pre-existing tests (zero regressions) + 18 new tests:
- 5 new unit tests in `test_power_graph.py` (F-H2 traversal exclusion)
- 4 new unit tests in `test_power_capacity.py` (F-H1 exception masking, F-H2 allocation exclusion)
- 7 new API tests in `test_power.py` (F-C1 concurrency ×5, F-M1, F-H2 end-to-end)
- 2 new integration tests in `test_phase3_power_constraints.py` (F-M3 DB-level bypass and acceptance)

No test was monkeypatched to avoid exercising the real logic under test (the one
`monkeypatch` use, on `MAX_TRAVERSAL_NODES`, is the same pre-existing technique the
original Phase 3 suite already used to make a *bound* reachable without building
thousands of real rows — it does not touch or weaken the F-C1 locking mechanism, which
every concurrency test exercises against the real, unmodified advisory-lock code path).
No PostgreSQL locking behavior was mocked in any concurrency test — every one uses a
dedicated `AsyncSession` per concurrent request against the real test database.

## Concurrency Test Results (Detail)

| Test | Result |
|---|---|
| Exact F-C1 race, single trial | `[201, 422]`, no cycle in final graph — PASS |
| F-C1 race, 10 repeated trials | `[201, 422]` in all 10 — PASS |
| 3-way triangle race | 2×`201`, 1×`422` — PASS |
| Two unrelated concurrent valid mutations | `[201, 201]` — PASS (no over-rejection) |
| Rollback leaves no partial edge | confirmed empty connection list for rejected pair — PASS |
| Pre-existing same-pair race (5 concurrent identical requests) | `1×201, 4×409` — unchanged, still PASS |

## Migration Validation

**EXECUTED — VERIFIED**, against dedicated scratch databases (not the shared
`dcim_test` database used by the pytest suite):

1. **Fresh install to head** (`dcim_p3correction`): all 7 migrations (Phase 1 through
   `0007_correction`) applied cleanly in sequence.
2. **Representative data + downgrade + re-upgrade:** inserted a real `pdu_outlet` row
   referencing a genuine `pdu`-typed `ManagedAsset`, downgraded to `0006_phase3`
   (verified the row survived, generated column gone), re-upgraded to head (verified the
   generated column and composite FK were correctly re-derived, the same row's data
   intact throughout).
3. **Pre-flight safety-check validation** (`dcim_p3violation`): manufactured a
   `pdu_outlet` row referencing a non-`pdu` `ManagedAsset` (`asset_type='rack'`) directly
   via SQL, bypassing the API entirely, then attempted `alembic upgrade head`. **The
   migration correctly aborted** with a `RuntimeError` naming the exact violating row,
   and the database remained at `0006_phase3` — confirmed via `alembic current`
   afterward. This is the specific behavior the task instructed ("if existing invalid
   data is discovered, STOP and report it rather than silently deleting or changing
   data") — verified to actually work, not merely coded and assumed to work.

Both scratch databases were dropped after validation; the shared `dcim_test` database
(already migrated to `0007_correction` earlier in this session for the pytest runs) was
left in its normal, migrated state.

## Performance

**EXECUTED — VERIFIED**, against a dedicated scratch database and a real `uvicorn`
instance (not the master-prompt's full target scale — a modest, honest measurement of
the specific overhead this correction adds, per the task's explicit instruction not to
fabricate benchmark numbers):

```
1. Sequential connection creation (advisory lock uncontended), n=10: avg=11.2ms
   (range 9.5-18.6ms)
2. 5 concurrent connection creations on 5 DISTINCT node pairs: wall=164.4ms,
   all 5 succeeded (201×5) — this is the direct, measured cost of the correction's
   full serialization: roughly 33ms/request when 5 requests contend, versus ~11ms
   uncontended.
3. Downstream traversal over a 200-node linear chain: 206.8ms
4. Same traversal after retiring the chain's leaf node: 192.9ms, 199 nodes (one fewer)
5. Dashboard summary: 25.7ms
```

**Honest disclosure of a new overhead this correction introduces:** the F-H2 retirement
filter adds one extra SQL query per BFS level in every traversal call (a
`non_retired_node_ids` check alongside the existing edge-expansion query) — for the
200-node *linear chain* measured above (the worst case for this specific overhead, since
a chain has 200 levels), this roughly doubles the number of database round trips
`_traverse` makes compared to the pre-correction implementation. This was not present
before this correction and was not present in the original Phase 3 performance pass. It
was **not** re-measured against the original implementation side-by-side in this session
(that would require reverting the fix to get a true A/B comparison, which was not done);
the number above is the corrected implementation's own absolute latency, not a delta.
For a *branching* topology (many children per node, few levels) rather than a long
chain, this overhead is proportional to depth, not breadth, and would be far smaller —
not measured separately in this pass. This is disclosed as a known limitation, not
hidden.

**Not executed:** full master-prompt-scale performance (1,000 racks / 10,000 equipment
/ 5,000 power nodes / 10,000+ connections) — out of scope for this correction per the
design's own classification, unchanged from the original implementation's own
disclosure.

## Browser Validation

**NOT EXECUTED in this implementation pass.** No frontend file was modified (confirmed
by `git status` — none of the 8 changed files are under `frontend/`), and no API
response shape changed for any existing field (confirmed by reading every modified
Pydantic model/response — none were touched; only internal exception-derivation logic,
traversal filtering, and one error-handling code path changed, none of which alter any
JSON field name or type). `npx tsc --noEmit` and `npm run build` were both re-run and
confirmed clean. Given zero frontend changes and zero API contract changes, an
interactive Playwright re-validation was judged unnecessary for this specific
correction and was not performed — this is disclosed honestly rather than claimed. The
independent red-team should still verify the F-H2/F-H1 fixes are visible correctly in
the actual UI (e.g., that `CAPACITY_UNKNOWN` renders sensibly, that a retired node's
`has_upstream_path: false` displays clearly) before treating the frontend as fully
validated for this correction.

## Security Validation

**EXECUTED — VERIFIED** via the full existing authorization/IDOR test suite (unchanged,
re-run as part of the 287-test pass — no authorization logic was touched by this
correction, and none was expected to need changing, since the design explicitly
classified site-scoping as `ACCEPTED CARRY-FORWARD`, not part of this correction's
scope). The one new security-relevant test added is
`test_pdu_outlet_rejects_non_pdu_asset_at_db_level` (F-M3) — a direct database-bypass
attempt (constructing the ORM objects directly, deliberately skipping the API's own
`db.get(PDU, ...)` guard) confirming the database itself, not just the API, now rejects
a `PDUOutlet` attached to a non-`PDU` asset. Malformed input handling was re-verified
for the F-M1 fix specifically (malformed `If-Match` → clean `400`). No new IDOR surface
was introduced (no new endpoint was added; the same nine mutating routes, all still
behind `require_permission`, are the only ones touched). The Phase 1 global-authorization
model was preserved exactly as-is — no site-scoped authorization was introduced,
consistent with the design's explicit instruction.

## Remaining Limitations

Carried forward, honestly, from `PHASE3_CORRECTION_DESIGN.md`'s own "Risks Remaining"
section, re-confirmed still true after implementation:

- The global advisory lock's throughput characteristics were only measured under 2-5-way
  contention in this pass (Performance section above) — not load-tested at higher
  concurrency or realistic production request rates for this endpoint.
- A direct SQL bypass of the advisory lock (or of the unresolved
  `power_node.owning_asset_id` half of F-M3) remains possible for anyone with direct
  database access — explicitly disclosed, not hidden, in both the code's own docstrings
  and this report.
- The F-H2 traversal overhead (one extra query per BFS level) was measured only for a
  200-node linear chain, the worst case for this specific cost — not measured for a
  wide, shallow, branching topology, nor at the master prompt's full target scale.
- Browser/frontend validation was not re-executed this pass (see above) — judged
  unnecessary given zero frontend/contract changes, but not independently confirmed by
  an actual UI walkthrough.
- F-H2's retirement semantics for the individual PDU/UPS/generator/panel node subtypes
  were not each separately re-tested by name beyond the generic-node and PDU-specific
  tests already added — the underlying traversal/allocation logic does not distinguish
  between `ManagedAsset` subtypes, so this is not believed to be a real gap, but it was
  not independently confirmed subtype-by-subtype.
- The `owning_asset_id` half of F-M3 (for `equipment_power_input`/`power_circuit` node
  types) remains unimplemented, exactly as the approved design classified it
  (`REQUIRES ARCHITECTURAL DECISION`) — this is a known, disclosed, intentionally
  deferred gap, not an oversight.
- F-M2's optional `degraded_reason` field remains unimplemented, per the design's own
  deferral.
- Full master-prompt-scale performance, dashboard N+1 latency re-measurement, and
  audit-log field-content verification all remain out of scope for this correction,
  unchanged from the design's own classification.

## Deviations from Correction Design

**None.** Every decision in `PHASE3_CORRECTION_DESIGN.md` was implemented exactly as
specified: the global advisory lock (not SERIALIZABLE, not broader graph-closure
locking); Model A retirement semantics with the root-exemption behavior documented in
the design's Part 8 reasoning; documentation-only treatment for F-M2; the
composite-FK-only implementation for F-M3 (deferring the harder `owning_asset_id` case
exactly as classified); and the six F-M5 log points matching the design's Part 12 table.
No implementation detail required an architectural substitution — the repository-wide
mutation-path search confirmed the design's single-mutation-path assumption was correct,
and no schema difference from the design's assumptions was found that would have
required stopping to investigate a mismatch.

One minor implementation-level clarification not fully pinned down by the design
document itself (not a deviation, a necessary specification decision the design left
open): whether a retired node's own traversal *root* query should be filtered the same
way as an intermediate node. This was resolved as "no, the root is exempt" (preserving
historical/audit inspection), documented extensively in the code's own docstrings in
three places, and covered by a dedicated test
(`test_retired_traversal_root_still_shows_its_own_direct_neighbor`) whose first draft
initially asserted the opposite outcome and had to be corrected to match this documented
decision — recorded here for transparency about how that ambiguity was actually
resolved during implementation, not merely inherited from the design.
