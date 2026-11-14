# Pre-MVP Consolidation Report

Repository consolidation and pre-MVP hardening gate for the DCIM repository, on new
branch `claude/pre-mvp-consolidation`. Does not implement Phase 9, telemetry, alarms,
the Edge Collector runtime, or the Mini-DCIM MVP. Produces one clean canonical
candidate branch containing all validated Phase 8 work, legitimate backend/CI
corrections, and the minimum security hardening required before Phase 8B / DCIM MVP
v0.1 begins.

## 1. Repository Branch Inventory

Verified directly (`git fetch origin --prune`, `git ls-remote origin`, `git log
--oneline` per branch) rather than trusting the task's own stated HEADs blindly —
every one matched exactly:

| Branch | HEAD (verified) | Matches expected? |
|---|---|---|
| `claude/phase8-independent-red-team` | `6af4e4ecec8f4bed3553cb35f16654d99f50c11c` | Yes |
| `claude/inspiring-edison-akoeoc` | `5b94bfab435f66da86cd9116e08a590f25c1b59c` | Yes |
| `claude/new-session-1vutvy` | `fc5fd57e59f71836828decb66dcdfb669f44b47c` | Yes |
| `codex/phase8-finding-verification` | `b52aaeb54cbfb24fe3afe5a8a2168248130e46bb` | Yes |
| `codex/phase8-independent-red-team` | `cb2f0f2add2eddca13698de8154b5306b84c2a5d` | Yes |

No discrepancy to investigate. `git status --short` on the starting checkout was
clean.

## 2. Default Branch State

`git remote show origin` reports the repository's default branch as
`claude/new-session-1vutvy`, HEAD `fc5fd57e59f71836828decb66dcdfb669f44b47c` — the
raw, pre-audit Phase 8 implementation commit, predating both the independent
red-team audit and its correction. This consolidation does not touch or merge into
the default branch.

## 3. Phase 8 Validated Source Branch

`claude/phase8-independent-red-team` at `6af4e4ecec8f4bed3553cb35f16654d99f50c11c` —
unchanged from the stated expected value, so no investigation of drift was needed.
`claude/pre-mvp-consolidation` was created directly from this commit
(`git checkout -b claude/pre-mvp-consolidation` from that branch). Confirmed present
and unmodified before any consolidation work began:

- `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`
- `PHASE8_BLOCKING_CORRECTION_REPORT.md`
- `PHASE8_FINAL_CLOSURE_VALIDATION.md`, whose final gate is exactly
  `PHASE 8 CLOSED — VERIFIED` (confirmed by reading the file's own last line, not
  assumed).

None of these three files, nor any other historical Phase 8 report, was edited by
this consolidation task.

## 4. Backend/CI Repair Branch Analysis

`claude/inspiring-edison-akoeoc`'s merge-base with `claude/phase8-independent-red-team`
is `fc5fd57e59f71836828decb66dcdfb669f44b47c` — it branched directly off the raw
Phase 8 implementation, sibling to (not descended from) the independent audit/
correction line. It contains exactly 5 commits, each read in full (not summarized
from its own commit message) before classification:

| Commit | What it does | Classification |
|---|---|---|
| `a82d6c3` fix: resolve ruff lint failures blocking CI | Line-wrapping/import-order fixes in `catalog.py`, `equipment.py`, `floor_plans.py`, `racks.py`, `domain/physical/models.py`, plus 3 test files (unused import/variable removal). Verified: zero behavior change, no logic touched. | **REQUIRED** |
| `115425c` fix: resolve mypy type errors blocking CI | `Sequence` vs `list` widening (`power_capacity.py`, `dashboard.py`), defensive `assert`s for invariants mypy can't see (`equipment.py`, `power_capacity.py`, `floorplan_import.py`), a `ProtocolDriver.__init__` base matching every concrete driver, and a `cast(Table, ...)` on `collector_auth.py`'s nonce table. The `collector_auth.py` change touches only `claim_nonce` (import block + one module-level `_NONCE_TABLE` constant + two `.__table__` call sites inside that function) — verified line-by-line against the independently-validated file: it does not touch `verify_collector_request`'s timestamp block (the I1 fix) at all. Auto-merged cleanly with zero conflicts; confirmed post-merge both the `_NONCE_TABLE` cast and the I1 fix's `len(timestamp_header) > 20` / integer-epoch comparison are present together. | **REQUIRED** |
| `3d144a7` fix: set CREDENTIAL_ENCRYPTION_KEY in CI | Adds one env var to `.github/workflows/ci.yml`'s backend job — required since Phase 8 introduced a mandatory `Settings.credential_encryption_key`. Confirmed missing from the pre-cherry-pick CI file. | **REQUIRED** |
| `8ca8630` fix: set TEST_ADMIN_DATABASE_URL in CI | Adds one env var matching the CI Postgres service's actual superuser password — without it, every test using the `_clean_tables` admin-engine fixture fails with `InvalidPasswordError`. Confirmed missing from the pre-cherry-pick CI file. | **REQUIRED** |
| `5b94bfa` fix: grant CAP_NET_RAW in CI | Adds a `sudo setcap cap_net_raw+ep` step before `pytest -q` in CI, for the ICMP driver's real raw-socket tests. Confirmed missing from the pre-cherry-pick CI file; confirmed still needed (this sandbox runs as root, masking the issue locally, but the ICMP driver's own docstring documents `CAP_NET_RAW`/root as a real, non-optional operational requirement). | **REQUIRED** |

No commit was found to be VALID-BUT-OPTIONAL, SUPERSEDED, CONFLICTING, or INCORRECT.
None of the 5 commits touches `collectors.py`'s `ingest_batch` per-record loop,
`discovery_service.py`'s `accept_reconciliation`/`reject_reconciliation`, or any other
code the I1–I4 correction changed — verified by diffing each commit against the base
independently, not merely trusting the commit messages. A manual semantic merge (as
opposed to a wholesale branch merge) was unnecessary because every one of the 5
commits either touches files entirely disjoint from Phase 8's own correction, or
(the one exception, `115425c`) touches a clearly disjoint region of a shared file —
`git cherry-pick` alone produced the correct result in every case, confirmed by
running the full backend suite and mypy immediately after each pick.

## 5. Commits Incorporated

All 5, cherry-picked onto `claude/pre-mvp-consolidation` in original order, each
verified individually (ruff/mypy/full suite) before proceeding to the next:
`075df73`, `ffd47dc`, `0114937`, `a06ab1b`, `a6366a7` (new SHAs on this branch — the
underlying diffs are unchanged from the originals, `115425c`'s auto-merge aside).

## 6. Commits Rejected/Superseded

None. Every commit on `claude/inspiring-edison-akoeoc` was legitimate and incorporated.

## 7. Merge Conflicts and Resolutions

One auto-merge (not a manual conflict resolution — Git resolved it via 3-way merge
with zero conflict markers) in `backend/app/application/collector_auth.py` during
`git cherry-pick 115425c` (the mypy fix), because the I1 correction had already
modified the same file. Verified clean by direct inspection immediately after: both
the mypy-added `_NONCE_TABLE = cast(Table, ...)` (used only inside `claim_nonce`) and
I1's own `len(timestamp_header) > 20` + integer-epoch-arithmetic fix (inside
`verify_collector_request`, a different function) are present in the merged file,
confirmed by `grep`, then confirmed behaviorally by `mypy app` (clean) and the full
backend suite (no regression). No other file produced even an auto-merge; all
others applied as plain fast-forward patches.

## 8. Production Files Changed (this consolidation's own hardening work, beyond the 5 incorporated commits)

```
README.md
backend/app/api/v1/collectors.py
backend/app/api/v1/integrations.py
backend/app/application/collector_service.py
backend/app/application/drivers/network_policy.py   (new)
backend/app/application/drivers/rest.py
backend/app/core/config.py
```

Plus 6 new test files and 2 updated existing test files (§17). No Phase 8 I1–I4
corrected code path (`collector_auth.py`'s timestamp check, `discovery_service.py`'s
`SELECT ... FOR UPDATE`) was touched by this consolidation's own hardening work.

## 9. Phase 8 Closure Preservation

Re-confirmed after all consolidation work: `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`,
`PHASE8_BLOCKING_CORRECTION_REPORT.md`, `PHASE8_FINAL_CLOSURE_VALIDATION.md` are
byte-identical to the validated `claude/phase8-independent-red-team` branch (`git diff
6af4e4e...HEAD -- PHASE8_INDEPENDENT_RED_TEAM_REPORT.md
PHASE8_BLOCKING_CORRECTION_REPORT.md PHASE8_FINAL_CLOSURE_VALIDATION.md` produces no
output). The `PHASE 8 CLOSED — VERIFIED` gate stands, unedited.

## 10. Codex Finding Reconciliation

Read both `PHASE8_CODEX_INDEPENDENT_RED_TEAM.md` and
`PHASE8_CODEX_FINDING_VERIFICATION.md` (and their `_SCOPE.md` companions) in full.
Both are **STATIC ANALYSIS ONLY** against the raw `fc5fd57` baseline, produced in an
environment with no PostgreSQL/Docker/npm available — neither ever executed a test,
a migration, or a real HTTP request. Every finding below was independently
reassessed against the CONSOLIDATED code, with actual execution where the original
review could not:

| Finding | Original severity | Still present at consolidation HEAD? | Already fixed? | Architecture decision? | MVP blocker? | Production blocker? | Action |
|---|---|---|---|---|---|---|---|
| **H1** REST outbound target / SSRF | HIGH | Was present (independently reproduced end-to-end, §11) | **Fixed this task** | No | No (was) | Was HIGH | Implemented `network_policy.py` + wired into `RESTDriver`/`run_polling_cycle` (§12) |
| **H2** site-scoped authorization | HIGH (Codex's original), downgraded to NOT VALID by Codex's own follow-up | Unchanged (global RBAC is unchanged) | N/A — not a defect | **Yes** — `ARCHITECTURE_REVIEW.md`'s own H7 Option B, `PHASE8_GAP_ANALYSIS.md`'s own explicit deferral | No | **No** | None — reaffirmed NOT VALID as a security finding under the current architecture (§14). Machine-side collector/site/assignment authorization remains separately enforced and unchanged. |
| **H3** VendorProfile/DeviceProfile/MetricMapping | HIGH (Codex's original), downgraded to MEDIUM by Codex's own follow-up | Unchanged (no persistent tables exist, same as at `fc5fd57`) | N/A — was never Phase 8's own deliverable | **Yes** | No (MVP needs only a thin per-Integration mapping, not the full catalog — see `PHASE8_ARCHITECTURE_CLARIFICATION.md`) | No | Wrote `PHASE8_ARCHITECTURE_CLARIFICATION.md` (§13); no code change |
| **M1** total collector request size | MEDIUM | Was present (confirmed: `get_current_collector` buffered the whole body via `request.body()` with no cap) | **Fixed this task** | No | No | Was a real resource-exhaustion gap | Bounded-stream reader + manual body parsing, replacing FastAPI's own body-param resolution (§14) |
| **M2** discovery/reconciliation concurrency | MEDIUM | **Already fixed** — by the I1–I4 correction, independently re-verified in `PHASE8_FINAL_CLOSURE_VALIDATION.md` (I3's `SELECT ... FOR UPDATE`, up to 25-way races; I4's SAVEPOINT recovering from a genuine `IntegrityError` on `ingest_discovery`'s own unique constraint) | Yes | No | No | No | None — reaffirmed fixed, no regression found in this consolidation's own regression run |
| **M3** raw exception leakage | MEDIUM | Was present (`error=str(exc)` in `ingest_batch`) | **Fixed this task** | No | No | Was real | Stable `error_code` + generic public message + server-side structured logging (§15) |
| **M4** nonce retention | MEDIUM (Codex) / LOW (independent audit's own F1) | Unchanged, no pruning job exists | No | No | No | **No** — bounded by the nonce table's own unique index, not a correctness defect | Deferred, per master prompt §16 — recorded as a known Phase 8B operational-hardening item, not implemented |
| **M5** reconciliation outbox / site compatibility | MEDIUM | Reconciliation decisions still write `audit_log` only, no `outbox_event` — unchanged since `fc5fd57`; site-compatibility half is **NOT VALID** under global RBAC, same reasoning as H2 | Not fixed (not attempted) | Partially (the site-compatibility half) | No | No | Not required by this task's own enumerated hardening list (§7–§16 of the master prompt); recorded as a deferred item, not implemented, to avoid unrequested scope creep into `discovery_service.py`'s already-hardened I3 functions |
| **FV1** persistent commit failure / ACK behavior | MEDIUM (this consolidation's own predecessor finding, `PHASE8_FINAL_CLOSURE_VALIDATION.md`) | Unchanged — re-confirmed present via the full backend suite (`test_phase8_final_closure_validation.py`'s own fault-injection tests still pass, same behavior) | No | No | No | No — the named store-and-forward invariant (no duplicate authoritative state, no permanent poisoning) was already proven to hold even under this condition | Reaffirmed non-blocking, not touched |
| **FV2** unbounded capability/config payloads | MEDIUM (this consolidation's own predecessor finding) | Was present | **Fixed this task** | No | No | Was real | `CapabilityDeclareIn`/`Integration.config` bounds (§16) |
| **I5** polling query scaling | MEDIUM (independent audit's own finding, reaffirmed unchanged) | Unchanged (~6 queries/integration, linear) | No | No | No | No | **Explicitly not optimized**, per master prompt §15 — reaffirmed via the full backend suite's own performance tests, no regression |

## 11. REST/SSRF Reproduction

Independently reproduced against the **consolidated** code, before any fix was
applied, using only safe controlled infrastructure (a genuine local
`asyncio.start_server` on `127.0.0.1`; never a real cloud metadata service or
external host):

1. An authenticated DCIM Manager creates a `rest` integration with an arbitrary
   `target_host`/`target_port`/`config` (`IntegrationIn` had, and still has, no
   target-policy validation at creation time — this is by design, see §12: policy
   enforcement belongs at poll time, in the driver, not at CRUD time).
2. The same actor assigns it to a collector with the `rest` capability and calls
   `poll-now`.
3. Before this task's fix: `RESTDriver.connect`/`poll` would have constructed
   `f"{scheme}://{target_host}{port_part}{path}"` and issued the request with no
   destination check at all — confirmed by reading the pre-fix driver source in
   full (§8 of the original Codex report's own attack-path description matches this
   codebase exactly).
4. After this task's fix (`tests/api/test_rest_ssrf_hardening.py`, run against the
   actual consolidated HEAD): a metadata-style target (`169.254.169.254`) and an
   RFC1918 target outside any configured allowlist are both cleanly rejected
   end-to-end via `poll-now` (`succeeded: false`, a policy-shaped error, zero
   `discovered_device` rows for that address) — the central process never attempts
   either connection. A target inside an operator-configured allowlist genuinely
   succeeds against a real local HTTP server, proving the fix denies unsafe targets
   without denying everything.

**The underlying outbound-request problem was confirmed real** (an authenticated
administrator could cause the central process to make an unintended HTTP request)
and is now closed, per §12.

## 12. REST Target-Policy Design

New module, `backend/app/application/drivers/network_policy.py` — deliberately
stdlib-only (no `sqlalchemy`, `app.db`, `app.application.rbac`, or `app.core.config`
import), so it stays exactly as portable as `app/application/drivers/` itself (§9 of
the master prompt). `collector_service.py`'s `run_polling_cycle` is the ONE place
central `Settings` gets translated into a `NetworkPolicy` and passed into
`RESTDriver` as plain data — a future Edge Collector builds the equivalent object
from its own local configuration and passes it to the same, unmodified driver class.

- **Scheme**: `allowed_schemes` (default `{http, https}`), rejects everything else.
- **Host canonicalization**: `canonicalize_host` strips URL-literal IPv6 brackets,
  rejects control characters/embedded whitespace, lowercases — never a string-prefix/
  substring security decision. `ipaddress.ip_address()` parses literal IPv4/IPv6;
  IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is unwrapped via `IPv6Address.ipv4_mapped` and
  re-judged as the plain IPv4 address it represents, closing the exact bypass form
  the master prompt names explicitly.
- **DNS / rebinding**: `resolve_host` resolves a hostname via `socket.getaddrinfo`
  (off the event loop thread), returning EVERY unique resolved address, not just one
  — `validate_target` validates ALL of them (a multi-answer DNS response cannot sneak
  one bad address past validation while another gets used). The result is pinned to
  ONE already-validated address (`ValidatedTarget.pinned_ip`); `build_connect_url`
  constructs the actual request URL against that literal IP, never the hostname
  again, so whatever this process connects to is exactly what was already validated —
  no re-resolution window. `httpx_request_kwargs` preserves the original hostname via
  the `Host` header (virtual-hosting) and via httpx/httpcore's `sni_hostname` request
  extension (correct TLS SNI and certificate-hostname verification against the
  original hostname even though the connection targets the pinned IP — confirmed
  against the installed httpcore 1.0.9 source, not assumed).
- **Explicit permitted network scope**: `NetworkPolicy.allowed_networks` — a tuple of
  `ipaddress.IPv4Network|IPv6Network`, empty by default (deny-by-default), populated
  from a new, NOT-hardcoded `Settings.rest_integration_allowed_networks: list[str]`
  (e.g. an operator sets `["10.10.0.0/16", "172.20.10.0/24"]` for their own
  deployment — this consolidation ships no example ranges as defaults).
- **Always-denied destinations**: `_always_denied_reason` — unspecified
  (`0.0.0.0`/`::`), multicast, IANA-reserved, link-local (`169.254.0.0/16`/`fe80::/10`
  — covers the cloud-metadata address structurally, not as a special case), the IPv4
  limited-broadcast address, and loopback — all denied EVEN under a deliberately
  broad allowlist (tested: `test_broad_private_allowlist_still_blocks_metadata_target`,
  `test_always_denied_addresses_rejected_even_with_broad_allowlist`). Loopback is
  handled deliberately: denied unless `NetworkPolicy.allow_loopback=True`, an explicit
  test/development-only field that must never be true in a production policy — a real
  bug was found and fixed here during testing (Python's `ipaddress` module classifies
  `::1` as BOTH `is_loopback` and `is_reserved`; the original check order let an
  explicitly-allowed loopback target still get denied as "reserved" immediately
  afterward — fixed by making the loopback check exclusive/short-circuiting).
- **Ports**: `allowed_ports` (default `{80, 443}`), fully configurable via
  `Settings.rest_integration_allowed_ports` for device-specific management ports.
- **HTTP methods**: `allowed_methods` (default `{GET, HEAD}`) — no state-changing
  method is silently permitted.
- **Redirects**: `max_redirects` defaults to `0` (`follow_redirects=False`) — tested
  with a real local server issuing a 302 to an address nothing listens on
  (`127.0.0.1:1`): the driver reports the 302 itself and never attempts the redirect
  target.
- **Headers**: the validated `Host` header is applied AFTER user-supplied
  `config["headers"]` are merged in, specifically so a misconfigured or hostile
  `Host` override in `config` can never win — tested directly against a real server
  capturing the raw request line.
- **Response size**: `RESTDriver.poll` now streams via `client.stream(...)` +
  `response.aiter_bytes()`, counting bytes and truncating the instant the running
  total exceeds `max_response_bytes` — never `response.content` (which fully
  materializes the response before any truncation could apply, exactly Codex's own
  criticism of the pre-consolidation code). Tested against a real server sending 10x
  the cap.
- **Timeouts**: bounded `connect`/`read`/`write`/`pool` timeouts, each independently
  configurable via `NetworkPolicy`, tested against a real server that sleeps 5s
  against a 0.5s read timeout.
- **Proxy/environment**: `trust_env=False` by default — tested with a real
  `HTTP_PROXY` environment variable pointing at a nothing-listening address,
  confirming the request is NOT routed through it.

## 13. H3 Architecture Disposition

New document, `PHASE8_ARCHITECTURE_CLARIFICATION.md` (does not edit any historical
report). Disposition: **B is correct** — Phase 8's own required deliverable is the
Protocol→Driver dispatch framework (`ProtocolDriver` ABC, `DRIVER_REGISTRY`, zero
vendor-conditional branching, independently re-confirmed still true at this
consolidation's HEAD by `grep -rn "vendor =="` across `app/`). The persistent
`VendorProfile`/`DeviceProfile`/`MetricMapping` layers `ARCHITECTURE_REVIEW.md` §20
documents are properly part of the SAME telemetry pipeline (`TelemetryReading`/
`TelemetryMetric`) that `PHASE8_GAP_ANALYSIS.md` §5 already explicitly defers to
Phase 9+ — they were never Phase 8's own deliverable, and no code change was made.
The document also records (without implementing) that the Mini-DCIM MVP will need at
minimum a thin per-`Integration` metric mapping to connect one real device, which
does not require the full vendor/device-profile catalog.

## 14. Request-Size Hardening (M1)

`get_current_collector` (shared by every collector-authenticated endpoint) now reads
the body itself via a genuine bounded byte-counted stream read
(`_read_body_bounded`), aborting with a clean 413 the instant the running total
exceeds `MAX_COLLECTOR_REQUEST_BYTES` (8 MiB — sized for a full 500-record batch at
`MAX_RAW_ATTRIBUTES_BYTES`=8192/record plus JSON overhead, ~4.25 MiB, with real
headroom) — a pre-check against `Content-Length` is attempted first but never
trusted alone; the byte-counted read is authoritative regardless of what
`Content-Length` claims or whether it's present at all (chunked transfer, no
`Content-Length` header).

**A real architectural pitfall was found and fixed during implementation**: FastAPI
resolves a route's own Pydantic `body: SomeModel` parameter independently of
`Depends()`, and Starlette caches the full body internally the first time ANYTHING
reads it — meaning declaring both a bounded-read dependency AND a normal
`body: IngestBatchIn` parameter on the same route would let FastAPI's own body
resolution fully buffer the request BEFORE the bounded reader ever ran, silently
defeating the size cap entirely (empirically confirmed with a minimal repro against
this exact FastAPI/Starlette version before writing the real fix). Fixed by removing
the FastAPI-native body parameter from both `heartbeat` and `ingest_batch`,
stashing the already-bounded raw bytes on `request.state.raw_body` in the shared
dependency, and validating them into the same Pydantic models manually
(`_parse_body`), producing byte-identical 422 responses to what FastAPI's own
resolution produced before.

Hostile tests (`tests/api/test_m1_request_size_hardening.py`): an oversized raw
(non-JSON) body is rejected with 413 before any JSON parsing is attempted; a body of
exactly the cap size is NOT rejected by size alone; a genuinely chunked upload with
no `Content-Length` header at all is still bounded by actual bytes read.

## 15. Public Error Sanitization (M3)

`IngestRecordResult` gained a new `error_code` field: `NOT_ASSIGNED` (the existing
403 case, whose `ApiError.detail` was already hand-written and safe — returned
as-is), `IDEMPOTENCY_CONFLICT`, `PROCESSING` (both pre-existing, now labeled), and
`INTERNAL_PROCESSING_ERROR` for anything else the per-record `try`/`except` catches
(a genuine `IntegrityError`, or any other unexpected exception) — that branch now
logs the full exception server-side via structlog (`logger.error(...)`, which passes
through structlog's own `_redact_sensitive` processor) and returns only a fixed,
generic message to the collector, never `str(exc)`.

The load-bearing test (`tests/api/test_m3_error_sanitization.py::
test_genuine_database_constraint_violation_does_not_leak_sql_to_collector`) forces a
REAL `asyncpg.exceptions.UniqueViolationError` (the same genuine two-record race on
`discovered_device`'s own unique constraint the I4 hostile self-review already
proved happens reliably) through this exact code path and asserts the collector's
own response contains none of: the exception class name, `asyncpg`, `sqlalchemy`,
raw SQL fragments, `constraint`, `duplicate key`, a traceback, or a filesystem path —
only the stable code and the fixed generic message.

## 16. Config/Capability Bounds (FV2)

- `CapabilityDeclareIn.protocol_codes`: bounded to 64 entries (`Field(max_length=64)`),
  each at most 64 characters — the built-in codes today are 4 characters
  (`icmp`/`rest`/`snmp`), so this is far more generous than any real deployment needs.
- `IntegrationIn.config` / `IntegrationPatchIn.config`: bounded to 16 KiB serialized
  (double `IngestRecordIn.raw_attributes`'s 8192-byte bound, appropriate since
  `config` is a one-time admin-authored object, not a per-poll payload) and 6 levels
  of JSON nesting — comfortably fits any real REST/SNMP integration configuration
  (a `headers` dict is one level deep) without being unbounded.

`tests/api/test_fv2_config_capability_bounds.py` covers: over-bound and at-bound
capability lists, an overlong single code, oversized config on both create and
patch, a deeply nested config, and a normal, realistic config still being accepted.
The prior closure-validation test that had documented the ABSENCE of this bound
(`test_security_sweep_capability_declaration_has_no_payload_size_bound`) was updated,
per its own docstring's explicit instruction to do so once a bound existed —
renamed and its assertion flipped to confirm the bound is now enforced.

## 17. Known Deferred Findings (recorded, not implemented, per explicit task instruction)

- **I5** (polling query scaling, ~6 queries/integration, linear) — not optimized;
  re-confirmed unchanged and non-regressed by the full backend suite's own
  performance tests.
- **F1/M4** (`CollectorRequestNonce`/`CollectorHeartbeat` retention) — no pruning job
  added; still bounded by the nonce table's own unique index, not a correctness
  defect.
- **F2** (collector-secret / integration-credential rotation) — deferred, unchanged.
- **M5** (reconciliation decisions have no `outbox_event`, only `audit_log`) — not
  implemented; outside this task's own enumerated required-hardening list, and
  touching `discovery_service.py`'s already-hardened I3 functions for an
  unrequested addition risked exactly the kind of scope creep the task explicitly
  warned against.
- **FV1** (a persistent, non-transient outer-commit failure breaks the per-record
  ACK contract for the rest of that one batch) — reaffirmed non-blocking; the named
  store-and-forward invariant (no duplicate authoritative state, no permanent
  poisoning) was already proven to hold even under this condition.
- **H3's forward-looking VendorProfile/DeviceProfile/MetricMapping catalog** — not
  built; recorded in `PHASE8_ARCHITECTURE_CLARIFICATION.md` as a Phase 8B/MVP-time
  decision, not a pre-MVP-gate requirement.

## 18. Backend Result

Full `pytest -q` on the consolidated branch, exact summary line:

```
523 passed, 14 warnings in 218.69s (0:03:38)
```

0 failed, 0 skipped, 0 xfailed, 0 xpassed. The 14 warnings are all the same
pre-existing `StarletteDeprecationWarning` already disclosed in every prior Phase 8
report. `ruff check app tests` and `mypy app` both report clean (`All checks
passed!` / `Success: no issues found in 89 source files`) on the full consolidated
tree. Category re-runs, for direct traceability to this task's own required list:
Phase 8 (all Phase 8 + this consolidation's own new files) 209 passed; Phase 3
(power/capacity/dashboard) 49 passed; Phase 2 (physical/spatial/floor-plan) 102
passed; Auth 8 passed.

## 19. Frontend Result

```
npm run typecheck   -> clean
npm run lint        -> clean
npm run build       -> succeeded ("✓ 126 modules transformed" / "✓ built in 1.90s")
```

No frontend file was changed by this consolidation (every hardening change is
backend-only); this confirms no regression, not new work.

## 20. Migration Result

`alembic heads` reports a single head, `0008_phase8`, unchanged. Independently
re-validated against a genuinely fresh scratch database (bootstrapped with the same
`uuid-ossp`/`pgcrypto`/`btree_gist` extensions the documented Docker/README setup
step provides, per the prior closure validation's own finding that this is a
pre-existing Phase 1 bootstrap dependency, not an Alembic gap): fresh→head succeeded
cleanly through all 8 migrations, downgrade to `0007_correction` succeeded, and
re-upgrade to head succeeded. No new migration was added or needed by any of this
consolidation's own changes (all are application-code-level: new module, new
Pydantic validators, new Settings fields with defaults, restructured request
handling — no schema change).

## 21. CI Result

The consolidated `.github/workflows/ci.yml` (after the 5 incorporated commits) was
not run on GitHub Actions directly from this task, but every one of its steps was
independently replicated locally, in the same order, with the same environment
variables, against this exact branch: role/database provisioning, extension
enablement, `ruff check app tests`, `mypy app`, `alembic upgrade head`, the
privileged-retention-role bootstrap script, the `CAP_NET_RAW` grant, and `pytest -q`
— all passed. Frontend: `npm run typecheck`, `npx eslint . --ext ts,tsx`, `npm run
build` — all passed. The `CAP_NET_RAW` step is retained exactly as the incorporated
commit wrote it (a single `setcap` grant on the Python binary before the test step),
not a broader "run CI as root" workaround — this sandbox happens to already run as
root, which is why the ICMP tests pass here without needing the grant locally, but
the grant remains required and documented (README.md §"Tests", `.github/workflows/
ci.yml`) for the non-root runner CI actually uses.

## 22. Security Regression Result

Full security sweep re-run against every file this consolidation touched or added:
zero bare `except:`, zero `TODO`/`FIXME`/`HACK`, zero `verify=False`/insecure-TLS
constructs, zero secret-logging. The one new finding this task's own work surfaced
(the Python `ipaddress` `::1` loopback/reserved ordering bug in
`network_policy.py`) was found BY this task's own hostile testing and fixed before
merge, documented in §12 rather than silently corrected — not a regression left in
place.

## 23. Final Canonical Branch SHA

Recorded after the commit in §21 of the final response below (this document is
written and committed together with the code in one commit, so its own SHA is the
final canonical SHA).

## 24. Recommendation for Next Milestone

`Phase 8B Edge Collector Runtime + DCIM MVP v0.1` remains the correctly-sequenced
next milestone. This gate's own findings support starting it: the central contract
(collector identity/auth, per-record idempotent ingestion, non-authoritative
discovery, human-gated reconciliation, a now-SSRF-hardened driver framework with
zero central-DB/RBAC coupling) needs no further redesign for Phase 8B to plug into.
Nothing in this consolidation was implemented, and nothing should be inferred as
implemented, toward that next milestone — it begins from a clean, hardened,
regression-tested foundation, not from work already done.

## Final Gate

**PRE-MVP CONSOLIDATION PASSED**
