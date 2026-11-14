# Phase 8 Blocking Correction Report (I1–I4)

This report documents the targeted correction of the four BLOCKING findings
(I1, I2, I3, I4) raised by `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`. It does
**not** overwrite, edit, or supersede that report — it remains the historical
record of what the independent audit found. This document is the correction
cycle's own record: what was changed, how it was validated, and what remains
open for the subsequent independent revalidation.

## 1. Starting Point

- Branch: `claude/phase8-independent-red-team` (existing branch, not new).
- Starting commit: `019b898167ea62aebc690ec4aac0547c3c9e5da6` ("test:
  independent Phase 8 hostile validation") — the commit that closed the
  independent audit with gate `PHASE 8 NOT CLOSED — CORRECTIONS REQUIRED`.
- This task corrects exactly the four findings the independent audit
  classified BLOCKING: I1 (MEDIUM), I2 (HIGH), I3 (HIGH), I4 (CRITICAL). It
  does not touch I5 or F1–F5 (all NON-BLOCKING, out of this task's scope).

## 2. Scope and Explicit Exclusions

In scope: I1, I2, I3, I4 only, on the collector-ingest and
reconciliation-decision code paths those findings identified.

Explicitly out of scope, and not touched by this correction: Phase 9 work,
Phase 8B / Edge Collector runtime work, unrelated redesign of any kind,
Power/inventory/spatial/telemetry/alarm/AI/reporting/CFD/digital-twin
subsystems, `CollectorRequestNonce`/`CollectorHeartbeat` pruning (F1), secret
rotation (F2), a polling scheduler, real SNMP support, or I5's query-count
behavior (touched by none of I1–I4's code paths and left exactly as the
independent audit measured it).

## 3. Summary of Corrected Findings

| ID | Independent audit's classification | Correction |
|---|---|---|
| I1 | MEDIUM / BLOCKING | Fixed — timestamp trust-boundary check now compares in pure integer epoch-seconds space; never constructs a `datetime` from untrusted input. |
| I2 | HIGH / BLOCKING | Fixed — `idem.get_or_claim`'s exceptions are now caught in their own per-record `try`/`except`, isolated from the rest of the batch. |
| I3 | HIGH / BLOCKING | Fixed — `accept_reconciliation`/`reject_reconciliation` now take a `SELECT ... FOR UPDATE` row lock before checking `status`, serializing racing decisions. |
| I4 | CRITICAL / BLOCKING | Fixed — the per-record failure path now rolls back a `SAVEPOINT` (`db.begin_nested()`), not the whole session, so a failed record no longer expires session-wide ORM state. |

## 4. Finding I1: Root Cause and Correction

**Root cause.** `verify_collector_request` (`app/application/collector_auth.py`)
parsed `X-Collector-Timestamp` with `int()` (unbounded), then called
`datetime.fromtimestamp(ts, tz=UTC)`. For a syntactically valid but
astronomically out-of-range integer, `datetime.fromtimestamp` raises
`OverflowError` (the exact exception type is platform-dependent —
`OverflowError`/`ValueError`/`OSError` depending on how far out of range and
which platform), which was not caught anywhere in the call chain, reaching
FastAPI's generic 500 handler instead of the clean 401 every other malformed-
header case in this same function produces.

**Correction.** Two changes, both in the same function:
1. Bound the header's length (`len(timestamp_header) > 20` → reject) before
   parsing at all — a real Unix-seconds timestamp is never more than ~11
   digits for millennia; this keeps the subsequent `int()` cheap and the
   check obviously sufficient without relying on any particular exception
   type `int()` itself might raise for a pathological digit string.
2. Never construct a `datetime` from the untrusted value. `now_ts = int(
   datetime.now(UTC).timestamp())` uses the trusted server clock only; the
   comparison `abs(now_ts - ts) > REQUEST_TIMESTAMP_WINDOW_SECONDS` is pure
   integer arithmetic, which has no overflow limit in Python at any
   magnitude. This is not a broader exception catch bolted onto the old
   approach — it removes the operation that could fail in the first place.

The verification order (timestamp check before nonce claiming) is
unchanged, so the audit's own finding that an invalid timestamp must not
consume a nonce still holds structurally, and is now covered by an explicit
regression test (§10).

## 5. Finding I2: Root Cause and Correction

**Root cause.** In `ingest_batch` (`app/api/v1/collectors.py`), the call
`await idem.get_or_claim(...)` sat outside the per-record `try`/`except`
block. `IdempotencyConflict` (same `dedup_key`, different request payload)
propagated out of the loop entirely, failing the whole batch with an
unhandled 500 instead of isolating the one conflicting record — breaking
the endpoint's own per-record ACK contract.

**Correction.** `get_or_claim` is now called inside its own `try`/`except`,
separate from the SAVEPOINT block that follows it:

```python
try:
    outcome = await idem.get_or_claim(
        db, key=f"{collector_id}:{record.dedup_key}", endpoint="collector_ingest", request_hash=request_hash,
    )
except IdempotencyConflict:
    results.append(IngestRecordResult(dedup_key=record.dedup_key, status="rejected",
        error="This dedup_key was already used with a different request body."))
    continue
except IdempotencyStillProcessing:
    results.append(IngestRecordResult(dedup_key=record.dedup_key, status="rejected",
        error="This record is still being processed by a concurrent request; retry shortly."))
    continue
```

This mirrors the exception-handling *pattern* already established elsewhere
in this codebase (`app/api/v1/managed_assets.py` maps the same two
exceptions to HTTP-level responses), adapted to this endpoint's own
contract: a per-record "rejected" status, not an HTTP-level error, since the
whole point of `ingest_batch` is that one record's outcome is independent of
its siblings'.

## 6. Finding I3: Root Cause and Correction

**Root cause.** `accept_reconciliation` and `reject_reconciliation`
(`app/application/discovery_service.py`) both fetched the
`ReconciliationDiff` via a plain `db.get()`, then checked `if diff.status !=
"pending"` in application code. Under READ COMMITTED, two genuinely
concurrent decisions (e.g. one accept, one reject) on the same diff can both
read `status == "pending"` before either commits — both pass the guard, both
mutate, both commit, and whichever commits last silently overwrites the
other's terminal state while both callers see a 200.

**Correction.** Both functions now take a `SELECT ... FOR UPDATE` row lock
on the diff before checking its status:

```python
diff = (
    await db.execute(select(ReconciliationDiff).where(ReconciliationDiff.id == diff_id).with_for_update())
).scalar_one_or_none()
```

This is the same locking pattern already used elsewhere in this codebase
for an analogous concurrency fix (the existing Phase 3 F-C1 precedent). The
row lock, not the `status` check, is what actually serializes the race: the
second concurrent request's `SELECT ... FOR UPDATE` blocks until the first
request's transaction ends (commit or rollback), then observes the
now-committed non-`"pending"` status and returns a clean 409. No schema
change, no new column, no migration — see §20.

## 7. Finding I4: Root Cause and Correction

**Root cause.** `ingest_batch`'s per-record `except` block called `await
db.rollback()` on any failure. Because `AsyncSessionLocal` is configured
with `expire_on_commit=False` but `db.rollback()` unconditionally expires
**every** object the session is currently tracking — not only objects
touched since the last commit — the request-scoped `collector` object
(fetched once via `Depends(get_current_collector)` before the per-record
loop began, and never itself mutated) was expired by the first record's
failure. The next record's plain attribute access on `collector` (e.g.
`collector.id`) then required an implicit lazy reload outside an awaited
context, crashing with `MissingGreenlet` ("greenlet_spawn has not been
called") — corrupting every subsequent record in the batch, not just the
failed one.

**Correction.** The per-record write path now runs inside a `SAVEPOINT`
(`async with db.begin_nested(): ...`) instead of relying on a full-session
rollback on failure:

```python
try:
    async with db.begin_nested():
        assignment = await current_assignment(db, record.integration_id)
        if assignment is None or assignment.collector_id != collector.id:
            raise ApiError(...)
        ...
        await ingest_discovery(db, ...)
        await idem.complete_claim(db, outcome.claim, ...)
    await db.commit()
    results.append(IngestRecordResult(..., status="accepted"))
except Exception as exc:
    await idem.release_claim(db, claim_id)
    results.append(IngestRecordResult(..., status="rejected", error=str(exc)))
```

The explicit `await db.rollback()` that used to appear in the `except`
block is removed entirely — the nested transaction's own automatic
rollback-to-savepoint (triggered by the `async with` block on exception)
replaces it. See §8 for why this specific primitive is correct, including
the empirical proof performed before implementation.

## 8. Transaction-Boundary Explanation

`SAVEPOINT`/nested-transaction rollback (`db.begin_nested()`) is
semantically narrower than session-wide `db.rollback()`:

- **Full session rollback** ends the session's entire transaction and, per
  SQLAlchemy's documented behavior, expires every object the session's
  identity map is tracking, regardless of whether that object was touched
  in the failed unit of work.
- **A SAVEPOINT rollback** (rolling back the `async with db.begin_nested()`
  block on exception) discards only the DML issued since that savepoint was
  opened — in this case, exactly one record's own attempted
  `DiscoveredDevice`/`ReconciliationDiff`/`OutboxEvent`/`IdempotencyKey`
  writes — and expires only the ORM objects touched within that nested
  scope. Every other object in the session's identity map, including the
  never-mutated `collector`, is left untouched and fully usable on the next
  loop iteration.

Before writing the production fix, this was verified empirically against a
real Postgres instance with a standalone script (not committed — a
throwaway proof, superseded by the committed test suite): a session fetches
an object, opens a nested transaction, fails a write inside it (forcing a
rollback-to-savepoint), and then performs an ordinary read on the
originally-fetched object — confirmed to succeed with no `MissingGreenlet`,
and a subsequent nested block in the same session still commits normally.
The committed regression suite (§13, §18) now covers this same property
against the real endpoint, including a genuine database-level
`IntegrityError` (§18), not only the standalone proof.

`ingest_discovery`'s domain write and `idem.complete_claim`'s idempotency
completion are both inside the same `SAVEPOINT` scope (confirmed by
re-reading the current source: both calls sit inside the same `async with
db.begin_nested():` block, lines 372–396 of `app/api/v1/collectors.py`), so
a failure at either point rolls back both together — there is no code path
where a record's domain write is durably committed but its idempotency
claim is not marked complete, or vice versa, for a normal application- or
constraint-level failure. See §18 for the one narrower residual gap this
review turned up (a commit-time-only failure), documented rather than
fixed, since fixing it is outside I1–I4's scope.

## 9. Concurrency-Control Explanation (I3)

`SELECT ... FOR UPDATE` is a pessimistic row lock: it is held for the
duration of the current transaction (which, for `accept_diff`/`reject_diff`,
is the whole HTTP request — the endpoint commits once at the end) and is
released on commit or rollback. A second request attempting to lock the
same `ReconciliationDiff` row blocks at the `SELECT` itself until the first
request's transaction ends, at which point it re-reads the row's
now-committed state. This makes the row lock — not the subsequent
`status != "pending"` application check — the actual point of
serialization; the `status` check merely converts "I lost the race" into a
clean, existing 409 response, which was already the intended behavior for a
non-concurrent re-decision.

This was the leading candidate over two alternatives considered and
rejected: (a) adding a `version` column for optimistic concurrency, which
would require a migration and is unnecessary complexity for a resource with
no long-held client-side edit sessions (reconciliation decisions are single
atomic actions, not multi-step edits); (b) an atomic conditional `UPDATE ...
WHERE status='pending' RETURNING id`, which is migration-free like the
chosen approach but is a less direct match for this codebase's own existing
concurrency-fix precedent (Phase 3's F-C1) and does not read as cleanly when
the same locked row also needs a subsequent domain read (the
`DiscoveredDevice` lookup in `accept_reconciliation`).

## 10. Regression Test Conversion (xfail → passing)

All four `xfail(strict=True)` reproduction tests the independent audit
committed are now either replaced or converted to passing assertions of the
corrected behavior, with 0 `xfailed` remaining for I1–I4 anywhere in the
backend suite:

- **I1** (`tests/api/test_phase8_independent_audit_repro.py`): the single
  old xfail test was replaced with 5 non-xfail tests covering malformed/
  out-of-range/empty/non-numeric timestamps (parametrized), stale
  timestamps, future timestamps, a valid in-window timestamp, and — the
  audit's own nonce-non-consumption requirement — a direct
  `collector_request_nonce` table check proving an invalid-timestamp
  attempt claims 0 nonce rows and the same nonce remains usable afterward.
- **I2**: the `xfail` marker was removed from
  `test_I2_same_dedup_key_different_payload_no_longer_crashes_whole_batch`
  (renamed from `..._crashes_whole_batch`); its assertions now check the
  corrected 200 response with a `"rejected"` status for the conflicting
  record and `"accepted"` for the valid sibling.
- **I3**: the `xfail` marker was removed from
  `test_I3_concurrent_accept_and_reject_race_no_longer_loses_a_decision`
  (renamed from `..._on_same_diff`); it asserts exactly one 200 and one 409
  from a genuinely concurrent accept/reject pair.
- **I4**: the `xfail` marker was removed from `test_sim_11_partial_ack_mixed_batch`
  in `tests/api/test_phase8_edge_collector_simulator.py`.

Before each marker was removed, the corrected test was run against the
fix and confirmed as `XPASS(strict)` — an unexpected pass under strict
xfail — as authoritative proof the fix works, prior to converting it to an
ordinary passing test.

## 11. Batch-Composition Test Matrix Results

A new file, `tests/api/test_phase8_correction_validation.py`, implements the
exact batch-composition matrix this correction task requires, inspecting
actual database state (not only the ACK response) after each case:

| Composition | Result |
|---|---|
| `[conflict, valid]` | conflict → `rejected` (0 device rows for it); valid → `accepted` (1 device row) |
| `[valid, conflict, valid]` | both valids → `accepted`; conflict → `rejected`, 0 device rows |
| `[valid, duplicate, conflict, valid]` | duplicate (same key + same payload) → `duplicate`, 1 device row (from the original delivery only); conflict → `rejected`, 0 device rows; both valids → `accepted` |
| `[good, bad, good]` | good → `accepted`, bad → `rejected`, good → `accepted` |
| `[bad, good]` | bad → `rejected`, good → `accepted` |
| `[good, bad, good, good]` | all three goods → `accepted`, bad → `rejected` |
| `[good, bad, bad, good]` | two **consecutive** failures — both bad → `rejected`, both goods → `accepted`, proving the SAVEPOINT mechanism recovers across repeated failures, not only a single one |

All 7 matrix tests pass. A duplicate is explicitly distinguished from a
conflict throughout: a duplicate is the same `dedup_key` **and** the same
payload (cached response replayed, `status: "duplicate"`); a conflict is the
same `dedup_key` with a **different** payload (`IdempotencyConflict`,
`status: "rejected"`).

## 12. DB-State Inspection Results (Audit/Outbox Atomicity)

`test_rejected_record_leaves_no_partial_domain_audit_or_outbox_state`
inspects the database directly (not the HTTP response) after a rejected
record and confirms all of the following hold simultaneously:

- 0 `discovered_device` rows for the rejected record's external identifier.
- 0 `reconciliation_diff` rows joined to that device.
- 0 `outbox_event` rows with `aggregate_type = 'discovered_device'`.
- The idempotency claim is fully released (`NULL`/absent), not stuck in
  `"processing"` — proven by successfully retrying the same `dedup_key`
  with a corrected record immediately afterward (`accepted`, 1 device row).

For I3, `_decision_audit_count` (a raw `audit_log` query filtered to
`entity_type = 'reconciliation_diff'` and
`action IN ('reconciliation.accept', 'reconciliation.reject')`) is asserted
`== 1` after every racing-decision test (accept-vs-accept, reject-vs-reject,
10-way mixed, accept-vs-reject) — exactly one terminal decision produces
exactly one audit entry, never one per racing request. See §17.

## 13. Edge Collector Simulator Results (Post-Correction)

`tests/api/test_phase8_edge_collector_simulator.py` (all 26 originally
implemented scenarios, 21 of which are separate test functions — several
scenarios share a function) — full re-run, 21/21 passed, 0 xfailed, 0
xpassed. `test_sim_11_partial_ack_mixed_batch` (the I4 reproduction) now
passes as an ordinary test.

## 14. Hostile Collector-Auth Re-validation

`tests/api/test_phase8_independent_audit_repro.py`'s I1 tests plus the
simulator's auth-focused scenarios (`sim_15` stale timestamp, `sim_16`
future timestamp, `sim_17` reused nonce, `sim_18` modified body after
signing, `sim_19` wrong secret, `sim_20` disabled collector) — all re-run
and passing, no regression from the I1 fix. The nonce-non-consumption
property (an invalid-timestamp request must not burn a nonce that a
subsequent valid request could still use) is now covered by an explicit,
passing, non-xfail test (§10), not only asserted in prose.

## 15. Site Isolation Re-validation

`sim_21_collector_attempting_another_collectors_integration` and
`sim_22_collector_attempting_another_sites_integration` — both re-run and
passing, no regression from the I2/I4 per-record transaction-isolation
rewrite. This correction task introduces no site-scoped human RBAC change,
per the explicit instruction not to.

## 16. Assignment/Idempotency Concurrency Re-validation

The full `tests/integration/` directory (existing Phase 8 genuine-
concurrency tests, including assignment-race and idempotency-concurrency
tests predating this correction) was re-run: 68/68 passed, no regression.
`test_concurrent_retry_after_rejection_is_idempotent` (new, in
`test_phase8_correction_validation.py`) adds a genuine 10-way concurrent
retry of a previously-rejected `dedup_key`: exactly 1 `accepted` + 9
`duplicate`, exactly 1 device row.

## 17. Reconciliation Concurrency Results (I3)

Six new tests in `test_phase8_correction_validation.py`, each using
genuinely separate DB sessions/connections (dedicated engine +
`get_db` override), not a shared session:

- **Accept-vs-accept**: exactly one 200, one 409; final `status ==
  "accepted"`; exactly one audit entry.
- **Reject-vs-reject**: exactly one 200, one 409; final `status ==
  "rejected"`; exactly one audit entry.
- **10-way concurrent, mixed** (5 accept + 5 reject on the same diff):
  exactly one 200, nine 409; exactly one audit entry.
- **Sequential re-decision** (not a race — a sanity check that the base
  guard still works once the lock is no longer contended): accept, then a
  later reject attempt gets a clean 409.
- **`ManagedAsset`-association consistency**: on the accept-wins branch,
  `DiscoveredDevice.status == "reconciled"` and
  `matched_managed_asset_id` is correctly set; on the reject-wins branch,
  the device shows **no** partial "reconciled" mutation from the losing,
  rolled-back accept attempt (`status != "reconciled"`,
  `matched_managed_asset_id is None`).
- **Rollback-of-the-winner**: an accept naming a nonexistent
  `matched_managed_asset_id` raises `NotFoundError` (404) after acquiring
  the row lock but before mutating `diff.status` — the diff is confirmed
  still `"pending"` afterward (the failed attempt's row lock was released
  by `get_db`'s session-wide rollback-on-exception), and a subsequent
  reject on the same diff succeeds cleanly.

All 6 pass, plus the pre-existing I3 reproduction test converted to passing
(§10) — 7 total I3-specific tests, all passing.

## 18. Hostile Self-Review of New Transaction Handling

Every failure point the correction task requires attacking was verified,
either by an existing test or a new one added specifically for this review:

| Failure point | Coverage |
|---|---|
| Exception before the savepoint (`IdempotencyConflict`/`IdempotencyStillProcessing` from `get_or_claim`) | §11 matrix (`conflict`/`duplicate` cases) |
| Exception inside the savepoint, application-level (unassigned integration → `ApiError`) | §11 matrix (`bad` cases) |
| Exception inside the savepoint, **real database-level** | New: `test_i4_concurrent_records_same_external_identifier_hits_real_db_constraint_inside_savepoint` |
| Exception after the domain write but before idempotency completion | Structural: both calls are in the same `async with db.begin_nested()` scope (§8) — no code path separates them |
| Exception after the idempotency claim is obtained | Same as "inside the savepoint" — `claim_id` is captured before the nested block and `release_claim` runs in the `except` branch either way |
| Duplicate retry after rejection | §12 |
| Concurrent duplicate | §16 |
| Database constraint failure | New test, below |
| Malformed sibling after a valid sibling | §11 (`[good, bad, good]`) |
| Many rejected records followed by a valid record | §11 (`[good, bad, bad, good]` — two consecutive failures; the SAVEPOINT mechanism is stateless per iteration, so this generalizes to any run length rather than being a property of exactly two) |
| Concurrent reconciliation race under multiple sessions | §17 |

**New test added by this review:**
`test_i4_concurrent_records_same_external_identifier_hits_real_db_constraint_inside_savepoint`
sends two genuinely concurrent records (separate sessions/connections),
same `(integration_id, external_identifier)` but different `dedup_key`s,
racing on `discovered_device`'s own
`uq_discovered_device_integration_id_external_identifier` unique
constraint — `ingest_discovery`'s existing-row `SELECT` is not itself
atomic against a concurrent `INSERT`. Run manually 10 times with a
temporary debug print (not committed) to confirm the race genuinely
manifests: all 10 runs produced a real `asyncpg.exceptions.
UniqueViolationError`, caught cleanly by the per-record `except`, with the
losing record marked `"rejected"` and its claim released, the winning
record `"accepted"`, exactly one device row persisted, and the session
fully usable afterward (proven by an ordinary follow-up request on the same
shared session). The committed test asserts on the invariant (`{status_a,
status_b} <= {"accepted", "rejected"}`, at least one `"accepted"`, exactly
one device row, no stuck claims) rather than on which specific outcome
occurs on a given run, so it is not flaky regardless of scheduling.

**One residual gap found and documented, not fixed** (narrower than I1–I4,
requires an exotic trigger, judged non-blocking): if `await db.commit()`
itself fails *after* the nested block has already exited cleanly (line 399
of `app/api/v1/collectors.py`) — for example, a connection-level failure
between the savepoint release and the outer commit — the subsequent
`idem.release_claim(db, claim_id)` call in the `except` branch has no
special handling for a session left in a failed-transaction state, and
could itself raise, propagating out of the per-record loop uncaught for
that one record. This is not reachable by any application-level or
ordinary-constraint failure (all of those are caught *inside* the nested
block, before `db.commit()` is ever reached, as demonstrated by the real
`IntegrityError` test above) — only by a connection/infrastructure-level
failure at that exact instant, which is outside the scope of what I1–I4
describe or require. Fixing it would mean adding new production recovery
logic (e.g. an explicit rollback-then-retry around `release_claim`) not
required by any of the four findings this task was scoped to correct, so
it is documented here for independent review rather than fixed in this
correction.

## 19. Performance Re-Measurement

`tests/api/test_phase8_independent_performance.py` and
`tests/api/test_phase8_performance.py` (the independent audit's own I5
query-count measurements for `GET /collectors`, `GET /integrations`, and
`run_polling_cycle`) — re-run, 9/9 passed, no change. None of I1–I4's
corrected code paths touch these endpoints or `run_polling_cycle`; I5
itself was not modified, per this task's explicit instruction not to
optimize it.

## 20. Migration Result

**No migration required for I1–I4 correction.** All four fixes are pure
application-code changes:

- I1: comparison logic only, no schema involvement.
- I2: exception-handling structure only, no schema involvement.
- I3: `SELECT ... FOR UPDATE` is a query-time locking clause, not a schema
  change — no new column, no new index, no new constraint.
- I4: `db.begin_nested()` (a `SAVEPOINT`) is a transaction-control
  primitive, not a schema change.

Confirmed via `git status --short migrations/` (no output — the directory
is untouched) and `alembic heads` (single head, `0008_phase8`, unchanged
from the independent audit's own starting point).

## 21. Full Backend Suite Result

Final run, this correction's complete working tree, exact pytest summary
line reproduced verbatim:

```
424 passed, 11 warnings in 157.98s (0:02:37)
```

0 failed, 0 skipped, 0 xfailed, 0 xpassed. The 11 warnings are all the same
pre-existing `StarletteDeprecationWarning` about `HTTP_422_UNPROCESSABLE_ENTITY`
the independent audit already noted (`PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`
§24) — unrelated to this correction, not newly introduced.

For direct comparison, `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md` §24 reports
its own baseline verbatim as `395 passed, 4 xfailed, 11 warnings` (399 total
collected). This correction's own final number above — `424 passed`, 0
xfailed — was read directly from a single, complete, freshly executed
`pytest -q` run against the full suite immediately before writing this
report (§ command shown above), not carried over from an earlier report or
reconstructed by arithmetic. No further breakdown of the exact per-file
delta between the two runs is asserted here, to avoid repeating the earlier
371-vs-372 reporting inconsistency this task was warned against — both
numbers above are each individually exact, directly observed pytest summary
lines.

## 22. Frontend Result

`npm run typecheck` and `npm run lint` — both clean, no errors, no output.
No frontend files were changed by this correction (I1–I4 are backend
transaction/concurrency/validation fixes with no API request/response
contract change), so this is confirmation of no regression, not evidence of
new frontend work.

## 23. Remaining Non-Blocking Findings

Unchanged from `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md` §27, none touched by
this correction, per explicit scope instructions:

- **I5** (MEDIUM, NON-BLOCKING) — `run_polling_cycle`'s near-linear query
  growth. Not optimized here.
- **F1** (LOW, NON-BLOCKING) — no nonce/heartbeat pruning job. Not added
  here.
- **F2** (LOW, NON-BLOCKING) — no secret-rotation endpoint. Not added here.
- **F3** (INFORMATIONAL, NON-BLOCKING) — REST driver error message detail.
- **F4** (INFORMATIONAL, NON-BLOCKING) — frontend SNMP-limitation
  disclosure.
- **F5** (INFORMATIONAL, NON-BLOCKING) — out-of-order-delivery `occurred_at`
  handling, a Phase 9 design point per the independent audit's own framing.

Plus the one new residual observation from §18 (commit-time-only failure
path in the release-claim recovery), documented, non-blocking, not fixed
in this correction.

## 24. Readiness for Independent Revalidation

All four BLOCKING findings (I1, I2, I3, I4) have been corrected, each with
a root-cause fix, a converted (not deleted) regression test proving the
fix, additional targeted concurrency/DB-state tests beyond the original
reproduction, and a hostile self-review of the new transaction handling
that found and documented one narrow, non-blocking residual gap without
expanding this correction's scope to fix it. The full backend suite passes
at 424/424 with 0 xfailed. Frontend typecheck and lint are clean. No
migration was required or added. No Phase 9, Phase 8B, or unrelated work
was performed.

This correction does not declare Phase 8 closed. That decision belongs to
the subsequent independent revalidation.

**READY FOR INDEPENDENT PHASE 8 CLOSURE VALIDATION**
