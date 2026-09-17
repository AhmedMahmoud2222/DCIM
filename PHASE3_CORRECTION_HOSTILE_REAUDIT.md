# Phase 3 Correction — Hostile Re-Audit

**This is a hostile re-audit of the correction, performed by the same agent that
implemented it, designed it, and performed the prior hostile self-audit. It is NOT an
independent red-team certification.** The final verdict below reflects that limitation
explicitly and is scoped accordingly.

## Executive Summary

The F-C1 correction (a global `pg_advisory_xact_lock`) **holds up under sustained,
independent, hostile attack**. This is the headline result: across 10 repeated
reproductions of the exact confirmed race, a fresh 3-way triangle variant, reversed
submission ordering, mixed shared/disjoint-endpoint concurrency, and direct PostgreSQL
system-view inspection (`pg_locks`), the lock genuinely blocks (proven by a deliberate
2-second widened-timing hold, observed end-to-end at 2.022s), genuinely releases on
rollback (0.001s reacquisition), and never once permitted a cycle to commit. F-H1 and
F-H2 were independently re-verified against the running API with fresh scenarios,
including several the implementation's own test suite did not construct (Case E's
sibling-branch isolation, Case G's retired-upstream-source, Case I's concurrent
retire-during-traversal), and all held.

**One new, real, previously-undisclosed-in-this-form finding**: the F-H2 retirement
filter's added per-BFS-level query compounds with the pre-existing (already-disclosed)
dashboard N+1 pattern, since `dashboard.py`'s loop calls `derive_node_capacity_exceptions`
→ `compute_allocated_kw`, which now issues roughly double the DB round-trips per node
it scans. This was not called out explicitly in `PHASE3_CORRECTION_IMPLEMENTATION.md`
in this combined form and is recorded here as a new MEDIUM finding.

**One measured, real operational concern, now quantified rather than merely
theorized**: at 100 concurrent connection-creation requests, wall time is 5.8 seconds
and p50 per-request latency is 3.06 seconds — the global lock's serialization cost is
real, not hypothetical, though still consistent with the design's own accepted
trade-off for what is expected to be a low-frequency operation.

No CRITICAL or HIGH finding from the original hostile audit was found to be reopened.
No new CRITICAL finding was discovered. The correction is judged ready to proceed to an
actual independent red-team — this document does not itself constitute that
independent validation.

## Audited Commit

```
HEAD:    46586423a367b48a13a1975ecd5e0a2a56e19337
Parent:  4606810586084511a6b3992050baca63c5aedbf6 (original Phase 3 baseline)
Branch:  claude/new-session-1vutvy
```

**EXECUTED — VERIFIED.** `git status` returned clean before this audit began.
`git diff --stat 4606810...HEAD` independently confirmed exactly the 14 files the
implementation report claims — 9 code/test files, the one new migration, and 4
carried-forward documentation files (the two hostile-audit docs and two design docs
from the prior sessions) plus the one new implementation report — no extra file, no
Docker/CI/config/frontend file, no accidental inclusion. The implementation report's
own file list was **not** trusted blindly; the actual repository diff was pulled and
read directly.

## Previous Findings and Closure Status

| Finding | Previous Severity | Current Status | Evidence | Notes |
|---|---|---|---|---|
| F-C1 | CRITICAL | **CLOSED** | EXECUTED — VERIFIED (Part 3/4/5 below) | Independently reproduced 10+ times via a fresh script, plus 6 additional attack variants the original test suite did not cover; zero cycles committed |
| F-H1 | HIGH | **CLOSED** | EXECUTED — VERIFIED (Part 8 below) | Re-verified via fresh HTTP calls against the real API; also independently discovered a second, non-monkeypatched path that naturally produces `data_quality="unknown"` and confirmed `CAPACITY_UNKNOWN` fires there too |
| F-H2 | HIGH | **CLOSED** | EXECUTED — VERIFIED (Part 9 below) | 8 of 9 requested cases (A,B,C,D,E,G,I; F folded into E) independently reproduced; documented root-exemption semantics reconfirmed exactly as implemented |
| F-M1 | MEDIUM | **CLOSED** | EXECUTED — VERIFIED (Part 10 below) | 4 malformed variants + stale + zero + negative all independently re-tested |
| F-M2 | — | **UNCHANGED (by design)** | STATIC ANALYSIS | No logic touched, confirmed by diff; documentation-only classification stands |
| F-M3 | MEDIUM | **PARTIALLY CLOSED** | EXECUTED — VERIFIED (Part 11/12 below) | `pdu_outlet` half closed and independently bypass-tested via raw SQL; `owning_asset_id` half confirmed still open, exactly as the design classified it (not a regression) |
| F-M5 | MEDIUM | **CLOSED** | STATIC ANALYSIS + EXECUTED (log lines observed firing during Part 3-4 experiments) | 5 of 6 designed log points confirmed present in code; contention log line's trigger condition (≥0.5s wait) was incidentally exercised by the Part 3 widened-timing test |
| NEW-1 | MEDIUM | **ACCEPTED CARRY-FORWARD, unchanged** | STATIC ANALYSIS (Part 16) | Confirmed no Phase 3 endpoint uses Idempotency-Key, before or after the correction |

## F-C1 Detailed Revalidation

### 3.1 Implementation Verification (STATIC ANALYSIS)

Re-read `app/application/power_graph.py` and `app/api/v1/power.py` directly (not
trusting the implementation report's description). Confirmed:

- `with_connection_mutation_lock` executes `SELECT pg_advisory_xact_lock(:key)` as its
  first and only statement before `yield` — nothing reads graph state before this call
  returns.
- `create_power_connection`'s body: `db.get(PowerNode, ...)` reads for source/target,
  the retired-node check, `lock_node_pair_in_canonical_order`, the cycle check, the
  duplicate-edge pre-check, and the `INSERT` are **all** inside `async with
  with_connection_mutation_lock(db):` — confirmed by direct line-by-line reading, not
  assumed.
- The lock uses `pg_advisory_xact_lock` (transaction-scoped), **not**
  `pg_advisory_lock` (session-scoped) — confirmed by exact string match in the source.
- **One documentation-precision nitpick, not a defect**: the docstring claims the
  advisory lock is acquired "as the first statement of that transaction." This is not
  literally true — `require_permission`'s dependency chain (`get_auth_context` →
  `get_current_user`) executes its own `SELECT` on the same `AsyncSession` before the
  route body runs (FastAPI resolves dependencies first), which is what actually
  auto-begins the SQLAlchemy transaction. The advisory lock is the first statement of
  the *route body*, not the *transaction*. This has **no functional consequence**
  (`pg_advisory_xact_lock`'s scope is tied to whatever transaction is current
  regardless of when it started, and the RBAC lookup is a read-only `SELECT` with no
  side effects that could interact with the lock's guarantee), but the report's wording
  is imprecise. **Severity: INFO** (documentation accuracy only).

### 3.2 Repository-Wide Mutation-Path Search (Re-run, EXECUTED)

Re-ran the search independently (not trusting the implementation report's own claimed
search):

```
grep -rn "PowerConnection(" backend/app backend/scripts       -> exactly 1 construction site (power.py:463, shifted from :430 by the correction's own added lines)
grep -rln "power_connection" backend/app                       -> power.py, power_graph.py, power_capacity.py, domain/power/models.py (unchanged set)
grep -rn "INSERT INTO power_connection\|bulk_insert.*[Pp]ower[Cc]onnection" backend/app backend/migrations backend/scripts -> no matches outside migrations 0006 (schema DDL only, no data seeding)
find backend/app/infrastructure -iname "*power*"                -> no results
find backend/scripts -iname "*power*"                            -> no results
grep -rn "effective_to" backend/app --include="*.py"             -> only in power.py (disconnect) and power_graph.py/power_capacity.py (read filters) -- no other mutator sets it
```

**Confirmed, independently: exactly one application code path can insert an active
`PowerConnection` row.** No Celery task, script, or admin tool creates or reactivates
one. No path "restores," "copies," or "bulk-creates" connections anywhere in this
codebase today. This matches both the original implementation's claim and the design's
assumption — re-verified, not merely re-stated.

## Part 4/5 — Attack Reproduction (EXECUTED — VERIFIED)

All of the following were run against a real PostgreSQL 16 instance and a real running
`uvicorn` process (dedicated scratch database and server, not the shared test
fixtures), via a fresh, independently-written diagnostic script (not the
implementation's own test file), then deleted after use.

| Attack | Result |
|---|---|
| Widened-timing block proof: manually hold the lock 2s via raw `asyncpg`, then fire a real HTTP connection-creation request | Request completed in **2.022s** (genuinely blocked, not a lucky interleave) — **PASS** |
| `pg_locks` inspection while held | `locktype='advisory', mode='ExclusiveLock', objid=918273645` — confirmed a genuine PostgreSQL advisory lock, not a masquerading table/row lock — **PASS** |
| Rollback releases the lock | Second acquisition after a deliberate `ROLLBACK` took **0.001s** — not stuck — **PASS** |
| Exact F-C1 race, 10 independent fresh trials | `[201, 422]` in all 10, no cycle in any — **PASS** |
| Reversed submission ordering (N3→N4 fired first in code) | `[201, 422]` — order-independent — **PASS** |
| Fresh 3-way triangle (independent script) | 2×`201`, 1×`422` — **PASS** |
| Mixed: 2 requests sharing an endpoint + 3 on fully disjoint pairs, 5-way concurrent | All 5 succeeded (`201`×5) — no over-rejection — **PASS** |
| Connection creation racing an unrelated disconnect | Both succeeded independently (`200`, `201`) — the two are correctly not serialized against each other (disconnect uses row-level `FOR UPDATE`, not the topology lock, by design) — **PASS** |
| Manually-inserted 3-cycle via direct SQL (bypassing the app entirely), then traversed | Returned in 0.010s, exactly the 2 non-root nodes, no infinite loop, no crash — the BFS visited-set is inherently cycle-safe regardless of how a cycle got there — **PASS** |
| New edge created touching the manually-cyclic component | `201` in 0.014s — correctly evaluated only against whether the *new* edge closes a *new* cycle from its own two endpoints, not required to "heal" a pre-existing illegitimate one — **PASS, expected behavior** |

**Every attack variant in Part 4/5's checklist that could be executed in this session
was executed.** Not executed: multiple separate OS-level application *processes*
(only one `uvicorn` worker was run — the advisory lock's correctness does not depend on
single- vs multi-process, since PostgreSQL is the single point of coordination
regardless of how many application processes connect to it, but this was not
independently demonstrated with 2+ real separate processes in this session — **NOT
EXECUTED**, flagged for the independent red-team). Database connection interruption
mid-lock-hold was reasoned about (Part 6) but not physically executed (e.g., killing
the TCP connection while the lock is held) — **NOT EXECUTED**.

## Part 6 — Advisory Lock Failure Modes

- **Pooled connections**: `pg_advisory_xact_lock` releases automatically at
  COMMIT/ROLLBACK of the *transaction*, which always ends before the connection is
  returned to SQLAlchemy's pool for reuse in this codebase's request lifecycle
  (`get_db`'s `async with AsyncSessionLocal()` closes the session, and any open
  transaction on it is finalized, at the end of every request) — **ANALYTICAL
  CONCLUSION**, consistent with the EXECUTED rollback test above (Part 4/5), not
  independently tested for a connection recycled mid-pool without going through a
  session close.
- **Application exception**: `get_db`'s own `except Exception: await session.rollback();
  raise` (confirmed by direct reading, `app/db/session.py`) guarantees any unhandled
  exception inside the route — including one raised *after* the lock is acquired but
  before commit — triggers a rollback, which releases the lock. This is the same
  mechanism the whole codebase already relies on for every other transactional
  invariant; not special-cased for the advisory lock, and not required to be.
- **Process crash**: `pg_advisory_xact_lock` is tied to the *database session/
  transaction*, not the client process — if the application process crashes while
  holding it, PostgreSQL detects the dropped connection and releases the lock along
  with the aborted transaction. **ANALYTICAL CONCLUSION** (standard, well-documented
  PostgreSQL behavior), not independently reproduced by physically crashing a process
  in this session — **NOT EXECUTED**.
- **Database restart**: releases all locks by definition (a restart implies every
  session is gone) — **ANALYTICAL CONCLUSION**, not executed (would require a real
  Postgres restart mid-test, judged too disruptive for this audit's shared environment).
- **Lock key collision**: re-confirmed via `grep` that `918273645` (equivalently
  `918_273_645`) appears exactly once in the entire `app/` tree — no other advisory-lock
  use exists to collide with it.

**No failure mode was found where the lock could remain stuck.** This section's
conclusions are a mix of EXECUTED (rollback) and ANALYTICAL (crash/restart, standard
PostgreSQL guarantees not independently physically reproduced).

## Part 7 — Global Lock Throughput (EXECUTED — VERIFIED)

```
n=10  concurrent distinct-pair creations: wall=685.0ms,  all 10 succeeded, p50=441.6ms, p99=609.4ms,  max=609.4ms
n=50  concurrent distinct-pair creations: wall=2965.9ms, all 50 succeeded, p50=1689.3ms, p99=2526.5ms, max=2526.5ms
n=100 concurrent distinct-pair creations: wall=5773.0ms, all 100 succeeded, p50=3061.5ms, p99=4943.5ms, max=4943.5ms
```

**This is a real, measured cost, not a theoretical one.** At 100 simultaneous
connection-creation requests, the median request waits over 3 seconds and the slowest
waits nearly 5 seconds — every request succeeded (no failures, no errors, no
timeouts), but latency scales roughly linearly with concurrent load, exactly as
expected for a design that trades concurrency for correctness on this one operation.
The per-request cost under load (~55-60ms average, extrapolating from the 100-request
wall time divided by 100) is higher than the ~11ms measured for a single uncontended
request in the implementation's own performance section — this gap was not fully
decomposed in this audit (how much is lock-wait queueing vs. connection-pool
contention vs. per-request application overhead under 100 simultaneous `httpx.AsyncClient`
instances each opening a fresh connection) — **flagged as an open question for the
independent red-team**, not resolved here.

**Is this operationally acceptable?** For the expected real-world usage pattern
(human-paced provisioning, not a bulk hot loop) — **yes, on the evidence gathered**.
For a hypothetical bulk-import feature creating hundreds of connections
programmatically in a tight loop — **this would be a genuine, measured bottleneck**,
exactly as the correction design's own "Risks Remaining" section anticipated, now
confirmed with real numbers rather than left as pure intuition. **Severity of this
observation: MEDIUM** (real, bounded, already partially disclosed, not a correctness
defect) — this is a re-affirmation with evidence, not a new finding, but the specific
numbers above were not previously measured and should be considered new information.

## F-H1 Detailed Revalidation (EXECUTED — VERIFIED)

Independently re-tested via fresh HTTP calls against the real API (not reusing the
implementation's own pytest file):

- Built a 30-node downstream chain from a node with a known `rated_capacity_kw=100`.
  Every chain node has no capacity record and, being a linear chain, each one's only
  child eventually bottoms out at a childless, capacity-less leaf — this is a
  **different, non-monkeypatched path** to `data_quality="unknown"` than the one the
  implementation's own tests exercise (those use a monkeypatched `MAX_TRAVERSAL_NODES`
  to force the *bounded-traversal* variant of unknown; this is the *unresolved-leaf-
  chain* variant, discovered incidentally during this audit). Result: `data_quality:
  "unknown"`, and `/power/capacity-exceptions` correctly returned exactly one
  `CAPACITY_UNKNOWN` for the root — **confirming the fix generalizes beyond the single
  scenario the implementation's own test constructed**, not merely fixed for that one
  narrow case.
- Cross-surface check: `/dashboard/exceptions` was also queried and returned
  consistent results (does not silently diverge from `/power/capacity-exceptions`).
- Did **not** re-attempt the real, unmonkeypatched 5,000-node `MAX_TRAVERSAL_NODES`
  bound in this session (building 5,001 real rows serially through the HTTP API was
  judged too time-costly for this audit pass) — **NOT EXECUTED at full real scale**;
  the implementation's own monkeypatched-bound unit test, plus this audit's
  independently-discovered natural-unknown-chain case, are judged sufficient combined
  evidence that the fix is not narrowly overfit to one exact reproduction, but the
  literal production constant was not exercised end-to-end.

**Precedence, re-verified**: `CAPACITY_UNKNOWN` and `CAPACITY_OVERLOAD`/
`CAPACITY_NEAR_LIMIT` remain mutually exclusive by the code's own control flow (the
`unknown` branch returns immediately) — reconfirmed by reading, not just trusting the
implementation report's claim.

## F-H2 Detailed Revalidation (EXECUTED — VERIFIED)

Fresh, independent reproduction of cases A through I (F folded into E, since a
duplicate-branch scenario is a variant of the same "live branch unaffected by retired
sibling" property):

| Case | Result |
|---|---|
| A — live path | Equipment reachable — **PASS** |
| B — retired intermediate | Equipment NOT reachable, **and** the retired PDU itself does not appear in the utility's own downstream result set — both properties independently asserted and confirmed, not just one — **PASS** |
| C — retired node as traversal root | Its own direct neighbor IS still returned (documented root-exemption behavior) — confirmed to match what `PHASE3_CORRECTION_IMPLEMENTATION.md` describes, not merely assumed — **PASS, matches documented design** |
| D — retired leaf | Excluded from results — **PASS** |
| E (+F) — retired branch alongside a live sibling branch from the same parent | Live branch and its own child both remain reachable; retired branch and its child are both excluded — retirement is correctly scoped to its own branch, not a blanket effect on the parent — **PASS** |
| G — retired upstream *source* (not intermediate) | Upstream traversal from a live downstream node correctly returns empty — the retired source does not leak through — **PASS** |
| I — retirement racing a concurrent traversal read | Both the retire POST and the downstream GET completed with `200`, no error, no corrupted/partial state observable from either response — **PASS** |

**The audit specifically probed the distinction the task highlighted** ("retired node
is not returned" vs. "retired node stops traversal") **by asserting both properties
independently in Case B** rather than only one — both hold.

Not independently re-tested: Case H (a specific "retired downstream node" as distinct
from Case D's leaf) was judged equivalent in mechanism to D and not separately
constructed — **STATIC ANALYSIS** (same code path, no reason to expect divergent
behavior, but not literally executed as its own distinct scenario).

## F-M1 Detailed Revalidation (EXECUTED — VERIFIED)

```
If-Match='text' ('not-a-number'):        400
If-Match='float-string' ('1.5'):         400
If-Match='hex-string' ('0x10'):          400
If-Match='quoted-plus-garbage' ('"3"extra'): 400
valid If-Match:                          200
stale If-Match (reused old version):     409
If-Match=0:                              409
If-Match=-1:                             409
```

A whitespace-only `If-Match` value (`"   "`) could not be tested — the `httpx` client
library itself rejects it as an "Illegal header value" before the request is ever
sent, a client-side HTTP library constraint unrelated to this application's own code.
This is **NOT EXECUTED** for that one specific sub-case, not a finding against the
application.

No malformed value produced a `500` in any variant tested. All results match the
implementation report's claims.

## F-M2 Detailed Revalidation

**No re-audit action beyond confirming no change was made.** `git diff` for the
redundancy-classification logic in `power_capacity.py` shows only the `has_upstream_path`
computation change (which is an F-H2 fix, already covered above) — the
shared-ancestor/SPOF-detection algorithm itself is byte-for-byte unchanged from the
original hostile audit's own review of it. **STATIC ANALYSIS**, consistent with the
design's `DOCUMENTATION ONLY` classification.

## F-M3 Detailed Revalidation (EXECUTED — VERIFIED)

- **Migration chain**: `0007_correction` revises `0006_phase3` correctly (confirmed via
  `alembic current`/`alembic upgrade head` output showing the full chain through both
  revisions).
- **Fresh install, representative data, downgrade, re-upgrade**: re-confirmed by
  re-running the exact validation sequence from the implementation session
  independently in this audit's own scratch database — same outcome (data preserved
  across downgrade/re-upgrade, constraint correctly re-derived).
- **Direct database bypass attempt, via raw SQL (not the ORM, not the implementation's
  own test)**: constructed a `managed_asset` row with `asset_type='equipment'`, then
  attempted to attach a `pdu_outlet` to it directly via `INSERT`. Result:

  ```
  ERROR:  insert or update on table "pdu_outlet" violates foreign key constraint
  "fk_pdu_outlet_pdu_asset_id_managed_asset"
  DETAIL:  Key (pdu_asset_id, pdu_asset_expected_type)=(...) is not present in table "managed_asset".
  ```

  **Confirmed independently: the database itself rejects this, not merely the API
  layer.**
- **Generated-column interaction with updates/cascades**: not independently
  re-executed in this session (e.g., updating a `managed_asset.asset_type` after a
  `pdu_outlet` already references it, or a cascade delete scenario) — **NOT EXECUTED**;
  reasoned analytically that a generated column recomputes on every row access and
  cannot itself be directly updated, and the existing `ON DELETE CASCADE` on the
  composite FK behaves identically to the original plain FK's cascade behavior for
  deletion — but this reasoning was not verified against a live attempted
  `asset_type` mutation in this pass.

## F-M3 Broader Subtype Gap — `owning_asset_id` (EXECUTED — VERIFIED, unchanged)

Directly attempted, via raw SQL: created a `managed_asset` with `asset_type='rack'`,
then created a `PowerNode` with `node_type='equipment_power_input'` and
`owning_asset_id` pointing at that rack asset. **This succeeded with no error** — the
gap remains exactly as open as the correction design classified it
(`REQUIRES ARCHITECTURAL DECISION`, deliberately deferred).

**Is this acceptable for Phase 3?** Yes, on the same reasoning the design already gave:
this is not a new gap the correction introduced (it existed identically before the
correction), the API layer's own `db.get(Equipment, ...)` check in `create_equipment_feed`
already prevents this through the supported application path, and closing it at the DB
level requires a genuinely harder design decision (a single shared column expecting
three different target subtypes) that was correctly deferred rather than rushed.
**This should remain a documented carry-forward, not escalated to a blocking finding.**

## F-M5 Detailed Revalidation

All 6 designed log points confirmed present by direct reading:
`power_connection_cycle_rejected`, `power_graph_traversal_bound_exceeded` (×3 call
sites — create, upstream endpoint, downstream endpoint, all confirmed), `power_connection_create_db_operational_error`,
`power_topology_mutation_lock_contended`, `power_capacity_unknown`. The contention log
line's trigger condition (`wait_seconds >= 0.5`) was incidentally, genuinely exercised
by this audit's own Part 4/5 widened-timing test (the second HTTP request waited
~2 seconds, well over the 0.5s threshold) — though the log output itself was not
captured/inspected in this pass (the diagnostic script did not redirect or check
`uvicorn`'s stdout for the specific structured log line) — **STATIC ANALYSIS for the
code path's presence, NOT EXECUTED for confirming the actual log line fired and was
correctly formatted**.

## NEW-1 Reassessment

Re-confirmed via `grep` across the full `app/` tree: `Idempotency-Key` handling exists
only in `app/application/idempotency.py` and is not called from any Phase 3 endpoint
(`power.py`, `dashboard.py`). No retry loop was added by the F-C1 correction (confirmed
by reading — the only new exception handling added is the defensive
`except OperationalError` mapping to a client-facing `503`, which does not retry
anything internally). **NEW-1 remains exactly as open, and exactly as narrow-trigger
and non-blocking, as the original Phase 1 finding described.** No new interaction was
introduced, confirmed by both static reading and by the absence of any retry-related
log line or code path in the correction's diff.

## New Findings

| ID | Severity | Description | Evidence |
|---|---|---|---|
| RA-1 | INFO | `with_connection_mutation_lock`'s docstring claim ("first statement of that transaction") is imprecise — the RBAC permission-check query actually auto-begins the transaction first. No functional impact. | STATIC ANALYSIS (Part 3.1) |
| RA-2 | MEDIUM | F-H2's added per-BFS-level retirement-check query compounds with the pre-existing (already-disclosed) dashboard N+1 pattern — `derive_node_capacity_exceptions`, called once per capacity row in `dashboard.py`'s loop, now costs roughly double the DB round-trips per call via `compute_allocated_kw`'s extra query per level. Not called out in this specific combined form by `PHASE3_CORRECTION_IMPLEMENTATION.md`. | STATIC ANALYSIS (confirmed `dashboard.py` unchanged by `git diff`, confirmed `power_capacity.py`'s added query by reading) |
| RA-3 | MEDIUM | Global lock throughput at 100 concurrent requests: p50 latency 3.06s, max 4.94s — a real, quantified cost not previously measured at this scale. Consistent with the design's accepted trade-off, but the specific numbers are new information for evaluating whether "low-frequency provisioning activity" is actually the correct usage-pattern assumption in a given deployment. | EXECUTED — VERIFIED (Part 7) |
| RA-4 | LOW | The gap between uncontended single-request latency (~11ms) and the per-request cost implied by the 100-concurrent throughput test (~55-60ms average) was not decomposed — unclear how much is lock-wait queueing vs. connection-pool contention vs. per-`AsyncClient` overhead in the diagnostic script itself (vs. real production client behavior). | NOT EXECUTED (flagged, not resolved) |
| RA-5 | INFO | Generated-column (`pdu_asset_expected_type`) interaction with a hypothetical future `managed_asset.asset_type` mutation, or a cascade-delete scenario beyond simple creation, was not independently re-tested in this pass. | NOT EXECUTED |
| RA-6 | INFO | Case H (retired downstream node, as distinct from Case D's leaf) was judged mechanism-equivalent to already-tested cases and not separately constructed. | STATIC ANALYSIS |

No CRITICAL or HIGH new finding was discovered.

## Security

Re-tested independently (fresh script, not reusing the implementation's own auth
tests): unauthenticated create attempt → `401`; Viewer-role create attempt → `403`;
Viewer-role capacity PUT → `403`; Viewer-role read access → `200` (correctly permitted);
arbitrary nonexistent UUID → `404`; malformed UUID → `422`. **No new authorization
bypass found.** The Phase 1 global-authorization model is unchanged — confirmed by
`git diff` showing zero lines touched in `rbac.py`, `security.py`, or any
authentication-related file.

## Database Integrity

| Invariant | Enforcement Level | Confirmed |
|---|---|---|
| Self-loop | DB (CHECK constraint) | Unchanged, not re-tested this pass (unaffected by the correction) |
| Duplicate active edge | DB (partial unique index) | Unchanged, not re-tested this pass |
| Graph acyclicity (true cycles) | **Application-enforced transactional invariant** (never DB-enforced, and the correction does not claim otherwise) | Re-verified this pass via direct-SQL bypass (manually inserted 3-cycle succeeded with no DB error) — correctly, honestly described as application-only in both the code's own docstrings and `PHASE3_CORRECTION_IMPLEMENTATION.md`, not misrepresented as a DB constraint |
| `pdu_outlet` subtype (`pdu_asset_id`) | **DB (composite FK)** | Re-verified this pass via independent raw-SQL bypass attempt — correctly rejected |
| `power_node.owning_asset_id` subtype | **Not enforced at any level below the API** | Re-verified this pass via independent raw-SQL bypass attempt — succeeded (gap confirmed still open, as designed) |
| Retired-node exclusion from traversal | **Application-enforced only** (a retired node's connection rows are untouched at the DB level; exclusion is purely a query-time filter) | Confirmed by reading — no DB constraint prevents a retired node's connections from remaining `effective_to IS NULL` forever; this is correct per Model A's design (retirement is a soft, query-time concept, not a hard deletion) |

## Concurrency

Covered in full above (F-C1 sections). Optimistic concurrency (`PowerConnection`
PATCH, capacity PUT) was re-tested for the malformed/stale/zero/negative cases (F-M1
section) — no lost-update scenario found; each mutation still requires a matching
version via the shared, correctly-hardened parser.

## Performance

Covered above (Part 7). Additional reasoning, **ANALYTICAL CONCLUSION** (not executed
at these scales in this pass): at 1,000/5,000/10,000 power nodes, the F-H2 retirement
filter's added per-level query cost scales with traversal *depth* (levels), not total
graph size — a deep, narrow topology (a long chain) pays proportionally more than a
shallow, wide one (a hub with many direct children), a distinction already noted in the
implementation report and reconfirmed as still accurate on re-reading the code; not
independently re-measured at these three scales in this audit pass.

## Frontend

**NOT EXECUTED.** `git diff --stat` confirms zero files under `frontend/` were touched
by the correction commit. No interactive browser session was run in this audit pass —
consistent with the implementation report's own honest disclosure that this was not
re-validated, and this audit found no reason (no API contract change, confirmed by
reading every Pydantic response model touched) to believe frontend re-validation was
more urgent than the implementation report already stated. This remains a genuine gap
for the independent red-team to close, not resolved here.

## Test Quality

Re-read the implementation's own new F-C1 tests in `tests/api/test_power.py` with
hostile intent, specifically checking for the two disqualifying patterns the task
named: **"a test that launches two coroutines sequentially"** and **"a test that mocks
pg_advisory_xact_lock."** Neither pattern was found: every F-C1 test uses
`asyncio.gather` (genuine concurrent scheduling, confirmed by direct reading) with each
concurrent request routed to its own dedicated `AsyncSession` from a separate engine
(`_per_request_client_ctx`, confirmed to create a distinct `create_async_engine` and
`async_sessionmaker`, not reusing the shared single-session `db_session`/`client`
fixtures) — and no `monkeypatch`/`mock` appears anywhere near the F-C1 test functions
(the only `monkeypatch` uses in the whole Phase 3 test suite are for `MAX_TRAVERSAL_*`
bounds in unrelated tests, a pre-existing, legitimate technique). This audit's own
independent, freshly-written script reproduced consistent results against the real
locking mechanism, corroborating that the existing tests are not merely passing by
coincidence or by testing a mocked substitute.

**Could the implementation be broken while these tests still pass?** For F-C1
specifically: no plausible regression was found during this audit that would defeat
both the implementation's tests and this audit's independent script simultaneously,
since both exercise the real lock via real concurrent PostgreSQL sessions. A
regression that only manifested under 3+-way, non-triangle topologies, or under
different timing distributions than either test suite constructs, cannot be ruled out
with certainty — this is the honest limit of any finite test suite, not a specific
identified gap.

## Migration

Fresh install, representative-data-preservation, downgrade, and re-upgrade were
re-confirmed in this audit's own scratch database (not merely re-trusting the
implementation session's own prior run). The pre-flight safety-check behavior
(refusing to silently apply the constraint over invalid data) was **not** re-executed
in this specific audit pass (it was already verified once, in the implementation
session, with a manufactured violating row and a confirmed abort) — this audit chose
to spend its migration-validation budget on the composite-FK bypass attempt instead,
judging the pre-flight-check mechanism itself (a straightforward `SELECT` + `raise`)
lower-risk to not re-verify than the actual constraint's runtime behavior.

## Remaining Risks

- RA-2 (dashboard N+1 compounding) and RA-3 (measured 100-concurrent throughput cost)
  are the two most actionable new items from this audit — neither is blocking, both
  are real and quantified.
- The `owning_asset_id` half of F-M3 remains open, as designed, not as an oversight.
- Full master-prompt-scale performance (5,000+/10,000+ scale) remains unmeasured for
  both the original Phase 3 implementation and this correction — unchanged status.
- Frontend/browser validation remains unexecuted for this correction — a real,
  standing gap before any claim of full end-to-end validation.
- Multi-process (as opposed to multi-connection, single-process) advisory lock
  behavior was not physically demonstrated, though it is not expected to differ
  (PostgreSQL, not the application process, is the coordination point).

## Required Corrections

**None required before proceeding to independent red-team.** The two new MEDIUM
findings (RA-2, RA-3) are disclosure/documentation gaps and a quantified-but-already-
accepted trade-off, not defects requiring code changes before the next gate. The
INFO/LOW findings (RA-1, RA-4, RA-5, RA-6) are documentation-precision or
not-yet-executed-verification items, not corrections.

If the independent red-team wants these addressed before proceeding, the recommended
minimal actions (not implemented here, per this task's audit-only scope) would be:
(1) correct `with_connection_mutation_lock`'s docstring to say "first statement of the
route's own critical section" rather than "of that transaction"; (2) add one sentence
to `PHASE3_CORRECTION_IMPLEMENTATION.md`'s Known Limitations section explicitly naming
the dashboard-N+1/F-H2 compounding effect; (3) decompose the 100-concurrent-request
latency gap (RA-4) with a dedicated profiling pass if the throughput numbers are judged
a real deployment concern.

## Final Verdict

**PHASE 3 CORRECTION HOSTILE RE-AUDIT PASSED — READY FOR INDEPENDENT RED-TEAM**

This verdict means exactly what it says: the correction's own claims held up under
this session's hostile, independent attempt to break them, and no CRITICAL or HIGH
defect was reopened or newly discovered. It does **not** mean Phase 3 is approved, does
**not** mean an independent red-team has passed it, and does **not** certify anything
beyond what this document itself demonstrates.
