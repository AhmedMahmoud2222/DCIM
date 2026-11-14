# Phase 3 Correction Design — Scope Companion

Companion to `PHASE3_CORRECTION_DESIGN.md`. This document records exactly
what was inspected, what was experimentally verified, what was reasoned
analytically, and what was left untouched, so a reviewer does not have to
reverse-engineer the evidence trail from the main design document's prose.

## Commit Audited

`4606810586084511a6b3992050baca63c5aedbf6` (unchanged since the original
hostile self-audit; this design session did not create a new commit and
did not move `HEAD`).

## Prior Documents Read in Full Before Starting

- `PHASE3_HOSTILE_SELF_AUDIT.md`
- `PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md`
- `PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md` (specifically §7, the
  original NEW-1 writeup, re-read in this session to get the exact root
  cause and recommended remediation direction rather than relying on the
  hostile audit's own summary of it).

## Findings Covered

F-C1, F-H1, F-H2, F-M1, F-M2, F-M3, F-M5, and Phase 1 NEW-1, per the task's
explicit required list — plus a full reassessment/classification pass over
every other finding in `PHASE3_HOSTILE_SELF_AUDIT.md` (dashboard N+1,
full-scale performance, migration validation, browser validation, audit
verification, NaN/Infinity handling, 3+-transaction races, multi-tenant
concerns, `POWER_TOPOLOGY_INVALID`, the CASCADE/RESTRICT FK landmine,
`redundancy_factor`), tabulated in Part 14 of the main design document.

## Files Inspected (Re-Read for This Design, Not Assumed from the Prior
## Audit's Summary)

- `backend/app/domain/power/models.py`
- `backend/app/application/power_graph.py`
- `backend/app/application/power_capacity.py`
- `backend/app/api/v1/power.py`
- `backend/app/api/v1/dashboard.py`
- `backend/app/application/concurrency.py`
- `backend/app/application/idempotency.py`
- `backend/app/core/errors.py`
- `backend/migrations/versions/0006_phase3_power_topology_and_capacity.py`
- `PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md` (§7, NEW-1)

Rack/equipment power-relationship code (`RackDetailPage.tsx`,
`EquipmentDetailPage.tsx`, the frontend `api.ts`) was **not** re-read in
this design session — the hostile audit's own prior review of these
(source-of-truth discipline, no client-side recalculation) is taken as
still valid, since none of the eight required findings touch the
frontend directly.

## Experiments Actually Performed (EXECUTED — VERIFIED)

All three ran against the real `dcim_test` PostgreSQL 16 database, using
the application's own `power_graph.py` functions directly (not mocked),
with `asyncio.Event`-forced interleaving to guarantee the exact race
condition on every trial rather than hoping for it by chance.

1. **Baseline reconfirmation** of the F-C1 race under plain READ
   COMMITTED with no candidate fix — reconfirmed the cycle forms (both
   new edges committed).
2. **Option A (SERIALIZABLE isolation), 5 trials** — one transaction
   aborted with SQLSTATE `40001` in every trial; no cycle ever committed
   across all 5.
3. **Option B (global `pg_advisory_xact_lock`), 5 trials** — transactions
   fully serialized (observed wait ≈77ms matching the first transaction's
   simulated hold time); the second transaction's cycle check, now
   running against fully-committed state, correctly rejected itself via
   the existing `WouldCreateCycle` exception in every trial; no cycle
   ever committed, and no new error class needed handling.

A verification bug was found and corrected **during** this design
session's own work: the first draft of the cycle-presence check (`is N1
reachable downstream of N2?`) is not, by itself, proof of the specific
4-cycle under test in this chosen topology, because the pre-existing
edges alone plus *either* new edge already make N1 "reachable from N2"
through the surviving chain — the corrected check (used in the results
above) instead verifies that **both** new edges are simultaneously present
in the committed graph, which is the actual, unambiguous test for this
specific 4-cycle. This same subtlety does not invalidate the original
hostile audit's F-C1 finding (there, both edges genuinely did commit, so
the conclusion holds either way) — it is recorded here as a methodological
correction for anyone reusing this experimental technique going forward.

## Experiments NOT Performed (NOT EXECUTED)

- **Option C (broader deterministic graph-closure locking)** — no working
  prototype was built; rejected by analytical reasoning alone (Part 4 of
  the main design document explains why a prototype was not considered a
  good use of this session's time — the phantom-discovery problem it
  would need to solve either reduces to Option A or introduces new
  unbounded complexity).
- **3+-concurrent-transaction cycle races** — only the minimal 2-transaction
  case was built and run; the N-way generalization of Option B's
  correctness is an analytical conclusion (a single global mutex admits
  no concurrent execution regardless of how many contenders there are),
  not independently executed against, say, a 4-transaction/6-node
  scenario.
- **Throughput/latency under sustained load** (10+ simultaneous
  connection-creation requests, or realistic production request rates for
  this endpoint) — both experiments above tested exactly 2-way contention;
  no load test was run.
- **Genuine Postgres deadlock (`40P01`) provocation** under either
  candidate option.
- **A real HTTP-layer retry loop** for Option A — the experiments called
  `power_graph.py`'s functions directly, not through
  `create_power_connection`'s full FastAPI route, so no API-level
  `40001`-catching/retry code was built or tested (Option A was not
  selected, so this was not pursued further once Option B's cleaner
  result was in hand).
- **F-H1's fix** — no code was written or tested; the precedence logic in
  Part 7 of the main design is a specification, not an implementation.
- **F-H2's fix** — same; Model A is a recommendation, not implemented or
  tested against the actual traversal code.
- **F-M1's fix** — same; the shared-helper substitution is described, not
  applied.
- **F-M3's composite-FK mechanism** — described and reasoned through
  (including why it works given `ManagedAsset.asset_type` already
  exists), but no migration was written or run to confirm it actually
  behaves as described against a real PostgreSQL instance.
- **Duplicate `If-Match` HTTP headers** — explicitly noted in the main
  design (Part 9) as unexplored, out of scope for this pass.
- **NaN/Infinity capacity value handling** — not tested; the main design
  flags this as `REQUIRES ARCHITECTURAL DECISION` rather than assuming
  Pydantic's defaults are either sufficient or insufficient.

## Evidence Classification Legend (as used throughout the main document)

- **EXECUTED — VERIFIED**: an experiment was actually run against a real
  PostgreSQL instance in this session, and the reported result is its
  actual output.
- **ANALYTICAL CONCLUSION**: a reasoned argument, not an experiment —
  stated as such wherever used, most prominently the N-way generalization
  of Option B and the rejection of Option C.
- **NOT EXECUTED — STATIC ANALYSIS ONLY** / **NOT EXECUTED**: explicitly
  named wherever a claim is made without having run anything to confirm
  it — this includes essentially all of the actual *fix* behavior
  described in Parts 7-12, since none of it was implemented in this
  design-only session.

## Files Explicitly NOT Modified

Every application file, test file, migration file, Docker/CI
configuration file, and frontend file in the repository. Confirmed by
`git status`/`git diff` immediately before and after this session's work
(see the main design document's own git-safety confirmation, reproduced
below).

```
git status --short
?? PHASE3_CORRECTION_DESIGN.md
?? PHASE3_CORRECTION_DESIGN_SCOPE.md
?? PHASE3_HOSTILE_SELF_AUDIT.md
?? PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md

git diff --stat
(empty)
```

The two hostile-audit files were already untracked from the prior session
and remain untouched by this one (not re-edited). Only the two new design
documents were added.

## Temporary Diagnostic Files Created and Removed

- `/tmp/.../scratchpad/design_experiment_serializable.py` — created, run,
  then deleted before this document was written.
- `/tmp/.../scratchpad/design_experiment_advisory_lock.py` — created, run,
  then deleted before this document was written.
- A leftover `/tmp/.../scratchpad/hostile_audit_cycle_race.py` from the
  *prior* session (already outside the repository, already not subject to
  the no-permanent-files rule) was also deleted during this session's
  cleanup pass, since it was no longer needed as a reference.

All three were outside the repository at all times; none were ever staged
or committed.

## Limitations of This Design Exercise

- This is a design produced by the same agent that implemented the
  original Phase 3 code and performed the original hostile self-audit —
  it carries the same self-review limitation the hostile audit itself
  disclosed: it is not independent validation of either the original
  defects or this design's proposed corrections.
- The two chosen-and-tested options (A and B) were compared primarily on
  correctness and a narrow 2-way concurrency/latency measurement — a full
  production capacity-planning exercise (expected real-world connection-
  creation request rate, acceptable p99 latency budget for this endpoint)
  was not performed and would strengthen the throughput argument in
  Option B's favor beyond what this session's timing numbers alone
  support.
- Every fix described for F-H1, F-H2, F-M1, F-M3, and F-M5 is a
  specification, not working code — the actual implementation could
  surface details (naming conflicts, an existing test that asserts the
  old, incorrect behavior on purpose, a migration edge case) that this
  design did not and could not anticipate without writing the code.
- No frontend impact analysis beyond the brief note in Part 7 (no
  response-shape change expected) was performed for any of the six
  `FIX IN CORRECTION` items — the implementation phase should re-verify
  each one against the actual UI, not just the API contract.

This document, like the main design it accompanies, does not approve
Phase 3 or any part of this correction plan. It is a record of what this
design session did and did not establish, for the human reviewer's next
decision.
