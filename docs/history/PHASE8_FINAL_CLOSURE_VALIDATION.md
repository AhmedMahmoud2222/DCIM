# Phase 8 Final Independent Closure Validation

Hostile, independent final validation of Phase 8 — Integrations + Collectors — on
branch `claude/phase8-independent-red-team` at corrected HEAD
`e4db1aaaa95cce26ec76fe7dc079403d81999bdd`. Treats
`PHASE8_IMPLEMENTATION_REPORT.md`, `PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md`,
`PHASE8_TRACEABILITY_MATRIX.md`, `PHASE8_EDGE_COLLECTOR_CONTRACT.md`,
`PHASE8_GAP_ANALYSIS.md`, `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`, and
`PHASE8_BLOCKING_CORRECTION_REPORT.md` as claims, not proof. No production code is
modified by this validation.

## 1. Branch

`claude/phase8-independent-red-team`

## 2. Starting SHA

`e4db1aaaa95cce26ec76fe7dc079403d81999bdd` — confirmed as the exact `git rev-parse
HEAD` at the start of this validation, with a clean working tree (`git status
--short` produced no output) and `git log --oneline -10` showing the expected commit
`fix: close Phase 8 independent audit blockers` at HEAD. Matched the master prompt's
expected value exactly; no discrepancy to report.

## 3. Final Validation SHA

Recorded at the end of §32 below, after committing this report and the new
validation test files. No production code changes are included in that commit.

## 4. Production-Code-Change Status

**NO production code was modified during this validation.** `git diff --stat
019b898167ea62aebc690ec4aac0547c3c9e5da6..e4db1aaaa95cce26ec76fe7dc079403d81999bdd`
(the correction's own diff, independently re-inspected — see §6) confirms exactly 3
production files changed by the prior correction commit; this validation task adds
only two new test files (`tests/api/test_phase8_final_closure_validation.py`,
`tests/api/test_phase8_final_closure_performance.py`) and this report. No blocking
defect was found that would have required an `xfail(strict=True)` regression per the
task's own validation-only rule — every test written here passes against the
corrected code as-is.

## 5. Independent Methodology

Every finding below was produced by one or more of: (a) direct re-reading of the
actual production source at corrected HEAD (not trusting either report's prose), (b)
freshly written adversarial tests using new cases, new batch compositions, and new
concurrency scenarios not present in `test_phase8_correction_validation.py` or
`test_phase8_independent_audit_repro.py`, (c) genuine fault injection around the
single `db.commit()` call boundary the correction report flagged as a residual
concern (never around domain logic itself, and always verified against the real,
executed underlying commit — not a mocked belief about what it would do), (d) a real
PostgreSQL migration cycle against a freshly created scratch database (not the
long-lived `dcim_test` database other suites reuse), and (e) source-level security and
architecture sweeps (grep plus manual review of every match, not a raw count). Where
a test's first draft produced a misleading result (two cases below), that is disclosed
rather than silently corrected out of the record.

## 6. Production Diff Review

`git diff --stat 019b898..e4db1aaa` confirms exactly 3 production files changed:
`backend/app/application/collector_auth.py` (I1), `backend/app/api/v1/collectors.py`
(I2+I4), `backend/app/application/discovery_service.py` (I3) — matching the expected
narrow scope exactly (collector authentication, collector ingestion, reconciliation
concurrency). Every changed line was re-read directly (not summarized from the
correction report):

- **I1** (`collector_auth.py`): a length gate (`len(timestamp_header) > 20`) plus a
  switch from `datetime.fromtimestamp()` to pure integer epoch-second arithmetic. No
  change to the verification ORDER (timestamp check still precedes nonce claiming).
  No weakened security: the length bound is a sanity/cost bound, not a trust
  decision (independently confirmed — see §7's 20/21-digit boundary tests).
- **I2/I4** (`collectors.py`): `idem.get_or_claim` wrapped in its own `try`/`except`
  (I2); the domain-write section replaced with `async with db.begin_nested(): ...`
  and the per-record `except` no longer calls a full `await db.rollback()` (I4). The
  `except Exception as exc:  # noqa: BLE001` broad catch was ALREADY present before
  this correction (it previously wrapped `await db.rollback()`); the correction did
  not introduce new broad exception handling, it narrowed what the exception handler
  discards (a savepoint's worth of work, not the whole session). No authorization
  semantics changed: the `current_assignment` check, its 403 on mismatch, and the
  `collector.id != collector_id` URL-vs-signed-identity check are all unchanged and
  still execute in the same relative order.
- **I3** (`discovery_service.py`): `db.get()` replaced with `select(...).where(...)
  .with_for_update()`. No new column, no migration (confirmed — see §23). No change
  to who may call these functions (`discovery:reconcile` permission requirement in
  `discovery.py` is untouched).

No accidental commit behavior, no lost idempotency guarantee, no cross-site
authorization regression, and no unrelated file was found in the diff.

## 7. I1 Result — Independent Timestamp Validation

**I1 CLOSED.**

A freshly written 14-case parametrized adversarial matrix
(`test_i1_fresh_timestamp_adversarial_matrix`, deliberately including cases the
correction's own test file does not use: whitespace-padded, all-whitespace,
tab/newline whitespace, an explicit `+` sign, a small negative value, the exact
20-digit and 21-digit length-gate boundaries, a negative 20-character boundary, int64
max and int64-max-plus-one, epoch zero, a zero-padded valid timestamp, scientific
notation, and an embedded NUL byte) plus five dedicated tests (valid-timestamp still
works; valid-timestamp-with-corrupted-signature still cleanly 401s; invalid-timestamp-
with-a-signature-correctly-computed-over-that-bad-string still cleanly 401s AND its
nonce remains unburned and reusable by a subsequent valid request; the secret is never
present in any auth-error response body) — all pass. No 500, no leaked exception text,
in any case.

The 20-character length gate was explicitly probed for a bypass: a 20-digit string
passes the length gate and is parsed to a very large (but not overflow-prone, since
Python integers have no bound) integer, then cleanly rejected by the ordinary
window-arithmetic check as "outside window" — not a crash, and not a special case. A
21-digit string is rejected at the length gate itself, before parsing. Both boundaries
produce a clean 401; neither produces a security bypass, since the length check is a
cost bound, not a trust decision — the actual security property (timestamp within
`REQUEST_TIMESTAMP_WINDOW_SECONDS`) is enforced by the arithmetic comparison
regardless of which path rejects first.

One case (non-ASCII Arabic-Indic digit characters) could not be tested through the
ASGI test transport: `httpx` itself raises `UnicodeEncodeError` before the request
reaches the application, because HTTP header values are ASCII/latin-1 only at the
protocol level (RFC 7230). This is not an application gap — a real HTTP client or
server would reject such a header at the same layer, before this code could ever see
it — so it is noted, not treated as an untested risk.

## 8. I2 Result — Independent Idempotency Isolation

**I2 CLOSED.**

Freshly written tests (not reusing the correction's own matrix): same-key/same-
payload is a genuine idempotent duplicate (1 device row after two identical
deliveries); same-key/changed-payload rejects only that record; four new batch
compositions (`[valid, conflict]`, `[conflict, valid]` in reverse order, `[conflict,
conflict, valid]` — the SAME conflicting key appearing twice, `[valid, conflict,
conflict, valid]`) all isolate correctly with the DB state independently inspected
after each. Namespace enforcement was independently confirmed rather than assumed:
the idempotency key is `f"{collector_id}:{dedup_key}"` — `integration_id` is not part
of it — so the SAME collector reusing a `dedup_key` against a DIFFERENT integration is
correctly treated as a conflict (the payload hash differs because `integration_id` is
part of the record body), not silently accepted as an unrelated second record; two
DIFFERENT collectors sharing the same textual `dedup_key` do not collide (per-
collector isolation confirmed). No stuck `"processing"` claims were found after any
rejection.

## 9. I3 Result — Independent Reconciliation Race

**I3 CLOSED.**

Pushed beyond the correction's own 10-way test: 25 genuinely concurrent accept-vs-
accept requests and 25 genuinely concurrent reject-vs-reject requests on the same
diff, each on its own DB session/connection. Both produce exactly one 200 and 24
clean 409s, with exactly one `reconciliation.accept`/`reconciliation.reject` audit
entry — no accidental double-success, no duplicate audit trail, at 25-way scale.

**Winner rollback**, independently constructed: a manually-controlled session
acquires `SELECT ... FOR UPDATE` on the diff directly (not through the HTTP
endpoint, so the test can hold the lock open on purpose), a genuinely concurrent HTTP
decision request is launched and proven to block (a 0.5s delay before release, so it
is not a lucky race), the holder then rolls back without ever deciding anything, and
the waiter is confirmed to acquire the row and complete its own legitimate decision
cleanly (200, not a hang, not an error) once the lock is released.

**Lock wait is not a server error**: confirmed directly by the same test — the
blocked waiter's eventual response is a clean 200, never a timeout or 500. No
`lock_timeout`/`statement_timeout` is configured anywhere in the application (grepped
the full `app/` tree), so a normal, bounded lock wait is never misclassified as a
server error by this code.

Reconciliation authorization was independently re-verified end-to-end (not merely
read from the RBAC seed table): a user with only the `Viewer` role, which the RBAC
seed grants `discovery:read` but explicitly not `discovery:reconcile`, receives a
clean 403 attempting to reject a pending diff, and the diff remains `"pending"`
afterward.

## 10. I4 Result — SAVEPOINT / Partial Batch Isolation

**I4 CLOSED.**

Three freshly written batch compositions beyond the correction's own matrix:
`[bad, bad, bad, good]` (three consecutive failures, not two), `[good, bad,
duplicate, good]` (a genuine duplicate mixed with a failure), and `[good, conflict,
bad, good]` (an idempotency-conflict failure and an authorization failure mixed in
the same batch) — all isolate correctly, with every record's DB-state outcome
independently checked (not just the ACK response), and collector identity confirmed
still usable via an ordinary follow-up request after each. A dedicated audit/outbox
test confirms a rejected record produces zero `DeviceDiscovered`/
`ReconciliationRequired` outbox rows (checked via a join on the specific device/diff
IDs involved, not a payload-text substring match, after an initial substring-based
draft of this test gave a misleading `1` instead of `2` for the accepted sibling's own
outbox count — corrected once the cause was identified: `ReconciliationRequired`'s
payload references the device by UUID, not by its external identifier).

## 11. Outer-Commit / Ambiguous-ACK Analysis (CRITICAL section)

This was independently, genuinely fault-injected — not accepted on the correction
report's own say-so. The fault wrapper always calls (or deliberately does not call)
the REAL `AsyncSession.commit()`; nothing about the underlying database transaction
itself is mocked.

**Finding A — the ambiguous ACK is real, but the core invariant survives it.**
When `await db.commit()` (line ~399 of `collectors.py`) genuinely succeeds at the
database level but the calling coroutine never learns that (simulating a connection
cut between server acknowledgment and driver confirmation), the client is told
`"rejected"` while the database durably holds `"accepted"` — confirmed directly by
querying actual device-row and idempotency-claim state after the fact.
**However**, the master prompt's own named invariant — *no duplicate authoritative
state, no permanent local-queue poisoning* — was directly tested and holds: a client
that believed `"rejected"` and retries the identical record observes `"duplicate"`,
not a second `"accepted"`, and no second device row is created.

**Finding B — a PERSISTENT (not one-shot) commit failure breaks the per-record ACK
contract for the rest of that batch.** When the commit failure is not transient —
`idem.release_claim`'s own separate `db.commit()` call (in the `except` branch) also
fails — that second failure is not itself guarded by any further `try`/`except` and
propagates out of `ingest_batch` entirely, past FastAPI's handler, as an unhandled
exception (observed directly as a raised exception through the ASGI test transport,
matching what a real ASGI server would log as an unhandled exception and terminate
the response on). The next record in that same batch is never attempted. A ONE-SHOT
transient failure (only the first commit call fails; `release_claim`'s own commit
succeeds) does NOT have this problem — the next record in the batch is confirmed to
process normally afterward.

**Finding C — the "stuck" idempotency state this produces is bounded, not
permanent.** After a persistent-failure request (replicating what production's own
`get_db` dependency does on an escaped exception — `await session.rollback()` — since
this test's dependency override does not itself replicate that, and the raw
same-session read was confirmed to otherwise show a misleading dirty-read "completed"
status that a genuinely separate connection would never see), the claim is left at
`status = "processing"` — not silently `"completed"` and not deleted. An immediate
retry is told to wait (`"rejected"` with a `"...retry shortly"` message — an honest
signal, not silent data loss or a silent double-accept). The codebase's OWN existing
`STALE_CLAIM_TIMEOUT` (30 seconds) reclaim mechanism (`_try_reclaim_stale`, unrelated
to and pre-dating this correction) was independently exercised (by backdating the
claim's `updated_at`, not by an actual 30-second sleep) and confirmed to recover the
claim cleanly, letting the retry succeed with exactly one device row created.

**Classification: MEDIUM, NON-BLOCKING.** The master prompt's explicit closure-
blocking bar is the WAN store-and-forward invariant itself (no duplicate authoritative
state, no permanent poisoning) — both were directly tested and both hold, in every
scenario exercised, including the worst one (a persistent connection-level failure).
The uncaught-exception-crashes-the-rest-of-the-batch behavior under Finding B is a
real robustness/observability gap worth a future hardening pass (e.g., guarding
`release_claim`'s own commit, or catching at the batch level to still return a
well-formed `IngestBatchOut` with the untried records marked for retry) — but it is
largely an inherent property of a genuinely dead database connection (any subsequent
statement on that same connection would fail regardless of how defensively this one
call site is wrapped), not a correctness or data-integrity defect, and a client
receiving no clean response for a batch has no rational choice but to retry the whole
batch later anyway — which the idempotency mechanism already makes safe, per Finding
A/C above.

## 12. Edge Collector Simulator Result

21/21 passed, 0 xfailed, re-run fresh at corrected HEAD. Independently audited the
simulator's own imports (`tests/api/test_phase8_edge_collector_simulator.py`):
imports only `compute_signature` (a genuine, unavoidable client-side operation any
real Edge Collector implementation would also need to perform locally — not a
shortcut into the server's internals) and a Phase 3 site-fixture helper; every
collector-role action goes through `await client.post(...)` against the real HTTP
API, never a direct call into `ingest_discovery`, `record_heartbeat`,
`accept_reconciliation`, or any other internal service function. Confirmed: no
"secretly using internal central service calls to perform the collector's role."
Covers registration, secret acquisition, capabilities, heartbeat, assignment, signed
ingestion, duplicate/delayed/out-of-order delivery, partial ACK, retry, restart-with-
same-identity, nonce replay, stale/future timestamp, modified body, wrong secret,
disabled collector, cross-collector/cross-site integration theft, oversized batch,
oversized/malformed record. Minor cosmetic note: the scenario numbering in test
function names has small gaps (no distinct `sim_05`/`sim_14`, some scenarios folded
into a shared test function) — every required category is still covered by some
test; this is a pre-existing labeling artifact from the original independent audit's
simulator build, not a functional gap, and is INFORMATIONAL/NON-BLOCKING.

## 13. Collector Authentication Result

Machine trust boundary independently re-confirmed by direct source read of
`collector_auth.py`'s `verify_collector_request`: collector-exists-and-active →
timestamp → signature (`hmac.compare_digest`) → nonce claim, in that order, unchanged
by the correction except for I1's narrower timestamp-parsing fix. Two new tests
empirically prove the trust boundary is structurally separate from human RBAC: a
collector's own valid HMAC signature over its own registration request does not
satisfy `POST /api/v1/collectors` (a human-RBAC-protected endpoint) — rejected with
401/403/422, not accepted; conversely, a valid human JWT bearer alone (no
`X-Collector-*` headers) does not authenticate to a collector-only endpoint. A
collector naming a syntactically valid but entirely nonexistent `integration_id`
(not merely one assigned elsewhere) is cleanly rejected per-record, not treated as
implicitly authoritative.

## 14. Nonce/Replay Result

Unchanged by this correction (the I1 diff touched only the timestamp-parsing block,
never `claim_nonce` or its atomic `ON CONFLICT DO NOTHING` claim). Re-confirmed via
the I1 test suite: an invalid-timestamp request claims zero nonce rows (verified by
direct `collector_request_nonce` table query) and the same nonce remains valid for a
subsequent genuinely valid request. `sim_17_reused_nonce_rejected` re-passed. Nonce
uniqueness remains DB-enforced (unique index on `(collector_id, nonce)`, untouched).

## 15. Site Isolation Result

`sim_21`/`sim_22` (cross-collector and cross-site integration theft) re-passed. A new
test independently confirms the "stale assignment after reassignment" case the master
prompt names explicitly: after Integration X is reassigned from Collector A to
Collector B, Collector A's now-stale assignment is cleanly rejected on a subsequent
ingest attempt (per-record `"rejected"`, zero device rows), while Collector B's fresh
assignment succeeds normally. No site-scoped human RBAC was introduced or is present
— machine authorization (per-collector assignment) and human RBAC (global roles)
remain the two separate concepts the architecture intends.

## 16. Assignment Concurrency Result

`test_concurrent_reassignment_race_leaves_exactly_one_current_assignment` (pre-
existing, part of the full suite re-run in §21) re-passed with no regression. The
partial unique index — `uq_collector_assignment_current_per_integration UNIQUE, btree
(integration_id) WHERE effective_to IS NULL` — was independently re-confirmed present
via `\d collector_assignment` against a freshly migrated scratch database (§23), not
merely re-trusted from a prior report: at most one current assignment per integration
is a real, DB-enforced constraint, not only an application-level check.

## 17. Discovery Boundary Result

Independently re-confirmed by source inspection (`grep` across
`discovery_service.py` and `collectors.py` for every `ManagedAsset(`/`Rack(`/
`Equipment(`/`PowerNode(`/`PowerConnection(`/`SpatialObject(`/`.add(` construction):
the only two objects ever `.add()`ed by discovery/ingest code are `DiscoveredDevice`
and `ReconciliationDiff`. `accept_reconciliation` remains the ONE place
`DiscoveredDevice.matched_managed_asset_id` may be set, and it only links to an
already-existing `ManagedAsset` (never creates one) — confirmed unchanged by this
correction's diff. No automatic authoritative inventory mutation exists anywhere on
the collector ingestion path.

## 18. Reconciliation Result

Covered jointly with §9/§11 above. Viewer role confirmed (empirically, not just from
the RBAC seed table) unable to accept or reject; `discovery:reconcile` is required and
unchanged; the actor is recorded on every decision (`decided_by_user_id`); a race
loser never produces a second `reconciliation.accept`/`reconciliation.reject` audit
entry, confirmed up to 25-way concurrency.

## 19. Driver Portability Result

`app/application/drivers/` (`base.py`, `icmp.py`, `rest.py`, `snmp.py`) independently
re-inspected import-by-import: none imports `sqlalchemy`, `AsyncSession`,
`app.db.*`, or `app.application.rbac`. `grep`-confirmed zero matches for any of those
across every driver file. The architectural separation (`connect/poll/disconnect/
normalize`, protocol/vendor identity never leaking outside this package) holds at
corrected HEAD exactly as before this correction (the diff never touched this
directory). Remains portable enough to be packaged into a future Phase 8B Edge
Collector process without a central-DB dependency to strip out first. No Phase 8B
work was performed or implied by this confirmation.

## 20. SNMP Status

Honestly scoped, independently re-read in full: the abstraction (`SNMPTransport`
protocol, `MetricMapping`, `SNMPDriver`'s `connect/poll/disconnect/normalize`
contract) is real and unit-tested; `SNMPDriver.connect()` explicitly raises
`DriverConnectionError` in the absence of a real `transport_factory`, rather than
silently falling back to a fake one — confirmed by `grep`ing the rest of the codebase
(`app/application/collector_service.py`, `app/api/`) for `SimulatedSNMPTransport`:
zero matches outside the driver module and its own tests. No test uses the simulated
transport and labels it as real-device validation; the module's own docstring
discloses "no real SNMP agent exists in this sandbox to poll against" plainly. Real
SNMP wire transport remains deferred, disclosed, not implemented — consistent with
the master prompt's own instruction not to attempt every vendor's MIB in this phase.

## 21. Polling Status

`run_polling_cycle` independently re-read in full: per-integration `try`/`except`
(one `DriverConnectionError` branch, one deliberate broad `except Exception` branch
explicitly commented as the sole intentional broad catch, both confirmed pre-existing
and unchanged by this correction's diff) means one integration's driver failure
cannot abort the cycle for its siblings; disabled integrations (`not
integration.enabled`) are skipped via a plain `continue` before any driver is
invoked. Manual polling (calling `run_polling_cycle` directly) works as exercised by
this validation's own performance measurement (§22). An automatic scheduler remains
absent — confirmed by `grep`ing for any cron/interval-loop construct around this
function — matching the deferred-scope disclosure already on record.

## 22. Audit/Outbox Result

Independently re-verified, not re-trusted: a rejected ingest record leaves 0
`discovered_device` rows, 0 `reconciliation_diff` rows, and 0 `outbox_event` rows for
that record (checked via ID-based joins, corrected after an initial substring-based
query gave a misleading count for the sibling accepted record — see §10). Every
racing reconciliation decision (up to 25-way) produces exactly one terminal audit
entry, confirmed by direct `audit_log` query, never one per losing request.

## 23. Performance Measurements

Freshly, independently re-measured (new file
`tests/api/test_phase8_final_closure_performance.py`, reusing the same correct
`before_cursor_execute` counting technique the existing performance suites
established — there is only one correct way to count real SQL statements, so reusing
a correct technique is not the same as reusing a claimed result) against corrected
HEAD:

| Measurement | n=1 | n=10 | n=50 | n=100 | n=500 |
|---|---|---|---|---|---|
| `GET /api/v1/collectors` (query count) | 4 | 4 | — | 4 | 4 |
| `GET /api/v1/integrations` (query count) | 4 | 4 | — | 4 | 4 |
| `run_polling_cycle` (query count) | 8 | 62 | 302 | — | — |

`GET /collectors` and `GET /integrations` remain genuinely flat (no N+1) at every
measured scale. `run_polling_cycle` grows linearly: `(62-8)/9 = 6.0` and
`(302-8)/49 = 6.0` queries per additional integration — an internally consistent
~6 queries/integration, closely matching Finding I5's own original estimate
(~5-6/integration). **This independently reconfirms I5 is still present, exactly as
disclosed, unmodified by this correction** (`run_polling_cycle`'s own code was never
touched by the I1-I4 diff). I5 was NOT optimized in this validation, per the master
prompt's explicit instruction. A first draft of this polling measurement used an
unreachable `203.0.113.1` (TEST-NET-3) target and produced a flat `query_count=3`
regardless of scale — that number was the FAILURE path's cost, not the SUCCESS path
`ingest_discovery` drives I5's actual growth on, and is disclosed here rather than
silently discarded; the corrected measurement above uses `127.0.0.1` (loopback,
matching the ORIGINAL I5 measurement's own setup), which genuinely succeeds and
exercises the code path I5 is actually about.

## 24. Migration Result

`alembic heads` confirms a single head, `0008_phase8`, unchanged. Independently
validated against a freshly created scratch database
(`dcim_migration_final_check`, not the long-lived `dcim_test`), not by trusting any
prior migration report:

- **fresh → head**: FAILED on the first attempt with `data type uuid has no default
  operator class for access method "gist"` on migration `0004`'s exclusion
  constraint. Root-caused (not dismissed): the `btree_gist`/`pgcrypto`/`uuid-ossp`
  extensions are created out-of-band by
  `backend/scripts/docker-initdb/01-create-app-role.sh` (Docker's
  `docker-entrypoint-initdb.d` mechanism) or the equivalent manual `README.md` setup
  step — never by an Alembic migration itself — and my scratch database, created via
  a bare `CREATE DATABASE`, skipped that bootstrap step. This is a **pre-existing
  Phase 1 infrastructure characteristic** (the script's own header comment
  cross-references `PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md` Finding C1), not a
  Phase 8 defect and not touched by this correction. Once the scratch database was
  bootstrapped with the same three `CREATE EXTENSION IF NOT EXISTS` statements the
  documented script runs, `alembic upgrade head` succeeded cleanly through all 8
  migrations in order.
- **head → 0007_correction**: downgrade succeeded cleanly.
- **0007_correction → head**: re-upgrade succeeded cleanly.
- **Re-running `alembic upgrade head` a third time** (already at head) was a clean
  no-op, no error.
- **Phase 8 tables present**: `collector`, `collector_assignment`,
  `collector_capability`, `collector_heartbeat`, `collector_request_nonce`,
  `discovered_device`, `integration`, `reconciliation_diff` — all confirmed via
  `\dt`.
- **Partial unique index confirmed**:
  `uq_collector_assignment_current_per_integration UNIQUE, btree (integration_id)
  WHERE effective_to IS NULL`, via `\d collector_assignment`.
- **RBAC seed idempotency**: confirmed both empirically (the head-reapplication
  no-op above) and by direct source reading of migration `0008`'s seed logic — it
  reads existing `permission`/`role_permission` rows first and only inserts codes
  genuinely missing (`if (resource, action) in permission_ids: continue`), the same
  idempotent pattern migrations `0004`/`0006` already established; re-running it
  against an already-seeded database would insert nothing, not raise a duplicate-key
  error.

## 25. Backend Exact Result

Full `pytest -q` run against corrected HEAD plus this validation's own new test
files, exact summary line reproduced verbatim:

```
479 passed, 12 warnings in 196.55s (0:03:16)
```

0 failed, 0 skipped, 0 xfailed, 0 xpassed. The 12 warnings are all instances of the
same pre-existing `StarletteDeprecationWarning` about `HTTP_422_UNPROCESSABLE_ENTITY`
already disclosed in both prior Phase 8 reports (11 pre-existing instances, plus 1
new instance from this validation's own `test_human_jwt_alone_cannot_satisfy_
collector_endpoint` test triggering the same deprecated code path) — not a new
warning class. This number was copied directly from a single, complete, freshly
executed run immediately before writing this report, not derived by arithmetic from
either prior report's own count (per this task's explicit instruction not to repeat
the earlier 371-vs-372-style inconsistency).

## 26. Frontend Result

```
npm run typecheck   -> clean, no output, no errors
npm run lint        -> clean, no output, no errors
npm run build       -> succeeded: "✓ 126 modules transformed" / "✓ built in 1.89s"
```

No frontend files were changed by the I1-I4 correction (backend transaction/
concurrency/validation fixes only, no API contract change), so this is confirmation
of no regression. `package.json`'s `scripts` block contains `dev`, `build`,
`typecheck`, `lint`, `preview` — **no `test` script exists**. No frontend automated
test suite is claimed to have run, because none exists to run; this is stated
plainly rather than left ambiguous.

## 27. Security Sweep

Every match was individually read, not merely counted:

- **Bare `except:`**: zero matches anywhere in `app/`.
- **`TODO`/`FIXME`/`HACK`**: zero matches anywhere in `app/`.
- **`verify=False`/insecure TLS**: zero matches.
- **`subprocess`/`shell=True`/`os.system`**: zero real matches (the one grep hit is
  prose in `svg_sanitizer.py`'s own docstring discussing subprocess isolation as an
  architectural aside, not actual subprocess usage).
- **Secret logging**: zero matches in source; the plaintext collector secret is
  returned exactly once in the registration HTTP response body and never logged;
  `SecretDecryptionError`'s message is a static, generic string that never includes
  the ciphertext or any derived value — confirmed by reading `app/core/secrets.py`
  in full.
- **Broad exception handling**: three `except Exception` sites in the Phase 8 code
  reviewed individually — `collectors.py:401` (the I2/I4 fix itself, already
  extensively analyzed in §11), and `collector_service.py:258`/`:378`
  (`run_polling_cycle`'s deliberate, pre-existing, explicitly-commented single
  intentional broad catch for driver failure isolation, unchanged by this
  correction). None is a silent swallow — every one records the failure on a
  per-record or per-integration result and re-surfaces it.
- **Lazy ORM access in unsafe async contexts / session rollback misuse**: this is
  exactly what I4 fixed and §11 stress-tested further; no new instance was found.
- **Driver DB coupling / hidden central dependencies**: zero, confirmed in §19.
- **Mutable global state**: zero matches at module scope in any Phase 8 file.
- **Unbounded payloads — NEW FINDING**: `CapabilityDeclareIn.protocol_codes` (a
  bare `list[str]` with no `Field(max_length=...)`) and `IntegrationCreateIn`/
  `IntegrationUpdateIn.config` (a bare `dict`) have no explicit size bound, unlike
  `IngestRecordIn.raw_attributes` (explicitly bounded to `MAX_RAW_ATTRIBUTES_BYTES` =
  8192 after the original audit's own Finding S1). `declare_capabilities` loops over
  every entry attempting one `INSERT` per unique code. Independently reproduced: a
  5000-entry capability list is currently accepted with no rejection
  (`test_security_sweep_capability_declaration_has_no_payload_size_bound`). **Pre-
  existing since the original Phase 8 implementation — not caused by, or touched by,
  the I1-I4 diff.** Classified **MEDIUM, NON-BLOCKING**: reachable only by an
  already-authenticated, already-registered collector (the same machine-trust tier
  the architecture already tolerates some misbehavior from elsewhere, e.g. a
  misbehaving driver), a resource-bounding hygiene gap rather than a correctness,
  authorization, idempotency, or data-integrity violation.

## 28. Remaining Findings

**New, this validation:**

| ID | Severity | Blocking | Summary |
|---|---|---|---|
| FV1 | MEDIUM | NON-BLOCKING | A PERSISTENT (not one-shot) failure at the outer `db.commit()` boundary breaks the per-record ACK contract for the rest of that one batch (uncaught exception, not a clean per-record response) — but the named store-and-forward invariant (no duplicate authoritative state, no permanent poisoning) holds in every case tested, including this one. See §11. |
| FV2 | MEDIUM | NON-BLOCKING | `CapabilityDeclareIn.protocol_codes` and `Integration.config` have no explicit payload size bound (unlike `raw_attributes`, which does). Pre-existing since the original Phase 8 implementation. See §27. |
| FV3 | INFORMATIONAL | NON-BLOCKING | Migration `fresh -> head` requires the `btree_gist`/`pgcrypto`/`uuid-ossp` extensions to already exist (created out-of-band by the documented Docker/README bootstrap step, never by Alembic itself) — a pre-existing Phase 1 characteristic, not a Phase 8 defect, surfaced only because this validation tested against a from-scratch database rather than the long-lived `dcim_test`. See §24. |
| FV4 | INFORMATIONAL | NON-BLOCKING | Edge Collector simulator test-function numbering has small labeling gaps (no distinct `sim_05`/`sim_14`); every required scenario category is still covered by some test. Cosmetic only. See §12. |

**Reaffirmed from `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`, unchanged, not touched by
this correction or this validation:**

- **I5** (MEDIUM, NON-BLOCKING) — `run_polling_cycle`'s near-linear query growth,
  independently re-measured at ~6 queries/integration (§23), not optimized here.
- **F1** (LOW, NON-BLOCKING) — no nonce/heartbeat pruning job. Not added.
- **F2** (LOW, NON-BLOCKING) — no collector-secret-rotation endpoint. Not added.
  Independently re-confirmed the same theme extends to `app/core/secrets.py`'s
  single static Fernet key for integration credentials — already disclosed in that
  module's own docstring as "an OPEN DECISION, not a finished production design."
- **F3** (INFORMATIONAL, NON-BLOCKING) — REST driver error message detail.
- **F4** (INFORMATIONAL, NON-BLOCKING) — frontend SNMP-limitation disclosure.
- **F5** (INFORMATIONAL, NON-BLOCKING) — out-of-order delivery `occurred_at`
  handling, a Phase 9 design point.

**No CRITICAL or HIGH finding, and no unresolved BLOCKING finding of any severity,
was found by this validation.**

## 29. Edge Collector Phase 8B Viability

The corrected central contract remains a viable, unmodified foundation for a real
Edge Collector runtime. Nothing in this validation required, or would have required,
a redesign of the central side to support Phase 8B's listed minimum capabilities: the
driver abstraction has zero central-DB/RBAC coupling (§19), the HMAC trust boundary
is a genuinely separate machine-authentication mechanism from human RBAC (§13), the
per-record ACK/idempotency contract is safe under retry, duplicate delivery, and
(per §11's stress test, the hardest case examined) even an ambiguous or persistent
commit-level failure — the one invariant Phase 8B's own store-and-forward queue would
actually depend on (no duplicate authoritative state, no permanent local poisoning)
holds under every fault condition this validation could construct. The one item worth
Phase 8B's own explicit attention (not a blocker to STARTING Phase 8B): a real Edge
Collector's retry/backoff logic should treat "no clean per-record ACK received at
all" (timeout, connection reset, an uncaught 5xx) as "retry the whole batch later" —
which is already the natural behavior any store-and-forward client implements, so no
special central-side accommodation is required, but Phase 8B's own design docs should
say so explicitly rather than leave it implicit.

## 30. MVP-Readiness Observation

Factual observation only, not a recommendation to begin the next phase now: the
now-independently-validated Phase 8 foundation (collector identity/auth, per-record
idempotent ingestion, non-authoritative discovery, human-gated reconciliation, and a
protocol-driver abstraction with no central-DB coupling) gives Phase 8B and a
subsequent minimal telemetry/alarms/dashboard MVP slice a stable base to build on
without needing to revisit anything this validation examined. The open, non-blocking
items (I5's linear polling-query cost, FV1's ambiguous-ACK edge case, FV2's unbounded
capability payload) are the kind of hardening a pass ahead of a general production
rollout would naturally address, not architectural blockers to starting that next
work.

## 31. Final Gate

Every BLOCKING finding from the independent red-team audit (I1, I2, I3, I4) was
independently re-verified as genuinely fixed, using freshly written adversarial
tests that go beyond the correction's own test matrix in every dimension the master
prompt specified (fresh timestamp edge cases, fresh idempotency batch compositions
and namespace checks, 25-way reconciliation races plus winner-rollback and lock-wait
behavior, fresh SAVEPOINT-isolation compositions including a genuine database
constraint violation). The CRITICAL outer-commit/ambiguous-ACK boundary was
genuinely fault-injected (not merely reasoned about) and found to preserve the one
named closure-blocking invariant — no duplicate authoritative state, no permanent
local-queue poisoning — under every condition constructed, including a persistent
connection-level failure. No new CRITICAL or HIGH finding was produced. The new
MEDIUM findings (FV1, FV2) do not violate a core Phase 8 correctness, security,
machine-trust, idempotency, or store-and-forward invariant, and are therefore
classified NON-BLOCKING rather than closure-blocking, per this task's own explicit
classification rule. Full backend regression is 479 passed / 0 failed / 0 xfailed /
0 xpassed; frontend typecheck, lint, and build are all clean; migration is verified
fresh-to-head, downgrade, and re-upgrade against a genuinely from-scratch database.

**PHASE 8 CLOSED — VERIFIED**
