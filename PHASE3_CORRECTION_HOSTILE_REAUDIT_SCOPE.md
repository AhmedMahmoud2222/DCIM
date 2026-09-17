# Phase 3 Correction Hostile Re-Audit — Scope Companion

Companion to `PHASE3_CORRECTION_HOSTILE_REAUDIT.md`. Records exactly what was executed,
what environment was used, and what was explicitly not covered, per the evidence
standard the task required.

## Commit Audited

`46586423a367b48a13a1975ecd5e0a2a56e19337`, parent `4606810586084511a6b3992050baca63c5aedbf6`.
Confirmed via `git rev-parse HEAD` / `git rev-parse HEAD^` at the start of this audit.
Working tree was clean before this audit began and after every intermediate step until
the two audit documents themselves were created.

## Documents Read in Full Before Beginning

- `PHASE3_HOSTILE_SELF_AUDIT.md` / `PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md`
- `PHASE3_CORRECTION_DESIGN.md` / `PHASE3_CORRECTION_DESIGN_SCOPE.md`
- `PHASE3_CORRECTION_IMPLEMENTATION.md`

## Environment

- PostgreSQL 16 (started via `sudo service postgresql start`), Redis (started via
  `redis-server --daemonize yes`), both required manual startup at the beginning of
  this session (not running by default in this container).
- The shared `dcim_test` database (used by the pytest suite) was already migrated to
  `0007_correction` from the prior implementation session — confirmed via
  `alembic current` before running the suite, not assumed.
- A dedicated scratch database, `dcim_reaudit`, was created fresh for every hostile
  HTTP/concurrency/DB-bypass experiment in this audit, with its own bootstrapped
  `DCIM Manager` and `Viewer` role users. Dropped at the end of this session.
- A dedicated `uvicorn` instance was run against `dcim_reaudit` on port 8020 for the
  duration of the HTTP-level experiments, then killed.
- Two temporary diagnostic scripts were created under
  `/tmp/.../scratchpad/` (outside the repository): `reaudit_fc1.py` (F-C1 attack suite)
  and `reaudit_fh1_fh2.py` (F-H1/F-H2 hostile cases). Both were deleted before this
  audit concluded, along with a leftover `correction_perf_check.py` from the prior
  implementation session's own cleanup that had not yet been removed.

## Experiments Actually Performed (EXECUTED — VERIFIED)

1. Full backend test suite: `pytest -q tests/` from `/home/user/DCIM/backend` — `287
   passed, 6 warnings in 71.58s`.
2. `git diff --stat` between the original Phase 3 baseline and the audited commit,
   independently confirming the changed-file list.
3. Direct source reading of `power_graph.py` and `power.py`'s `create_power_connection`
   to confirm lock-acquisition ordering, not trusting the implementation report's
   description.
4. Repository-wide re-search for every possible `PowerConnection` mutation path
   (`grep`/`find` across `app/`, `scripts/`, `migrations/`).
5. Widened-timing block proof: a raw `asyncpg` connection held the advisory lock for
   2 seconds while a real HTTP request attempted to acquire it; the request's actual
   completion time (2.022s) was measured, not assumed.
6. `pg_locks` system-view inspection while the lock was held, from a second raw
   connection.
7. Rollback-releases-the-lock proof: held, rolled back, re-acquired from a second
   connection, timed.
8. The exact F-C1 hostile race, reproduced via real concurrent HTTP requests, 10
   independent trials, using a freshly-written script (not the implementation's own
   test file).
9. Reversed-submission-order variant of the same race.
10. A fresh 3-way triangle race.
11. A mixed shared-endpoint-plus-disjoint-pairs 5-way concurrent scenario.
12. Connection creation racing an unrelated disconnect.
13. A manually-inserted (direct SQL) 3-cycle, then traversed via the real API to
    confirm safe, bounded, non-looping behavior.
14. A new edge created touching the manually-cyclic component, confirming the app
    correctly evaluates only the new edge's own cycle status.
15. Throughput measurement at 10, 50, and 100 concurrent connection-creation requests.
16. F-H1 independent re-test: a 30-node natural (non-monkeypatched) unresolved-chain
    scenario, confirming `CAPACITY_UNKNOWN` fires via a path the implementation's own
    tests did not construct.
17. F-H2 cases A, B, C, D, E, G, I independently reproduced via fresh HTTP calls.
18. F-M1: 4 malformed `If-Match` variants, valid, stale, zero, negative, all
    independently re-tested via fresh HTTP calls.
19. Direct raw-SQL bypass attempt against the F-M3 composite FK (non-PDU asset
    attached to a `pdu_outlet`) — confirmed rejected at the database level.
20. Direct raw-SQL bypass attempt against the still-open `owning_asset_id` gap —
    confirmed it still succeeds (gap open, as designed).
21. Audit-log/outbox-event count cross-check: `SELECT count(*) ... GROUP BY action`
    against `audit_log` and `outbox_event`, compared to the actual `power_connection`
    row count, confirming an exact 1:1 correspondence (246 creates = 246 audit rows =
    246 of the 247 outbox events, plus 1 disconnect matching the remaining event) after
    a large number of successful and rejected attempts across this entire session.
22. Authorization/IDOR spot-check: unauthenticated, Viewer-role mutation attempts,
    Viewer-role read access, nonexistent UUID, malformed UUID — all via fresh HTTP
    calls against a freshly bootstrapped Viewer user.
23. Dashboard summary timing at the scale accumulated by this session's own testing
    (438 power nodes at time of measurement): 69.7ms.

## Experiments NOT Performed (NOT EXECUTED, explicitly)

- Multiple separate OS-level application processes (only one `uvicorn` worker was
  run for the HTTP-level experiments).
- Physical database connection interruption or process-crash simulation (reasoned
  about analytically, per standard PostgreSQL advisory-lock semantics, not physically
  reproduced).
- Physical PostgreSQL restart.
- The real, unmonkeypatched `MAX_TRAVERSAL_NODES=5000` bound for F-H1 (building 5,001
  real rows serially through the HTTP API was judged too time-costly for this audit
  pass).
- F-H2 Case H (retired downstream node, as distinct from the already-tested leaf case)
  as its own separate construction.
- Frontend/browser interactive validation (no frontend file was touched by the
  correction; judged lower priority than the backend hostile-attack work this task
  emphasized, given the session's time budget — disclosed as a standing gap, not
  resolved).
- Generated-column behavior under a `managed_asset.asset_type` mutation or a
  non-trivial cascade-delete scenario beyond simple row creation.
- Decomposition of the gap between uncontended (~11ms) and 100-concurrent-derived
  average (~55-60ms) per-request latency into its constituent causes (lock queueing vs.
  connection-pool contention vs. diagnostic-script overhead).
- Full master-prompt-scale performance (1,000/5,000/10,000+ node/connection counts) —
  reasoned about analytically only, consistent with every prior document's own
  disclosure of this same limitation.
- Re-execution of the migration pre-flight-safety-check test (manufactured invalid
  data aborting the migration) — this was verified once already, in the implementation
  session, and this audit chose not to spend its budget re-confirming an
  already-demonstrated, low-risk mechanism, prioritizing the composite-FK bypass
  re-test instead.

## Evidence Classification Legend (as used throughout the main report)

- **EXECUTED — VERIFIED**: an experiment was actually run in this audit session
  against real PostgreSQL/Redis/uvicorn, and the reported result is its actual,
  observed output.
- **STATIC ANALYSIS**: a conclusion drawn from reading source code directly in this
  audit session, not from running anything.
- **ANALYTICAL CONCLUSION**: a conclusion reasoned from documented PostgreSQL/
  transaction semantics, not independently executed in this session.
- **NOT EXECUTED**: explicitly named wherever this audit did not attempt something the
  task requested, with the reason given inline.

## Files Explicitly Not Modified

Every application file, test file, migration file, Docker/CI configuration file, and
frontend file in the repository. Confirmed by `git status` (clean) and `git diff`
(empty) immediately before this document and its companion were created, and again
immediately after, with only the two new audit-document files appearing as untracked.

## Limitations of This Re-Audit

- Performed by the same agent that designed, implemented, and previously
  self-audited this correction — not independent by any measure. Its value is in the
  specific, adversarial, evidence-based attempts to break the correction's own claims,
  not in any claim of impartiality.
- Time-boxed: the experiments prioritized F-C1 (the previously CRITICAL finding) most
  heavily, consistent with the task's own instruction that this is "the most important
  section" — other areas (frontend, full-scale performance, migration edge cases)
  received proportionally less direct re-execution and lean more on static analysis or
  explicit non-execution disclosure.
- No experiment in this session was run more than once with different random
  seeds/timings beyond the explicit repeated-trial counts stated (10 for the main F-C1
  race) — a still-larger repeated-trial count could in principle surface a
  lower-probability race this audit's specific sample size did not encounter; this is
  the ordinary limitation of any finite-sample concurrency testing, not a specific
  known gap in this correction.
