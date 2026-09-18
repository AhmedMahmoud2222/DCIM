# Phase 8 Implementation Report — Integrations + Collectors

Starting HEAD: `99fce6555d1d75152b09a61780c6a8e837395ee4` (Phase 3 independently closed,
"PHASE 3 CLOSED — VERIFIED"). See `PHASE8_GAP_ANALYSIS.md` for the mandatory scope
reconciliation performed before any implementation work began.

## 1. Objective

Implement the architecture's Integrations + Collectors foundation (§18/§19/§20 of
`ARCHITECTURE_REVIEW.md`): collector identity, capability, assignment, and health;
integration configuration with encrypted credentials; a protocol/driver abstraction
(ICMP/SNMP/REST); a discovery/reconciliation boundary that structurally cannot let
discovery write authoritative inventory; a machine-to-machine collector trust boundary
distinct from user auth; WAN-buffered idempotent batch ingestion; and the minimum
operational UI — without redesigning Phase 3 (Power) or building Phase 9 (Telemetry).

## 2. Architecture Sections Implemented

§12 (Single Source of Truth — the temporal assignment pattern, never a mutable
`collector_id` column), §18/§19 (Collector/Edge Collector), §20 (Integration layering),
§21 (Idempotency), §22 (Domain Events/Outbox), §23 (Observability), §25 (Event
identity/correlation), §26 (Discovered vs. authoritative state), §27 (ITSM readiness —
open decision, not implemented), §31 (Security), §32 (RBAC/site scoping — global
preserved, data model kept extensible), §37 (Observability/operations), §42 (Data
ownership), §43 (Domain boundaries), §44 (Failure/resilience), §47 (Phase sequencing —
reconciliation), §48 (Architecture consistency), §49/§50 (Open decisions/Risks — see
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6).

## 3. Files Changed

**New domain/application/API files:**
`backend/app/domain/integration/models.py` (`Collector`, `CollectorCapability`,
`CollectorHeartbeat`, `CollectorRequestNonce`, `Integration`, `CollectorAssignment`,
`DiscoveredDevice`, `ReconciliationDiff`), `backend/app/core/secrets.py` (Fernet
encrypt/decrypt), `backend/app/application/collector_auth.py` (HMAC machine-trust),
`backend/app/application/collector_service.py` (registration, capabilities,
assignment, heartbeat/health, polling orchestration), `backend/app/application/
discovery_service.py` (discovery/reconciliation), `backend/app/application/drivers/`
(`base.py`, `icmp.py`, `rest.py`, `snmp.py`, `__init__.py`), `backend/app/api/v1/
collectors.py`, `backend/app/api/v1/integrations.py`, `backend/app/api/v1/discovery.py`.

**New migration:** `backend/migrations/versions/0008_phase8_integrations_and_collectors.py`.

**New tests:** `backend/tests/api/_phase8_helpers.py`, `backend/tests/api/
test_collectors.py` (24), `backend/tests/api/test_integrations.py` (9), `backend/tests/api/
test_discovery.py` (7), `backend/tests/api/test_phase8_performance.py` (6, one
parametrized ×4), `backend/tests/integration/test_phase8_concurrency.py` (4),
`backend/tests/unit/test_collector_auth.py` (8), `backend/tests/unit/test_drivers.py` (8).

**New frontend files:** `frontend/src/features/integrations/{api.ts,CollectorsPage.tsx,
IntegrationsPage.tsx,DiscoveryPage.tsx}`.

**Additively modified (existing files, no removed functionality):**
`backend/app/api/v1/router.py` (new router registration), `backend/app/application/
rbac.py` (7 new permission codes), `backend/app/core/config.py`
(`credential_encryption_key` setting), `backend/app/core/errors.py`
(`UnauthorizedCollectorError`), `backend/pyproject.toml` (`cryptography`, `httpx` moved
to main deps), `backend/tests/conftest.py` (test encryption key env default),
`backend/.env.example` (documented placeholder), `frontend/src/app/App.tsx` (3 new
routes), `frontend/src/components/layout/AppShell.tsx` (3 new nav items),
`frontend/src/types/index.ts` (Phase 8 type definitions).

**Not touched:** anything under `app/domain/physical/`, `app/domain/spatial/`,
`app/domain/power/`, or migrations `0001`–`0007` — confirmed via `git diff --stat`.

## 4. Integration Model

`Integration`: identity (`id`, unique `name`), `integration_type` (icmp/snmp/rest,
DB `CHECK`-constrained), optional `site_id` (FK, nullable), `target_host`/`target_port`,
`config` (JSONB, protocol-specific), `credential_ciphertext` (Fernet-encrypted, nullable,
never serialized back out — `IntegrationOut` exposes only `has_credential: bool`),
`enabled`, `poll_interval_seconds`, `last_poll_at`/`last_success_at`/`last_failure_at`,
`consecutive_failures`, optimistic-concurrency `version` (If-Match, reusing
`app.application.concurrency`). CRUD at `POST/GET/PATCH /api/v1/integrations`.

## 5. Collector Model

`Collector`: `collector_type` (central|edge, `CHECK`-constrained together with `site_id`
nullability — central must NOT have a site, edge MUST), `status` (registered/active/
disabled), `version_string`, `secret_ciphertext` (Fernet, returned in plaintext exactly
once at registration, matching the "show it once" API-key UX). `CollectorCapability`
(declarative protocol support, unique per `(collector_id, protocol_code)`).
`CollectorAssignment` (temporal — `effective_from`/`effective_to` + a partial unique
index guaranteeing at most one currently-open assignment per integration, the identical
pattern `RackPlacement`/`EquipmentPlacement`/`PowerConnection` already established).
`CollectorHeartbeat` (append-only; health is *computed* from the latest row at read
time via `classify_collector_health`/`classify_collector_health_bulk`, never stored,
never inferred from device or integration health).

## 6. Edge Collector Security Model

A genuine machine-to-machine trust boundary (`app/application/collector_auth.py`),
never composed with the user JWT/RBAC path: `X-Collector-Id`/`X-Collector-Timestamp`/
`X-Collector-Nonce`/`X-Collector-Signature` headers, HMAC-SHA256 over
`{collector_id}.{timestamp}.{nonce}.{raw_body}`, verified in strict fail-fast order
(collector active → timestamp window → signature → nonce claim), the nonce claimed only
after signature success (so a forged request cannot burn a legitimate future nonce).
The collector's own secret is Fernet-encrypted (reversible — required so the server can
recompute the HMAC), a deliberate, disclosed departure from the Argon2 (one-way)
convention used for user passwords, with the reasoning documented in `app/core/secrets.py`.

## 7. WAN Buffering / Store-and-Forward Status

**Central-side contract implemented and tested**: idempotent per-record ingestion
(`dedup_key`, reusing the existing `IdempotencyKey` mechanism), `occurred_at` preserved
verbatim alongside `received_at`, per-record ACK (accepted/duplicate/rejected),
partial-batch-failure isolation, batch-size and per-record-size bounds.
**Collector-side buffering agent does not exist in this phase** — no actual edge
collector binary/process is built or deployed; the boundary is documented precisely in
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §4, with the operational unknowns (buffer duration,
storage capacity, queue-full behavior, retry/backoff) listed as explicit open decisions
in §6 rather than invented.

## 8. Discovery/Reconciliation Boundary

`ingest_discovery` upserts `DiscoveredDevice` on `(integration_id, external_identifier)`
and creates a `pending` `ReconciliationDiff` for a new device — it never touches
`ManagedAsset` or any subtype table. `accept_reconciliation` is the ONE function
permitted to set `DiscoveredDevice.matched_managed_asset_id`, and only onto an
already-existing `ManagedAsset` (verified — attempting to link a nonexistent asset ID
returns 404, never creates one). Proven with a direct `ManagedAsset` row-count
assertion before/after a discovery cycle (`test_discovery_never_creates_managed_asset_rows`).

## 9. Protocol/Driver Implementation

`ProtocolDriver` ABC (`connect`/`poll`/`disconnect`) with a `DRIVER_REGISTRY` dict
dispatch (`{"icmp": ICMPDriver, "rest": RESTDriver, "snmp": SNMPDriver}`) — no
`if vendor == ...`/`if protocol == ...` branching anywhere else in the codebase
(verified by grep). ICMP is a genuine raw `SOCK_RAW`/`IPPROTO_ICMP` socket (not a
shell-out — this sandbox has no `ping` binary), tested against real loopback traffic.
REST is a genuine `httpx`-based driver, tested against this application's own real
`/api/v1/health/live` endpoint via in-process ASGI transport (not mocked). SNMP is an
honestly-disclosed abstraction only (`SNMPTransport` protocol, `SimulatedSNMPTransport`
test double) — no real SNMP wire protocol implementation exists yet; this is stated
plainly in the driver's own docstring and in every relevant report, never implied to be
production-ready.

## 10. Idempotency

Every collector write path reuses the existing `app.application.idempotency` module —
no second framework. Ingest: keyed `f"{collector_id}:{dedup_key}"`,
`endpoint="collector_ingest"`. Nonce claims: a separate, purpose-built atomic
`INSERT ... ON CONFLICT DO NOTHING` (the correct primitive for "has this exact value
been seen before," a different shape than the request/response-caching `IdempotencyKey`
mechanism, reusing the same atomic-insert pattern rather than reusing the wrong
abstraction just to avoid writing new code).

## 11. Outbox/Event Integration

Reuses `app.application.outbox_service.write_outbox_event` throughout — no second event
bus. Implemented event types: `CollectorRegistered`, `CollectorHeartbeatReceived`,
`IntegrationEnabled`, `IntegrationDisabled`, `DeviceDiscovered`, `ReconciliationRequired`.
`CollectorDisconnected` (named as an example in the master prompt) is NOT implemented —
there is no background sweep in this phase that detects a collector going stale/offline
and fires a transition event; this is disclosed as deferred, not silently omitted (see
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6).

## 12. Observability

`GET /collectors`/`GET /collectors/{id}` expose computed `health`
(healthy/stale/offline), `last_heartbeat_at`, `seconds_since_heartbeat` — batch-computed
for the list endpoint (see §14 below). `GET /integrations` exposes `enabled`,
`last_poll_at`/`last_success_at`/`last_failure_at`, `consecutive_failures`,
`assigned_collector_id`. Device connectivity (a driver's own `AcquisitionResult`),
collector health, and integration health are three structurally separate fields,
never collapsed into one status, per the master prompt's explicit requirement.

## 13. RBAC/Security

Seven new permission codes (`integration:read`, `integration:manage`, `collector:read`,
`collector:manage`, `collector:assign`, `discovery:read`, `discovery:reconcile`) added
to `DEFAULT_ROLE_PERMISSIONS` via the same idempotent migration-time seed mechanism
migrations 0004/0006 established. Administrator/DCIM Manager get all seven; Engineer
gets read + `discovery:reconcile` (not manage/assign); Operator/Viewer get read-only.
Global-only RBAC preserved (no site-scoped enforcement added), but every `Collector`/
`Integration` already carries a nullable `site_id` so a future site-scoped decision does
not require a migration.

## 14. Performance

Measured (not assumed) via `tests/api/test_phase8_performance.py`, using the identical
`before_cursor_execute` query-counting harness `tests/api/test_power.py` established for
Phase 3's own N+1 guard:

| Collectors | `GET /collectors` query count (before N1 fix) | `GET /collectors` query count (after fix) |
|---|---|---|
| 1 | 5 | 4 |
| 10 | 14 | 4 |
| 100 | 104 | 4 |
| 500 | ~504 (extrapolated from the linear pattern; not measured pre-fix at this scale) | 4 |

The pre-fix numbers for n=1/10/100 were the actual measured counts from the original
per-row `classify_collector_health` loop before it was batch-loaded (see
`PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md` finding N1); n=500 pre-fix was not separately
measured once the linear pattern was established and fixed, since the fix was applied
before the n=500 case was added to the parametrized test. Post-fix, all four scale
points (1/10/100/500) measure identically at 4 queries — flat, not linear.
`GET /integrations` and `run_polling_cycle` received the equivalent fix (see the
implementation of `current_assignments_bulk` and the batch `Integration` fetch in
`run_polling_cycle`), verified via the equivalent query-count-growth assertion for
`GET /integrations` (`test_list_integrations_query_count_does_not_grow_linearly`) —
`run_polling_cycle`'s own fix was code-reviewed (batch fetch replacing a per-assignment
`db.get`) but not separately measured with a dedicated query-count test, since its input
size (integrations assigned to one collector) is expected to remain small in practice
and the fix mirrors the same, already-measured pattern.

## 15. Red-Team Findings

See `PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md` for the full 30-item checklist. Five
findings (N1 performance/N+1, N2 cross-site leakage, S1 oversized payload, S2
trust-boundary/authorization gap, and an audit-completeness gap), all corrected in this
same task with a regression test each, all re-verified against a clean full-suite run
afterward. No unresolved Critical/High finding.

## 16. Open Decisions

Eleven explicit open decisions, none silently invented — see
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6 (collector buffer duration/storage capacity,
heartbeat timeout tuning, certificate/secret rotation, SNMP versions/security modes,
initial vendor priority, central deployment model, RPO/RTO, object storage, secrets/KMS,
notification/ITSM integration, and scheduled (non-manual) polling).

## 17. Deferred Work (Explicitly, Not Silently)

An actual edge collector agent/binary; the collector-side local durable queue; a
scheduler wiring `run_polling_cycle` to `Integration.poll_interval_seconds`
automatically; the `CollectorDisconnected` event and its detection sweep; a real SNMP
transport implementation; a collector secret rotation endpoint; TLS/mTLS termination
(a deployment-layer concern, not application code this phase can implement).

## 18. Test Results

**Backend**: 371 passed, 0 failed, 0 xfail (306 pre-existing Phase 1–3 tests + 66 new
Phase 8 tests), executed in a single clean run with no concurrent competing process
against the same test database (an earlier contaminated run — two pytest invocations
against the same Postgres database simultaneously — produced spurious cross-test
failures unrelated to any code defect; disclosed here rather than silently omitted,
since it is exactly the kind of environment-vs-code confusion this report should not
paper over).

**Frontend**: `npm run typecheck`, `npm run lint`, `npm run build` all clean. No
automated frontend test suite exists in this repository to extend (`npm test` is not a
defined script) — confirmed by reading `package.json`, not assumed.

**Migration**: fresh install to head, downgrade to `0007_correction`, table-absence
verified after downgrade, re-upgrade to head, RBAC permission re-seed verified
idempotent (exactly 7 rows, no duplicates) — all against real PostgreSQL 16, not SQLite.

## 19. Exit Gate

See the final response for the exact one-of-two-strings verdict, per the master
prompt's requirement that this report never claim "complete" merely because the
implementation runs.
