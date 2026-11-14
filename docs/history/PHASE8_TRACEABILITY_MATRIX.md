# Phase 8 Traceability Matrix

Requirement → Implementation → Test → Evidence. Requirement numbers refer to the section
numbering of the Phase 8 master prompt ("Claude Code — Phase 8 Integrations + Edge
Collectors — Scope Reconciliation → Implementation → Adversarial Validation").

| # | Requirement | Implementation | Test(s) | Evidence / Status |
|---|---|---|---|---|
| 1 | Scope reconciliation before implementation | Architecture-phase vs. repo-phase mapping table | — | `PHASE8_GAP_ANALYSIS.md` §1 — IMPLEMENTED |
| 2 | Read architecture before implementing | §12/18/19/20/21/22/23/25/26/27/31/32/37/42/43/44/47/48/49/50 read and applied | — | Cited throughout this matrix and the implementation report — STATICALLY VERIFIED |
| 3 | Objective: Integrations+Collectors foundation, central remains system of record | Full domain model + services below | Full test suite | IMPLEMENTED |
| 4 | `Integration` domain model | `app/domain/integration/models.py::Integration` | `test_integrations.py` (9) | TESTED |
| 4 | `Collector` domain model (central/edge, site, status, version, heartbeat) | `app/domain/integration/models.py::Collector` | `test_collectors.py` (24) | TESTED |
| 4 | `CollectorCapability` (declarative) | `app/domain/integration/models.py::CollectorCapability` | `test_declare_and_list_capabilities` | TESTED |
| 4 | `CollectorAssignment` (explicit, answerable both directions) | `app/domain/integration/models.py::CollectorAssignment` + temporal partial-unique-index pattern | `test_assignment_*` (7 tests), `test_phase8_concurrency.py::test_concurrent_reassignment_race_*` | TESTED (including genuine concurrency) |
| 4 | `CollectorHeartbeat` (health never inferred from device health) | `app/domain/integration/models.py::CollectorHeartbeat`, `collector_service.classify_collector_health`/`classify_collector_health_bulk` | `test_list_and_get_collector_includes_computed_health`, `test_heartbeat_with_valid_signature_updates_health_to_healthy` | TESTED |
| 4 | Credentials never plaintext | `app/core/secrets.py` (Fernet), `Integration.credential_ciphertext`/`Collector.secret_ciphertext` | `test_create_integration_returns_no_credential_field`, `test_update_credential_then_read_never_exposes_it` | TESTED |
| 5 | Protocol→Driver→Vendor Profile→Device Profile→Metric Mapping layering, no `if vendor==` branching | `app/application/drivers/base.py` (`ProtocolDriver` ABC), `DRIVER_REGISTRY` dict dispatch in `drivers/__init__.py` | `grep -rn "vendor ==" app/` (none found) | STATICALLY VERIFIED — no vendor-conditional branching anywhere in the codebase |
| 6 | ICMP driver | `app/application/drivers/icmp.py` — real raw `SOCK_RAW`/`IPPROTO_ICMP` socket, not a shell-out | `test_icmp_driver_pings_localhost_successfully`, `test_icmp_driver_times_out_when_no_reply_arrives_in_time`, `test_icmp_driver_disconnect_is_idempotent`, `test_poll_now_succeeds_on_real_icmp_and_creates_a_discovered_device` | TESTED against a real socket/real loopback ping |
| 6 | SNMP driver framework (transport/auth/OID/mapping/vendor separation), not every vendor's MIB | `app/application/drivers/snmp.py` — `MetricMapping`, `SNMPTransport` protocol, `SimulatedSNMPTransport` test double | `test_snmp_driver_with_simulated_transport`, `test_snmp_driver_rejects_unsupported_version`, `test_snmp_driver_without_transport_factory_is_honest_about_scope` | Abstraction TESTED; real-hardware polling explicitly NOT TESTED (no real SNMP transport built — disclosed, `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6) |
| 6 | REST driver (generic, no hardcoded vendor) | `app/application/drivers/rest.py` | `test_rest_driver_polls_real_health_endpoint_in_process`, `test_rest_driver_raises_on_http_error_status` | TESTED against this app's own real `/api/v1/health/live` endpoint (in-process ASGI, not mocked) |
| 7 | Discovery ≠ authoritative inventory; no unattended overwrite | `app/application/discovery_service.py::ingest_discovery`/`accept_reconciliation`/`reject_reconciliation` — structurally the ONE function permitted to set `matched_managed_asset_id`, never creates a `ManagedAsset` | `test_discovery_never_creates_managed_asset_rows`, `test_accept_rejects_nonexistent_managed_asset` | TESTED — DB row-count assertion proves no authoritative-table writes |
| 8 | Outbound-only Edge Collector transport model | HMAC request signing initiated BY the collector TO central (`POST` from collector); central never opens a connection to a collector | — | Architecturally satisfied by construction (no inbound-to-collector code path exists) — STATICALLY VERIFIED |
| 9 | Secure transport: identity, signing, replay protection, timestamp validation, payload validation/size limits, idempotent ingestion | `app/application/collector_auth.py` (HMAC-SHA256, nonce, 300s window), `IngestRecordIn` field bounds, `MAX_BATCH_RECORDS`/`MAX_RAW_ATTRIBUTES_BYTES` | `test_collector_auth.py` (8), `test_ingest_batch_rejects_oversized_batch`, `test_ingest_batch_rejects_oversized_single_record_raw_attributes` | TESTED. TLS/mTLS is a deployment-layer concern, not application code — disclosed in `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §3 as NOT applicable to this phase's code |
| 9 | Never use user JWT as collector trust mechanism | `get_current_collector` is a wholly separate FastAPI dependency from `get_current_user`/`require_permission` | `test_heartbeat_requires_collector_signature_not_user_jwt` | TESTED |
| 10 | WAN outage / store-and-forward (central-side contract) | `POST /collectors/{id}/ingest` — per-record idempotency, `occurred_at`/`received_at` both preserved, per-record ACK, partial-batch-failure isolation | `test_ingest_batch_is_idempotent_on_duplicate_dedup_key`, `test_ingest_batch_one_bad_record_does_not_fail_the_rest` | TESTED (central side). Collector-side local queue/buffer does NOT exist in this phase (no collector agent is built) — explicit boundary in `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §4, open decisions in §6 |
| 11 | Idempotency reused, not duplicated | `app.application.idempotency` (`get_or_claim`/`complete_claim`/`release_claim`) reused verbatim, keyed `f"{collector_id}:{dedup_key}"` | `test_ingest_batch_is_idempotent_on_duplicate_dedup_key` | TESTED |
| 12 | Outbox reused, only necessary event types | `CollectorRegistered`, `CollectorHeartbeatReceived`, `IntegrationEnabled`, `IntegrationDisabled`, `DeviceDiscovered`, `ReconciliationRequired` via existing `write_outbox_event` | Full suite (outbox rows written in same transaction as domain writes) | IMPLEMENTED. `CollectorDisconnected` NOT implemented — no background sweep exists to detect and fire it; disclosed as deferred (`PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6) |
| 13 | Observability: collector/integration metrics, device/collector/integration health kept SEPARATE | `CollectorOut`/`IntegrationOut` API response fields; `classify_collector_health` never reads `Integration`/device state | `test_list_and_get_collector_includes_computed_health` | TESTED |
| 14 | Failure isolation (device/integration/driver/collector) | `run_polling_cycle`'s per-integration try/except (one documented broad catch, scoped to exactly one integration) | `test_poll_now_one_failing_integration_does_not_prevent_a_sibling_from_succeeding` | TESTED — real SNMP failure alongside real ICMP success in the same cycle |
| 15 | RBAC reused; global-only preserved; data model doesn't block future site scoping | 7 new permission codes in `DEFAULT_ROLE_PERMISSIONS` (`rbac.py`); every `Integration`/`Collector` already carries a nullable `site_id` column | `test_viewer_cannot_register_collector`, `test_viewer_can_read_but_not_create`, `test_viewer_can_read_but_not_reconcile` | TESTED |
| 16 | Multi-site representation without implicit naming | `Site` (existing) → `Collector.site_id` → `CollectorAssignment` → `Integration`; site mismatch rejected at assignment time (finding N2) | `test_assignment_rejects_collector_and_integration_scoped_to_different_sites` | TESTED |
| 17 | Do not build telemetry yet | No `TelemetryReading`/time-series/alarm/notification/AI/CFD/3D-twin code added | `grep` of diff | STATICALLY VERIFIED — clean contract in `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §5 |
| 18 | Do not redesign Power | No file under `app/domain/power/`, `app/application/power_*.py`, or migrations 0006/0007 modified | `git diff --stat` (see implementation report) | STATICALLY VERIFIED |
| 19 | No duplicated cross-cutting infrastructure | Reused: audit, outbox, idempotency, RBAC, concurrency (If-Match), error handling, Argon2 module considered and explicitly rejected for the collector secret (documented reasoning, not silent duplication) | — | STATICALLY VERIFIED (see `PHASE8_GAP_ANALYSIS.md` §2) |
| 20 | Testing requirements (identity/assignment/protocol/discovery/reconciliation/security/WAN/failure-isolation/concurrency) | See rows above and §"Test Counts" below | 372 backend tests total (306 pre-existing Phase 1-3 + 66 new Phase 8), 0 failed | TESTED — see Implementation Report §"Test Results" |
| 21 | Migration: Alembic, additive, fresh/upgrade/downgrade/re-upgrade on real Postgres | `migrations/versions/0008_phase8_integrations_and_collectors.py` | Manual validation: fresh install → head, downgrade to 0007, table-absence verified, re-upgrade to head, RBAC re-seed verified idempotent | TESTED against real PostgreSQL 16 (not SQLite) |
| 22 | Minimum frontend: Collectors/Integrations/Discovery | `frontend/src/features/integrations/{CollectorsPage,IntegrationsPage,DiscoveryPage}.tsx` | `npm run typecheck`/`lint`/`build` | TESTED (typecheck/lint/build clean); no automated frontend test suite exists in this repo to extend (confirmed — no `npm test` script) |
| 23 | Edge Collector ownership contract documented | `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §1 | — | Present |
| 24 | Phase 9 contract shown, not implemented | `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §5 | — | Present |
| 25 | Red-team own implementation (30 items) | See `PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md` | Regression tests added per finding | 5 findings, all corrected with regression tests (N1, N2, S1, S2, plus an audit-completeness gap) |
| 26 | Performance measured at 1/10/100/500 collectors | `tests/api/test_phase8_performance.py` | `test_list_collectors_query_count_at_scale[1,10,100,500]` | TESTED — flat 4 queries at every scale point after the N1 fix (was linear before) |
| 27 | Security boundary review | `PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md` §"Trust Boundary Analysis" | `test_ingest_rejects_a_record_for_an_integration_not_assigned_to_this_collector` | TESTED |
| 28 | Open decisions explicitly identified | `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §6 (11 decisions) | — | Present |
| 29 | Five required documentation files | This file + 4 others | — | All present at repo root |
| 30 | Exit gate | See Implementation Report | — | See final response |
| 31 | Commit requirement | Single commit, `git status --short` clean afterward | — | See final response for exact SHA |
| 32 | Final response format | 27-item response | — | Delivered in this task's final message |

## Coverage Summary

- Domain model: 8/8 new tables implemented and tested (`Collector`, `CollectorCapability`,
  `CollectorHeartbeat`, `CollectorRequestNonce`, `Integration`, `CollectorAssignment`,
  `DiscoveredDevice`, `ReconciliationDiff`).
- Protocol drivers: 3/3 implemented (ICMP genuinely tested against a real socket, REST
  genuinely tested against a real in-process HTTP endpoint, SNMP an honestly-disclosed
  abstraction-only foundation).
- New tests: 66, all passing, 0 xfail.
- Findings from the self-red-team: 5, all corrected in this same task with a regression
  test each (none left open as "blocking").
- Deferred (not blocking, explicitly documented): actual edge collector agent/binary,
  collector-side local durable queue, scheduled (non-manual) polling, `CollectorDisconnected`
  event, real SNMP transport, collector secret rotation endpoint, TLS/mTLS (deployment-layer).
