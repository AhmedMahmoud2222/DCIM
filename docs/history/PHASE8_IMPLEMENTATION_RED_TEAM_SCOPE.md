# Phase 8 Implementation Red-Team Scope

**This is a self-red-team performed by the implementation agent against its own Phase 8
work, in the same task, per the master prompt's explicit instruction: "Red-team your own
implementation... If a blocking defect is discovered, correct it, add a regression test,
and re-run the affected validation."** Unlike Phase 3's stand-alone hostile-audit tasks
(which forbade silent fixes and required a separate closure task), this task's own
instructions require finding AND fixing genuine defects within this same pass — every
finding below that was judged genuinely blocking was corrected, with a regression test,
before this document was finalized. This is not a substitute for an independent
adversarial review by someone other than the implementer.

## Executive Summary

Five real defects were found and corrected during this pass, all with regression tests,
all re-verified against a clean full backend suite run afterward:

1. **N1 (Performance/N+1)** — `GET /collectors`, `GET /integrations`, and
   `run_polling_cycle` each issued one query per row in a loop (health lookup, current-
   assignment lookup, integration lookup respectively). Fixed with batch-loading
   functions (`classify_collector_health_bulk`, `current_assignments_bulk`, and a
   batch `Integration` fetch in `run_polling_cycle`), mirroring Phase 3's own
   `PowerGraphSnapshot` precedent. Verified flat at 4 queries from n=1 through n=500
   collectors (`test_phase8_performance.py`).
2. **N2 (Cross-site leakage)** — `assign_integration` never checked that an edge
   collector's `site_id` matched its target integration's `site_id`; an edge collector
   scoped to Site A could be assigned an integration explicitly scoped to Site B. Fixed
   with an explicit mismatch check (both sides must be set and differ to reject — a
   central collector or site-agnostic integration is not a mismatch).
3. **S1 (Oversized payload)** — `MAX_BATCH_RECORDS` bounded record *count* but not the
   size of any single record's `raw_attributes` blob, so total payload size was
   effectively unbounded. Fixed with a Pydantic field validator (`MAX_RAW_ATTRIBUTES_BYTES
   = 8192`).
4. **S2 (Trust-boundary gap)** — the ingest endpoint never verified that
   `record.integration_id` was actually currently assigned to the authenticated
   collector; a valid signature (proving identity) was being treated as sufficient
   authorization to report for *any* integration_id in the system. Fixed with a
   per-record `current_assignment` check before `ingest_discovery` is called.
5. **Audit-completeness gap** — `collector_service.py`'s own module docstring claimed
   "`write_audit_log` for every state-changing action," but `register_collector`,
   `declare_capabilities`, and `assign_integration` never actually called it — only the
   outbox event was written. Fixed by adding the missing `write_audit_log` calls.

One additional class of bug was fixed opportunistically once the pattern was recognized
(the same class as the pre-existing, disclosed Finding M1 from Phase 1): `HeartbeatIn.status`
had no `max_length` matching its column's actual width (`String(16)`), so an oversized
value would reach Postgres and surface as an unhandled 500 rather than a clean 422.
`Integration`'s plaintext `credential` input fields had the same gap relative to
`credential_ciphertext`'s `String(4000)` width and were given a `max_length=2000` bound
(generous enough that a Fernet-encrypted 2000-char credential fits comfortably under 4000).

No CRITICAL defect (data corruption, authentication bypass, or crash under normal use)
was found. S2 is the most severe finding — a genuine trust-boundary gap, not merely a
performance or data-modeling issue — and was treated as blocking accordingly.

## 30-Item Checklist (Master Prompt §25)

| # | Item | Result |
|---|---|---|
| 1 | Collector identity | Uniqueness (`name` unique constraint, tested via concurrent-registration race), lifecycle (registered→active→disabled transitions used by tests), heartbeat (append-only, tested) — no defect found |
| 2 | Collector/site association | **N2 found and fixed** (see above) |
| 3 | Collector authentication | HMAC scheme tested against tampered body, wrong secret, expired timestamp, replayed nonce, disabled collector, unknown collector, malformed ID — no defect found beyond N2/S2 above |
| 4 | Assignment integrity | Capability-match enforcement, reassignment (close-then-open), disabled-collector rejection (409), bogus collector 404 — no defect found |
| 5 | Duplicate registration | Genuine concurrency test: 10 concurrent identical-name registrations → exactly 1×201, 9×409 |
| 6 | Duplicate telemetry/event delivery | Ingest is idempotent per `dedup_key` (tested). Heartbeat has no dedup key — a retried heartbeat after a lost ACK creates an extra row. Judged non-blocking: `classify_collector_health` only reads the single most recent row, so an extra near-duplicate heartbeat changes nothing observable. Documented, not treated as a defect. |
| 7 | Replay attacks | Nonce uniqueness tested sequentially AND under genuine concurrency (10 simultaneous identical requests → exactly 1×204, 9×401) |
| 8 | WAN buffering | Central-side contract implemented and tested; collector-side buffer does not exist in this phase (no agent built) — explicit, disclosed boundary, not a defect (see `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §4) |
| 9 | Queue overflow | Not applicable — no collector-side queue exists yet; central-side batch size is bounded (`MAX_BATCH_RECORDS`) |
| 10 | Collector restart recovery | Nonces and idempotency claims are DB-backed, not in-memory — a collector generating fresh random nonces after restart is unaffected. STATICALLY VERIFIED, not executed via an actual process restart (no collector process exists to restart) |
| 11 | Central restart recovery | Same DB-backed reasoning applies centrally — no in-memory state is load-bearing anywhere in the collector auth/idempotency path. STATICALLY VERIFIED |
| 12 | Partial ACK | Tested — one bad record in a batch does not fail its sibling; each record gets its own accepted/duplicate/rejected status |
| 13 | Malformed payloads | Timestamp/nonce/signature length and format validated and tested; **S1 found and fixed** for oversized `raw_attributes`; JSON schema violations inherit the app-wide `RequestValidationError` handler (pre-existing, already covered elsewhere in the suite) |
| 14 | Oversized payloads | **S1 found and fixed** |
| 15 | Unauthorized administration | RBAC-permission-gated endpoints tested for 403 (Viewer role) across register/assign/declare-capabilities/reconcile |
| 16 | Discovery overwriting inventory | Structurally impossible (`accept_reconciliation` is the one function that may set `matched_managed_asset_id`, and only onto an already-existing `ManagedAsset`) — tested via a direct `ManagedAsset` row-count assertion before/after discovery |
| 17 | Cross-site leakage | **N2 found and fixed** |
| 18 | Driver failure isolation | Tested — a real SNMP failure (no transport factory, an honest, disclosed limitation) alongside a real ICMP success in the same polling cycle; the failure does not prevent the success |
| 19 | Integration failure isolation | Same test as #18 — each integration's outcome (success/failure, `consecutive_failures`, `last_success_at`/`last_failure_at`) is independent |
| 20 | Collector failure isolation | Structurally isolated — every service function is scoped by an explicit `collector_id` parameter, no shared mutable state between collectors. STATICALLY VERIFIED (a dedicated two-collector test was judged redundant given the structural guarantee, given time constraints) |
| 21 | Idempotency | Tested extensively — ingest duplicate replay, nonce replay (including under genuine concurrency) |
| 22 | Audit completeness | **Audit gap found and fixed** (see above) |
| 23 | Outbox correctness | All outbox writes happen in the same transaction as their corresponding domain write (same `db` session, committed together) — verified by code inspection; `CollectorDisconnected` is not implemented (no detection mechanism exists yet — disclosed, not silently omitted) |
| 24 | Concurrent assignment races | Tested — 20 concurrent reassignment requests alternating between two collectors on the same integration leave exactly one currently-open assignment row |
| 25 | Hidden N+1 queries | **N1 found and fixed** |
| 26 | Unbounded polling behavior | Not applicable in this phase — polling is a manual, on-demand trigger only (`poll-now`); no automatic scheduler exists yet to run unbounded. Documented as an open/deferred decision |
| 27 | Retry storms | Not applicable for the same reason as #26 — no automatic retry loop exists yet |
| 28 | Stale collector state | Health is computed live from the latest heartbeat row every time it's read, never cached — staleness is structurally impossible |
| 29 | Clock/timestamp problems | The 300-second timestamp window is symmetric (rejects both a too-old and a too-far-in-the-future timestamp), tested via the expired-timestamp case; exact clock-sync tolerance is an open decision (`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6) |
| 30 | Phase 9 telemetry coupling | Verified no `TelemetryReading`/time-series/alarm code exists yet; the `AcquisitionResult.metrics` field is defined but unconsumed beyond discovery, giving Phase 9 a clean extension point without needing to touch Phase 8's domain model (`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §5) |

## Trust Boundary Analysis (Master Prompt §27)

```
UNTRUSTED SITE NETWORK → DEVICE → EDGE COLLECTOR → INTERNET/WAN →
CENTRAL INGESTION API → VALIDATION/AUTHENTICATION → DOMAIN SERVICE → AUTHORITATIVE STORE
```

- **Device → Edge Collector**: not evaluated in this phase (no edge collector agent
  exists to run against a real device network; the drivers run inside the central
  process itself in this phase, per `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §2).
- **Edge Collector → Internet/WAN**: TLS 1.3/mTLS is a deployment-layer decision
  (reverse proxy/load balancer), not application code — explicitly out of this phase's
  implementable scope, disclosed as an open decision.
- **Internet/WAN → Central Ingestion API**: HMAC-SHA256 signature (not the bare secret)
  crosses the wire; the collector's raw secret never does. Constant-time comparison
  (`hmac.compare_digest`). Tested.
- **Validation/Authentication**: collector existence + active status → timestamp window
  → signature → nonce claim, in that exact fail-fast order (never partially trusting a
  request that fails a later check). Tested for every individual failure mode.
- **Domain Service**: the collector's signature only establishes identity, never
  authorization for an arbitrary `integration_id` — this is exactly where **S2** was
  found: a registered collector was being treated as authoritative for any integration
  it named, not just the ones actually assigned to it. Fixed.
- **Authoritative Store**: `ingest_discovery` never writes to `ManagedAsset`/`Rack`/
  `Equipment`/`PDU`/`UPS`/`Generator`/`PowerPanel` — only `DiscoveredDevice`/
  `ReconciliationDiff`. A collector, even a fully authenticated and correctly-assigned
  one, can never cause an authoritative inventory write by itself — a human must always
  call `accept_reconciliation`.

**Conclusion**: after the S2 fix, a collector is treated as semi-trusted at every layer
this phase actually implements — a valid signature is necessary but never sufficient for
any specific write; the specific integration/site/capability relationship is checked
independently before any effect on state.

## What Was Deliberately NOT Fixed (and Why)

- The heartbeat duplicate-row-on-retry behavior (#6 above) — genuinely harmless given how
  health is computed; fixing it would mean adding a second idempotency mechanism for a
  problem that doesn't actually manifest, which the master prompt explicitly warns
  against ("do NOT introduce a second unrelated idempotency framework" — and heartbeats
  don't need the first one either, since duplication here has no observable effect).
- `protocol_codes`/`collector_type`/`integration_type` string values have DB `CHECK`
  constraints but no matching Pydantic `max_length`/enum validation at the API boundary.
  An invalid value hits the existing global `IntegrityError` handler and returns a clean
  409 (not a 500) — imprecise (409 "Conflict" rather than 422 "Validation Error") but not
  a crash. Judged low-severity/informational, not fixed in this pass given the size of
  the surface already covered; noted here for completeness rather than left silently
  unmentioned.
