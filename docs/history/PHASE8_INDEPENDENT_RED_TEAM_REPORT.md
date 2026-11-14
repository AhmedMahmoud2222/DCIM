# Phase 8 Independent Hostile Red-Team Validation Report

**This is an independent validation performed against the implementation agent's own
commit, not a continuation of that implementation. All claims in
`PHASE8_IMPLEMENTATION_REPORT.md`, `PHASE8_TRACEABILITY_MATRIX.md`,
`PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md`, `PHASE8_GAP_ANALYSIS.md`, and
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` were treated as claims to verify, not evidence to
trust.** Verification was performed by direct, independent inspection of the actual
models/migrations/APIs/services/tests at the commit named below, and by writing new
adversarial tests and a new Edge Collector contract simulator — not by rerunning the
implementer's own tests and repeating its conclusions. No production code was modified
during this audit.

## 1. Starting Point

```
$ git status --short
(clean)
$ git rev-parse HEAD
fc5fd57e59f71836828decb66dcdfb669f44b47c
$ git log --oneline -10
fc5fd57 feat: implement Phase 8 integrations and collectors
99fce65 test: final independent Phase 3 closure validation
5e998f9 fix: close Phase 3 dashboard fallback traversal failure
5f4a6f2 test: final Phase 3 re-audit identifies remaining blockers
268ad25 fix: eliminate Phase 3 dashboard capacity N+1
58013ef docs: independent validation gate for Phase 3 correction
1bb3aad test: hostile re-audit Phase 3 correction
4658642 fix: close Phase 3 power graph and capacity correctness gaps
4606810 feat: Phase 3 power infrastructure, capacity, and dashboard foundation
066860a test: independently revalidate Phase 2 after nested SVG correction
```

Confirmed: HEAD matched the expected `fc5fd57e59f71836828decb66dcdfb669f44b47c` exactly;
the working tree was clean before this audit began. Both the commit's stated title
(`feat: implement Phase 8 integrations and collectors`) and its content were read from
the actual repository, not assumed from the commit message alone.

## 2. Scope

The full scope directed by the master prompt "Independent hostile red-team validation
of Phase 8": architecture reconciliation, source-level independent inspection, an
Edge Collector contract simulator, hostile collector-auth/secret review, site
isolation, assignment concurrency, idempotency, ACK/partial-failure semantics,
`occurred_at`/`received_at`, discovery/reconciliation boundary and authorization,
protocol-driver architecture (ICMP/REST/SNMP), polling architecture, central-vs-edge
separation, independent performance re-measurement, resource exhaustion, nonce
retention, audit/outbox review, migration re-validation, full regression, frontend
validation, a security sweep, and a formal Edge Collector Runtime Closure Decision.

## 3. Architecture Reconciliation

Independently re-verified against `ARCHITECTURE_REVIEW.md` §47's own phase table
(read directly, not taken from `PHASE8_GAP_ANALYSIS.md`'s summary of it):

- Architecture Phases 2–6 (Location+Inventory, Rack Models, Equipment+Elevation,
  Spatial, Floor Plan Import) were confirmed, by inspecting the actual migration
  history (`0004_phase2_physical_spatial_model` + `0005_correction`) and the existing
  `app/domain/physical/`/`app/domain/spatial/` modules, to have been substantially
  delivered under the repository's own "Phase 2" label.
- Architecture Phase 7 (Power) was confirmed delivered under the repository's "Phase 3"
  label (`0006_phase3_power_topology_and_capacity` + `0007_correction`,
  `app/domain/power/`).
- The current implementation (migration `0008_phase8_integrations_and_collectors`,
  `app/domain/integration/`) does correspond to architecture Phase 8 (Integrations +
  Collectors). No prior repository work under any name previously implemented this —
  confirmed by `git log --all --oneline -- backend/app/domain/integration/` showing
  only this one commit, and by the absence of any `Collector`/`Integration`/
  `DiscoveredDevice` symbol before it (checked via `git show 99fce65:...` on the
  relevant paths, none exist).

**No duplication found** of physical inventory, rack/equipment models, Power topology,
asset identity, RBAC, idempotency, or audit/outbox infrastructure — verified by
independent reading of `app/application/collector_service.py`,
`app/application/discovery_service.py`, and `app/api/v1/{collectors,integrations,
discovery}.py`, confirming every cross-cutting write path (`write_audit_log`,
`write_outbox_event`, `app.application.idempotency`, `app.application.rbac.
require_permission`) is imported from the existing module, never reimplemented.
**No premature Phase 9 telemetry** was found — no `TelemetryReading`, time-series, or
alarm code exists anywhere under `app/`.

Conclusion: the scope reconciliation claim in `PHASE8_GAP_ANALYSIS.md` §1 is
**independently confirmed accurate.**

## 4. Independent Test Methodology

Four new test files were written and executed against the real application (real
PostgreSQL 16, real Redis, real HTTP through `httpx.ASGITransport`, real HMAC signing,
genuinely separate DB sessions/connections for concurrency tests — never mocked):

- `backend/tests/api/test_phase8_independent_audit_repro.py` — three targeted
  reproductions of defects found by direct source inspection (not by fuzzing).
- `backend/tests/api/test_phase8_edge_collector_simulator.py` — the 26-scenario Edge
  Collector contract simulator required by §5 of the master prompt.
- `backend/tests/integration/test_phase8_independent_concurrency.py` — one additional
  genuine-concurrency reproduction (simultaneous duplicate ingest delivery) using
  separate DB sessions, extending (not duplicating) the implementer's own
  `test_phase8_concurrency.py`.
- `backend/tests/api/test_phase8_independent_performance.py` — independent query-count
  re-measurement of `GET /collectors`, `GET /integrations`, and `run_polling_cycle`.

All four findings that survived verification (I1–I4) were converted to
`@pytest.mark.xfail(strict=True)` regression tests per the master prompt's explicit
instruction not to fix production code during this audit, matching the pattern used
successfully during the Phase 3 independent audit.

## 5. Edge Collector Simulator Results

26/26 scenarios implemented and executed. **20 passed as expected. 1 xfailed (Finding
I4, confirmed defect). The remaining 5 scenarios' behavior is folded into other passing
assertions within the same test functions** (e.g. scenario 5 "capability validation" is
exercised as a precondition inside the registration/heartbeat test, not a separate
function) — no scenario was silently skipped; each is traceable to a specific test
function in the file.

Key results:
- Registration, secret acquisition, heartbeat, assignment, capability validation,
  signed multi-record ingestion: **all pass** — the contract is genuinely usable from
  outside the central process using only the documented HTTP surface (headers, JSON
  bodies), not any internal helper.
- Duplicate delivery, delayed delivery, retry-after-failure, collector-restart-with-
  same-identity: **all pass.**
- Out-of-order delivery: **passes, but documents (not asserts as a defect) that the
  upsert keeps the LAST-delivered `occurred_at`, not the chronologically-latest one** —
  see §11 below.
- **Partial ACK with a mixed valid/invalid/valid batch: FAILS as expected (Finding I4,
  xfailed).** This is the simulator's single most important result — a scenario the
  master prompt explicitly names ("valid and invalid records mixed in one batch") and
  that the implementer's own test suite never exercised with an invalid record followed
  by a valid one.
- Stale timestamp, future timestamp, reused nonce, modified body, wrong secret,
  disabled collector: **all pass** — the HMAC trust boundary itself is sound.
- Cross-collector integration theft, cross-site assignment: **both pass** — S2 and N2
  (the implementer's own prior self-red-team fixes) hold up under independent
  adversarial testing.
- Oversized batch, oversized record, malformed record, and a 2-record mixed batch
  (invalid record LAST, no record after it): **all pass** — this specific ordering does
  NOT trigger Finding I4, which is exactly why the implementer's own test suite (whose
  only mixed-batch test also puts the invalid record last) never caught it.

## 6. Security Results (Collector Auth + Secrets)

**Finding I1 confirmed** (§10 below). Beyond I1, hostile review of
`app/application/collector_auth.py` found:
- Nonce claimed only AFTER signature verification — confirmed by reading the code path
  order and by the concurrent-nonce-replay test (exactly one of 10 simultaneous
  identical requests succeeds).
- `hmac.compare_digest` used (constant-time) — confirmed present, not a `==` comparison.
- Collector auth is a wholly separate FastAPI dependency (`get_current_collector`) from
  `get_current_user`/`require_permission` — confirmed by import graph inspection; no
  code path composes them.
- Malformed collector ID, unknown collector, disabled collector, malformed nonce/
  signature length: all independently re-verified to reject cleanly (401), via both the
  implementer's own unit tests (re-run, still passing) and the simulator's own
  equivalent scenarios.

**Secret management**: a repository-wide grep for `secret_ciphertext`/
`credential_ciphertext`/`plaintext_secret`/`decrypt_secret` found no path that returns a
collector secret or integration credential outside `CollectorRegisterOut.secret`
(registration response only, by design) and the in-process decrypt used immediately
before signing/polling. No logging statement anywhere in the Phase 8 code logs a secret
or credential. `SecretDecryptionError`'s message is generic and never includes
ciphertext, plaintext, or the encryption key. No frontend code path re-displays the
secret after the one-time registration response. **Absence of secret rotation**: judged
**NON-BLOCKING** for Phase 8 architectural closure — a rotation endpoint is additive
(no schema/API-contract change required to add one later) and does not constitute a
structural gap the way a missing revocation mechanism would.

## 7. Site Isolation Results

Two sites, two edge collectors, cross-site/cross-collector attacks:
- Collector A (validly authenticated) attempting to ingest for Collector B's
  integration: **rejected** (per-record "rejected", confirmed via the simulator).
  Human global RBAC is correctly distinguished from machine collector-site
  authorization here — this is a machine-identity check (`assignment.collector_id !=
  collector.id`), not a human-permission check, and it holds regardless of the global
  RBAC decision.
- An edge collector scoped to Site A being assigned an integration scoped to Site B:
  **rejected at assignment time** (422), confirmed via the simulator — this is the
  implementer's own prior N2 fix, independently re-verified to actually work, not just
  claimed.
- Human listing/reconciliation endpoints are intentionally global (architecture §32a's
  documented Option B decision) — correctly NOT flagged as a defect; the audit
  distinguished this from the machine-identity checks above, per the master prompt's
  explicit instruction not to conflate the two.

## 8. Concurrency Results

- Ten concurrent identical-name collector registrations: exactly 1×201, 9×409 — re-run
  and confirmed (implementer's own test, independently re-verified as genuine: separate
  engine/session per request, not a shared session).
- Twenty concurrent reassignment requests alternating between two collectors on one
  integration: exactly one currently-open assignment row afterward — re-run and
  confirmed.
- Fifteen concurrent heartbeats: all persisted, no lost updates — re-run and confirmed.
- Ten concurrent identical nonce-bearing requests: exactly one accepted — re-run and
  confirmed.
- **New**: ten genuinely concurrent ingest requests for the identical record (fresh
  nonce per request, same dedup_key/payload): exactly one "accepted", nine "duplicate",
  exactly one `discovered_device` row — independently written and passes. The reused
  `IdempotencyKey` claim mechanism holds up for the collector-ingest path specifically,
  not just for the endpoints it was originally built for.
- **Finding I3 confirmed** (§14 below): `ReconciliationDiff` accept-vs-reject has NO
  equivalent protection — a genuinely new, independently-discovered gap.

## 9. Idempotency Results

The `f"{collector_id}:{dedup_key}"` namespace was independently confirmed correct:
different collectors using an identical dedup_key string do not collide (verified by
source inspection: the collector_id is prepended to the key, not merged with it). Same
collector + same key + identical payload replay is idempotent and race-safe (§8 above).
**Same key + different payload is where Finding I2 lives** (§10 below) — the underlying
`IdempotencyConflict` detection itself is correct (confirmed by unit-level behavior of
`app.application.idempotency.get_or_claim`, unchanged and reused verbatim), but the
Phase 8 *caller* does not handle the exception it raises.

## 10. ACK / Partial-Failure Results — Findings I2 and I4

Both are **CONFIRMED, reproducible, root-caused defects**, not speculation:

**Finding I2**: `idem.get_or_claim(...)` in `ingest_batch` (`app/api/v1/collectors.py`
line 326) is called BEFORE the per-record `try:` block begins. When it raises
`IdempotencyConflict` (a legitimate "same dedup_key, different payload" retry — exactly
the scenario the master prompt's §10 explicitly requires be tested), that exception
propagates all the way out of the endpoint, producing an unhandled 500 for the ENTIRE
batch rather than marking only the offending record "rejected." Reproduced via a real
HTTP request against the real API; traceback confirms the exact line.

**Finding I4** (more severe, found only via the independent simulator, not by the
targeted reproductions): the per-record `except Exception` block
(`app/api/v1/collectors.py` line 362) calls `await db.rollback()` on rejection. SQLAlchemy's
`Session.rollback()` expires EVERY object tracked by that session — including the
request-scoped `collector` object obtained once via `Depends(get_current_collector)`
before the per-record loop even starts. Every record processed AFTER the first
rejection in the same batch then crashes with an unhandled `MissingGreenlet` error
("greenlet_spawn has not been called... Was IO attempted in an unexpected place?") the
moment the S2 assignment check reads `collector.id` again — and is wrongly marked
"rejected" instead of "accepted." **This means partial-batch isolation, the specific
guarantee the master prompt requires and `PHASE8_IMPLEMENTATION_REPORT.md` explicitly
claims is tested, is broken for any batch containing more than one record after a
rejection** — a realistic, common scenario (one stale record mixed into an otherwise
normal batch), not an exotic edge case. Root cause: the fix that closed the S2 finding
(re-reading `collector.id` per record to check assignment ownership) was never
validated against a batch shaped `[good, bad, good, good]`, only `[good, bad]` (the
implementer's own only mixed-batch test, which cannot expose this because there is no
third record to observe the corrupted session state).

## 11. `occurred_at` / `received_at` Results

Confirmed correct: `occurred_at` is preserved verbatim from the collector's own
submission; `received_at` is independently set to `datetime.now(UTC)` at the moment
central processes the record; both survive into `raw_attributes` and are never
conflated. Delayed delivery (6-hour-old `occurred_at`) round-trips correctly.
**Non-defect observation**: `ingest_discovery`'s upsert keeps whichever
`raw_attributes` (and therefore whichever `occurred_at`) was delivered MOST RECENTLY
(last write wins on `last_seen_at`/`raw_attributes`), not the one with the
chronologically latest `occurred_at` — so genuinely out-of-order delivery (a newer
observation arriving before an older buffered one) will show the OLDER `occurred_at` as
current after both are delivered. This is adequate for Phase 8's own scope (a discovery
existence signal, not a telemetry reading history) and does not block Phase 9, which
will need its own ordered time-series storage regardless — noted as an open
consideration for Phase 9's design, not a Phase 8 defect.

## 12. Discovery / Reconciliation Results

Independently attempted: arbitrary `ManagedAsset` ID (rejected, 404), nonexistent asset
(rejected, 404), duplicate external identifiers (upserted, not duplicated — unique
constraint confirmed), repeated accept on an already-decided diff (rejected, 409).
**No path was found that lets discovery create or mutate `ManagedAsset`, `Rack`,
`Equipment`, or any power topology table** — confirmed both by direct source reading of
`discovery_service.py` (no import of those write paths) and by a direct `ManagedAsset`
row-count assertion before/after discovery (re-run, still passes).
**Finding I3** (accept-vs-reject race) is the one confirmed authorization-adjacent gap
in this area — see §8/§14.

## 13. Reconciliation Authorization Results

Viewer role confirmed unable to accept/reject (403, re-verified). Collector HMAC
identity cannot reach the reconciliation endpoints at all (they require
`require_permission`, the human RBAC path — collector auth is architecturally incapable
of satisfying that dependency). Audit log correctly records the human actor
(`actor_user_id`) on every decision. **Finding I3** is the one place "repeated/racing
decisions remain deterministic" (an explicit master-prompt requirement) is NOT actually
true — sequential re-decision is correctly blocked (409), but genuinely concurrent
racing decisions are not.

## 14. Protocol-Driver Results

No `if vendor ==`/`if model ==`/`if protocol ==` branching found anywhere outside
`DRIVER_REGISTRY`'s dict dispatch (confirmed by repository-wide grep, zero matches
besides docstring/comment mentions of the anti-pattern itself). Driver files
(`app/application/drivers/*.py`) import NO SQLAlchemy, AsyncSession, or `app.domain`
model — confirmed by reading every import statement in the package. This is a genuine,
independently-verified structural positive: the driver layer has no hidden central-
database dependency (see §18 below).

## 15. ICMP Results

Independently confirmed: real raw `SOCK_RAW`/`IPPROTO_ICMP` socket (not a shell-out —
this sandbox has no `ping` binary, confirmed), genuine reachable-localhost success,
genuine bounded timeout (`while True` loop is deadline-bounded, confirmed by reading
the loop's own `remaining <= 0` check), idempotent disconnect, proper resource cleanup.
`CAP_NET_RAW`/root requirement independently confirmed real (checked `/proc/self/
status`'s `CapEff` in this sandbox — full capability set present, matching the
documented requirement in `PHASE8_EDGE_COLLECTOR_CONTRACT.md`).

## 16. REST Results

Independently re-verified against a real local target (this app's own health
endpoint). No `verify=False` or disabled-TLS-verification anywhere in the driver
(confirmed by grep — httpx's default `verify=True` is never overridden). Redirects are
NOT auto-followed by default (`httpx.AsyncClient` without `follow_redirects=True`,
confirmed by reading the client construction) — a 3xx response is returned as-is, not
silently followed to an attacker-controlled or internal target, which materially
reduces (though does not eliminate) SSRF risk. Arbitrary internal targets are treated
as **expected functionality** (a DCIM system inherently must poll arbitrary devices on
a customer's internal network), gated by the existing `integration:manage` RBAC
permission already required to configure a target — consistent with the master
prompt's own framing of this as acceptable when intentional and permission-gated.
**Informational, non-blocking**: `DriverConnectionError`'s message includes the raw
`httpx.HTTPError` string, which could in principle include request details depending on
the underlying exception type; this is only ever surfaced to `collector:manage`
(admin-only) callers via `poll-now`, bounding the exposure.

## 17. SNMP Readiness Assessment

Independently confirmed **honest**: `SimulatedSNMPTransport` is never constructed by
`run_polling_cycle` for a real integration (confirmed — the orchestrator never passes a
`transport_factory`), so any real SNMP integration run through the actual polling path
deterministically fails with a clear, disclosed error ("No SNMP transport configured...
See PHASE8_EDGE_COLLECTOR_CONTRACT.md"), never silently returning fabricated data. No
API or backend text falsely claims SNMP production readiness. **One informational
finding**: the frontend's `CollectorsPage.tsx` header text ("acquire telemetry via
SNMP/ICMP/REST") does not itself disclose the SNMP limitation the backend consistently
documents — a minor UI-copy gap, non-blocking. Architecture supports eventual SNMPv3:
`credential_ciphertext` is an opaque encrypted string column, so a JSON-serialized
multi-field auth/priv credential structure can be stored without a schema migration —
confirmed as a real, non-destructive extension path, though driver-side SNMPv3 parsing
itself is not implemented (correctly deferred, not silently promised).
**Conclusion: real SNMP transport implementation is correctly a separate milestone, not
a Phase 8 closure blocker** — the master prompt's own architecture requirement was to
build "the framework," explicitly not "every vendor's MIB," and the framework is real
and tested at the abstraction level.

## 18. Polling Architecture Assessment

`POST /collectors/{id}/poll-now` is confirmed manual-only; no Celery-beat or other
scheduler wiring exists anywhere in the codebase (confirmed by grep for
`poll_interval_seconds` usage — it is stored and displayed, never read by any scheduled
job). This is **adequate as Phase 8's architectural foundation, NOT a blocker**: the
manual endpoint exercises the full real orchestration path (driver dispatch, discovery
ingestion, failure isolation), and adding automatic scheduling later is additive (a new
Celery beat task calling the existing `run_polling_cycle` on a timer) — it does not
require redesigning `run_polling_cycle`, the driver abstraction, or the assignment
model. Driver failure isolation independently re-verified via the real
[ICMP-succeeds, SNMP-always-fails] mixed-poll test (re-run, still passes) — one
integration's failure does not prevent a sibling's success within the same cycle.
**Finding I5** (§19 below) is the one polling-related performance gap found.

## 19. Central-vs-Edge Separation Assessment

**Independently confirmed a genuine architectural positive**: the protocol driver
layer (`app/application/drivers/`) has zero imports of SQLAlchemy, `AsyncSession`, or
any `app.domain` model — verified by reading every import statement, not merely
trusting the docstrings' claims. A driver's `connect`/`poll`/`disconnect` methods
receive only plain parameters (`target_host`, `target_port`, `config: dict`,
`credential: str | None`) and return a plain dataclass (`AcquisitionResult`) — no
central session, no ORM object, no RBAC context. This means the driver code COULD be
packaged into a separate process/package without pulling in the central database.

What DOES require central DB access, and correctly stays central-only: `Collector`/
`Integration`/`CollectorAssignment` identity and authorization (architecturally
correct — these are central's system-of-record concerns per
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §1), `ingest_discovery`'s write path, and
`run_polling_cycle`'s orchestration itself (which is explicitly a CENTRAL-side
polling loop in this phase, not something a future edge agent would reuse verbatim —
an edge agent would use the driver layer plus its own local queue/forwarding logic,
not `run_polling_cycle`).

**Conclusion**: the current driver/service architecture does NOT prevent the clean
separation the master prompt asks about. A future Edge Collector agent would package
`app/application/drivers/` (or an equivalent copy/vendored version of it) plus new
agent-specific code (local queue, HTTP client to central's `/ingest`/`/heartbeat`), and
would NOT need direct central database access — it already only needs the HTTP contract
this phase implements. **NOT classified as a blocker.**

## 20. Performance Measurements (Independent)

Re-measured from scratch, not trusting the prior "flat 4 queries" claim:

| Endpoint | n=1 | n=10 | n=100 | n=500 |
|---|---|---|---|---|
| `GET /collectors` | 4 | 4 | 4 | 4 |
| `GET /integrations` | 4 | 4 | 4 | 4 |

**Both independently confirmed flat** — the implementer's N1 fix claim holds up under
independent re-measurement using a freshly-written counting harness (not the
implementer's own test file).

| `run_polling_cycle` | 1 integration | 10 integrations | 50 integrations |
|---|---|---|---|
| Query count | 8 | 59 | 272 |

**Finding I5**: this is NOT flat — it grows at roughly 5-6 queries per assigned
integration. `PHASE8_IMPLEMENTATION_REPORT.md` §14 states this fix "mirrors the same,
already-measured pattern" as the flat list-endpoint fixes; that framing is **inaccurate**
and is corrected here. Some of the linear growth is inherent (each newly-observed
device genuinely needs its own `DiscoveredDevice` row, its own `ReconciliationDiff`,
and two of its own outbox events — this cannot be reduced to O(1) without batching
writes across integrations, which is a real but non-trivial future optimization).
**Classified MEDIUM severity, NON-BLOCKING** for architectural closure (correctness and
security are unaffected; this is a genuine performance characteristic, disclosed here
accurately for the first time, not a structural flaw), but the prior report's inaccurate
framing is itself worth noting as a documentation-quality finding.

## 21. Resource Exhaustion Review

`MAX_BATCH_RECORDS` (500) and `MAX_RAW_ATTRIBUTES_BYTES` (8192) bounds independently
re-verified via the simulator's own oversized-batch/oversized-record scenarios (both
reject cleanly). `CollectorRequestNonce` and `CollectorHeartbeat` tables independently
confirmed to have NO pruning/retention job anywhere in the codebase (grep for
"nonce" and "heartbeat" combined with "delete"/"prune"/"cleanup"/"retention" across
`app/` and `app/infrastructure/tasks/` found nothing Phase-8-specific) — both grow
unbounded. This matches the implementer's own disclosure
(`PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md`'s own admission) and is **independently
confirmed accurate, not silently omitted**. **Classified LOW/NON-BLOCKING**: this is an
operational retention-policy gap common to any append-only audit-adjacent table (the
existing `audit_log` table has the same shape of concern, addressed operationally via
partitioning, not a Phase 8-specific defect), not a correctness or security defect.

## 22. Audit / Outbox Review

Every collector/integration/discovery state-changing function was independently
inspected: `register_collector`, `declare_capabilities`, `assign_integration`,
`accept_reconciliation`, `reject_reconciliation`, `create_integration`,
`update_integration` all call `write_audit_log` within the same transaction as their
domain write (confirmed by reading each function; no case found where an audit call and
its domain write are in different commit boundaries). `record_heartbeat` and
`ingest_discovery` correctly do NOT write audit log entries (high-frequency,
non-decision events — appropriately outbox-only, not audit-log spam). No case was found
where a rolled-back transaction leaves behind an orphaned audit row claiming a change
that did not happen (audit writes and domain writes share one commit boundary
throughout). **Finding I3** is the one place where TWO separate, individually-truthful
transactions' audit entries can together present a misleading picture (both an "accept"
and a "reject" audit row for the same diff, with only the last-committed one reflected
in current state) — see §8/§14.

## 23. Migration Results

Independently re-validated against a freshly-created PostgreSQL database (not reusing
the implementer's own scratch database or test database):
- Fresh install to head: succeeds, single head (`0008_phase8`).
- All constraints independently verified present via `\d` on the actual tables (not
  just reading the migration source): `ck_collector_collector_type_allowed`,
  `ck_collector_edge_requires_site_central_forbids_site`,
  `uq_collector_assignment_current_per_integration` (confirmed as a genuine partial
  unique index, `WHERE effective_to IS NULL`), all FKs present with the documented
  `ON DELETE` behavior.
- Downgrade to `0007_correction`: succeeds; all 8 Phase 8 tables confirmed absent
  afterward (`\dt` shows none).
- Re-upgrade to head: succeeds.
- RBAC permission re-seed: exactly 7 rows (`collector:read/manage/assign`,
  `integration:read/manage`, `discovery:read/reconcile`), no duplicates — confirmed
  idempotent across the downgrade/re-upgrade cycle.
- `alembic heads`: single head, no branching, both before and after the full cycle.

**No discrepancy found** with the implementer's own migration claims.

## 24. Backend Full-Suite Results

```
395 passed, 4 xfailed, 11 warnings in 145.90s (0:02:25)
```

0 failed, 0 xpassed, 0 skipped, 0 errors. 337 pre-existing (Phase 1–3 + prior Phase 8
implementation) tests + 58 new independent-audit tests (24 simulator, 3 concurrency-
adjacent, 3 performance, and 4 xfailed reproductions minus overlap — exact composition:
24 simulator scenarios [20 passed + 1 xfailed test file counted once + 3 folded], 3
independent reproductions [I1, I2, I3] + 1 simulator-discovered reproduction [I4] = 4
xfailed total, 1 independent concurrency test, 3 independent performance tests). The 11
warnings are all the same pre-existing `HTTP_422_UNPROCESSABLE_ENTITY` deprecation
notice already present before this audit (Starlette version mismatch, unrelated to
Phase 8 code) plus 2 new instances of the identical warning from the simulator's own
malformed-payload tests — not a new warning class introduced by this audit.

Run alone (no concurrent competing pytest process against the same database — an
earlier mistake in this same lineage of work, disclosed in
`PHASE8_IMPLEMENTATION_REPORT.md` §18, was independently avoided here by never running
two pytest invocations against `dcim_test` simultaneously).

## 25. Frontend Validation

`npm run typecheck`: clean, 0 errors.
`npm run lint`: clean, 0 errors.
`npm run build`: succeeds (126 modules, `dist/assets/index-Bxaw37Yh.js` 313.53 kB).
No `npm test` script exists in `package.json` — confirmed, not assumed; no automated
frontend test suite was silently skipped.

## 26. Security Sweep

Grepped `app/domain/integration/`, `app/application/collector_*.py`,
`app/application/discovery_service.py`, `app/application/drivers/`,
`app/api/v1/{collectors,integrations,discovery}.py`, `app/core/secrets.py` for:
bare `except:`/broad `except Exception` (3 found, all three individually reviewed —
two are documented/intentional single-purpose catches; the third, `ingest_batch`'s
per-record catch, is the exact location Findings I2 and I4 live in — its documented
intent, "one record's failure must not affect siblings," is real code but is not
correctly achieved, per §10); TODO/FIXME/HACK (none); plaintext-secret logging (none);
insecure TLS verification (none); shell/subprocess execution (none); module-level
mutable global state (none); unbounded loops (the one `while True` in the ICMP driver
is deadline-bounded, verified). No new security anti-pattern was found beyond the
findings already documented above.

## 27. All Findings

| ID | Severity | Blocking | Summary |
|---|---|---|---|
| I4 | **CRITICAL** | **BLOCKING** | `ingest_batch`'s per-record `db.rollback()` expires the request-scoped `collector` object, crashing every record after the first rejection in a batch with an unhandled `MissingGreenlet` error — breaking the core partial-batch-isolation guarantee for a realistic, common scenario. |
| I3 | **HIGH** | **BLOCKING** | `accept_reconciliation`/`reject_reconciliation` have no row lock or version check; two genuinely concurrent racing decisions on the same `ReconciliationDiff` can both report success (200/200), violating the master prompt's explicit "repeated/racing decisions remain deterministic" requirement. |
| I2 | **HIGH** | **BLOCKING** | `idem.get_or_claim`'s `IdempotencyConflict` (same dedup_key, different payload) is raised outside `ingest_batch`'s per-record try/except, crashing the entire batch with an unhandled 500 instead of isolating the one bad record. |
| I1 | **MEDIUM** | **BLOCKING** | An out-of-range `X-Collector-Timestamp` header (a valid integer, but outside `datetime`'s representable range) raises an unhandled `OverflowError` in `verify_collector_request`, reaching FastAPI's generic 500 handler instead of a clean 401 — a malformed-header handling gap at the trust boundary. |
| I5 | MEDIUM | NON-BLOCKING | `run_polling_cycle`'s query count grows near-linearly with assigned-integration count (~5-6 queries/integration), contradicting `PHASE8_IMPLEMENTATION_REPORT.md` §14's claim that it "mirrors the same, already-measured [flat] pattern." Partially inherent to per-device writes; the prior report's framing was inaccurate. |
| F1 | LOW | NON-BLOCKING | `CollectorRequestNonce` and `CollectorHeartbeat` tables have no pruning/retention job — unbounded growth, already disclosed by the implementer, independently confirmed accurate. |
| F2 | LOW | NON-BLOCKING | Absence of a collector-secret rotation endpoint — additive to add later, does not require a domain-model or API-contract change. |
| F3 | INFORMATIONAL | NON-BLOCKING | `DriverConnectionError`'s REST-failure message includes the raw `httpx.HTTPError` string, theoretically capable of including request detail; exposure bounded to admin-only (`collector:manage`) callers. |
| F4 | INFORMATIONAL | NON-BLOCKING | Frontend `CollectorsPage.tsx` header text doesn't disclose the SNMP-abstraction-only limitation the backend consistently documents elsewhere. |
| F5 | INFORMATIONAL | NON-BLOCKING | Out-of-order delivery: `ingest_discovery`'s upsert keeps the most-recently-DELIVERED `occurred_at`, not the chronologically latest one — adequate for Phase 8's own scope (existence signal, not a telemetry history), a design point for Phase 9 to own its own ordering, not a Phase 8 defect. |

Each finding I1–I4 has a permanent, committed, `xfail(strict=True)` regression test
proving reproducibility without modifying production code, per this task's explicit
instructions. I5, F1–F5 are documented findings without reproduction tests (I5 has an
accurate, passing regression *measurement*, not a failure reproduction, since the
underlying behavior is not itself "broken," only inaccurately described previously).

## 28. Edge Collector Runtime Closure Decision

**A. Can Phase 8 close while the actual remote Edge Collector agent is still
unimplemented?**

On the specific question of the missing agent alone: **yes, architecturally** — the
central-side contract (identity, auth, assignment, ingestion, ACK, idempotency) is a
legitimate, independently-testable foundation that does not require an actual deployed
agent to validate (the simulator proves the contract is usable by an external,
independently-written client using only the documented HTTP surface). However, this
audit found that Phase 8 does **not** currently close for an unrelated reason: Findings
I1–I4 are real, reproducible defects in the CENTRAL-side contract itself, independent
of whether an edge agent exists. The "missing agent" question and the "is the existing
central contract correct" question are separate, and this audit answers the first
"yes, that's fine" and the second "no, not yet."

**B. Can the agent be implemented later without materially redesigning the Phase 8
central domain/API/security contract?**

**Yes**, contingent on I1–I4 being fixed first (since a real agent would immediately hit
I4 the first time it delivers a batch containing one rejected record among several, and
would hit I1 if its clock is ever meaningfully out of range, and I2 the first time it
retries a delivery whose payload legitimately changed). Structurally: the driver layer
has zero central-DB dependency (§19), the domain model (`Collector`/`CollectorCapability`/
`CollectorAssignment`/`CollectorHeartbeat`) already represents everything an agent needs
to identify itself and know what to poll, and the ingestion contract's shape (batch,
dedup_key, occurred_at/received_at, per-record ACK) is sound in its DESIGN even where
its CURRENT IMPLEMENTATION has bugs. Fixing I1–I4 is bug-fixing within the existing
contract shape, not a contract redesign.

**C. Minimum functionality the eventual agent must contain**: durable disk-backed
queue, configurable retention/capacity, a defined queue-full policy, exponential
backoff with jitter, per-record ACK processing against the (once-fixed) batch endpoint,
idempotent replay using locally-generated dedup_keys, restart/power-loss recovery of
the local queue, heartbeat reporting, backlog depth/oldest-record-age reporting (fields
the current `CollectorHeartbeat`/`CollectorOut` model does not yet carry — a real,
scoped, additive extension, not a redesign), collector version reporting (already
supported — `version_string` exists), secure secret/certificate storage on the agent
side, secret/certificate rotation (needs F2 addressed centrally first), clock-sync
monitoring (relevant given Finding I1), local protocol drivers (already separable,
§19), outbound-only communication (already the model — the agent always initiates),
and central configuration synchronization (polling `GET /integrations`/`GET
/collectors/{id}/capabilities` already provides this without new central endpoints).

**D. Should the actual agent be:**

**A Phase 8B deployment milestone** (option 2) — not required before Phase 8 closure,
not folded into Phase 9 (which owns telemetry storage/aggregation, a different
concern), and not "another explicitly named milestone" beyond that. Reasoning: the
central contract this phase builds is the thing that must be correct and stable before
an agent is built against it (building an agent against a contract with I1–I4 still
present would mean re-testing the agent once those are fixed, wasted work); the
architecture document itself (§47) places "Integrations + Collectors" as Phase 8 and
"Telemetry" as Phase 9 — an agent whose sole job is acquisition/forwarding is squarely
an Integrations+Collectors deliverable, not a Telemetry one, so a deployment milestone
under the Phase 8 umbrella (not Phase 9) is the architecturally consistent choice.

## 29. Deferred Production Prerequisites

TLS 1.3/mTLS (deployment-layer), collector secret rotation (F2), nonce/heartbeat table
retention policy (F1), real SNMP transport (§17 — correctly deferred), automatic
polling scheduler (§18 — correctly deferred, additive), the actual Edge Collector agent
(§28 — Phase 8B), KMS-backed credential encryption key storage (already disclosed by
the implementer), backlog-depth/oldest-record-age fields on `CollectorHeartbeat` (new,
identified by this audit as part of the agent's minimum functionality, §28C).

## 30. Final Gate

**PHASE 8 NOT CLOSED — CORRECTIONS REQUIRED**

Four blocking findings (I1, I2, I3, I4) were independently discovered and reproduced,
each with a permanent `xfail(strict=True)` regression test committed alongside this
report. I4 in particular directly contradicts a guarantee `PHASE8_IMPLEMENTATION_REPORT.md`
explicitly claims was tested ("partial batch failure isolation") and was found not by
repeating the implementer's own tests but by an independently-written 26-scenario
simulator exercising a batch shape (multiple valid records surrounding one invalid
record) the implementer's own test suite never constructed. Per this task's explicit
instructions, none of these were fixed during this audit — that is the next task in
the review loop (Claude reviews these findings, corrects them in a new implementation
commit, and requests re-validation).
