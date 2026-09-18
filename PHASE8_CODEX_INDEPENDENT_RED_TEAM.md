# Phase 8 Codex Independent Red-Team

## Executive summary

**PHASE 8 CODEX INDEPENDENT RED-TEAM FAILED — CORRECTIONS REQUIRED**

The commit has a sound basic HMAC/replay design and prevents automatic discovery writes to `ManagedAsset`, but it is not safe to accept. A user with `integration:manage` can register arbitrary REST targets and a user with `collector:manage` can cause the central process to request them; there is no URL, DNS/IP, redirect, port, or egress policy. This is an SSRF primitive. The new site identities are also not enforced at the authorization boundary: global read/manage permissions expose and mutate all sites' integrations, collectors, discoveries, and reconciliation decisions. The promised Protocol → Driver → VendorProfile → DeviceProfile → MetricMapping architecture is not implemented as persistent domain objects.

| Severity | Confirmed | Static findings |
|---|---:|---:|
| CRITICAL | 0 | 0 |
| HIGH | 0 | 3 |
| MEDIUM | 0 | 5 |
| LOW | 0 | 2 |
| INFO | 0 | 2 |

“Confirmed” requires a completed runtime exploit in this environment. The findings below are **STATIC ANALYSIS** unless explicitly labelled otherwise.

## Repository / commit verification

**EXECUTED — VERIFIED.** Remote is `https://github.com/AhmedMahmoud2222/DCIM.git`; the checkout identifies `AhmedMahmoud2222/DCIM`. Current branch before audit work was `claude/new-session-1vutvy`, HEAD was `fc5fd57e59f71836828decb66dcdfb669f44b47c`, and `HEAD^` was `99fce6555d1d75152b09a61780c6a8e837395ee4`. The tree was clean.

`git diff --name-status 99fce... fc5fd...` confirms a single commit adding the Phase 8 docs, integration/collector/discovery domain/API/application/driver code, migration `0008`, Phase 8 tests and frontend screens; it modifies routing, RBAC, configuration, error handling, dependencies, test configuration, and frontend navigation/types. No Phase 1–3 implementation files were changed except shared RBAC/error/config/test plumbing.

## Architecture and data model

**STATIC ANALYSIS.** `DiscoveredDevice` is separate from `ManagedAsset`; `ingest_discovery` does not create/mutate inventory and reconciliation links only an existing asset. This correctly preserves the discovery/authoritative-inventory boundary. However, the required `VendorProfile`, `DeviceProfile`, and persistent `MetricMapping` layers do not exist: `MetricMapping` is only a driver-local dataclass in `drivers/snmp.py`, is never loaded for normal polling, and SNMP's production path supplies no transport. Thus the documented protocol chain is not actually available as a domain model.

Migration 0008 has useful DB checks for collector type/status, integration type, discovered-device/diff status, core foreign keys, unique collector name, unique `(integration_id, external_identifier)`, nonce uniqueness, and a partial unique current assignment. It does **not** DB-enforce integration site ownership, collector-assignment site compatibility, valid ports/poll intervals/failure counters, protocol-specific config, capability protocol code, non-overlapping historical assignment periods, reconciliation state/link consistency, or JSON shape/size. These are not equivalent to DB invariants.

## Security, collector boundary, RBAC / IDOR

**STATIC ANALYSIS.** HMAC signs raw body plus collector ID/timestamp/nonce, uses `compare_digest`, checks active status, uses a five-minute window, and atomically claims a nonce through PostgreSQL `ON CONFLICT`. Missing, malformed, stale, altered-body, wrong-ID, disabled and replayed requests are designed to fail. Collector-to-path mismatch is also rejected. Those controls could not be runtime-verified because PostgreSQL was unavailable.

The collector request body is read before any application request-size guard (`get_current_collector` calls `await request.body()`), then Pydantic parses it. The later 500-record and 8 KiB-per-attribute rules do not cap total HTTP body size, JSON nesting, header size, or a 500-record batch whose fields/configuration consume substantial memory. Nonce rows are never pruned, making the replay table unbounded.

`require_permission` intentionally ignores `RoleAssignment.scope_type/scope_id`. New list/get/update/reconcile routes do no site predicate or object-level check. A valid role holder can list Site B rows and patch a known Site B integration; a discovery reconciler can link Site B discovery to any existing asset. The documented `assign_integration` check only prevents explicitly mismatched edge-collector/integration assignment, not user-level cross-site access. UUID knowledge is sufficient for direct GET/PATCH/reconcile operations.

## SSRF, ICMP and SNMP

**STATIC ANALYSIS — HIGH SSRF.** `IntegrationIn` accepts arbitrary `target_host`, nullable unrestricted port, arbitrary `config`, and REST `scheme`, `path`, `method`, `headers`, and `credential_header`. `RESTDriver.connect` concatenates them into a URL and `poll` issues `httpx.AsyncClient.request` with no allowlist, no IP/DNS resolution checks, no private/link-local/loopback/metadata block, no allowed-port/scheme policy, and no explicit egress proxy. `poll-now` then runs this from the central process. Reproduction once runtime is available: create a `rest` integration with `target_host=169.254.169.254`, `scheme=http`, metadata path, assign it to a capable central collector, and call `poll-now`; likewise test `127.0.0.1`, RFC1918, IPv6 loopback/link-local, DNS rebinding, redirects and arbitrary ports. The 256 KiB cap occurs after `response.content` has already buffered the complete response, so it is not a response-memory limit.

ICMP uses a raw IPv4 socket with no target validation, no IPv6 support, no concurrency/rate limiter, and a fixed two-second timeout. It does not shell out, so there is no command-injection path. SNMP accepts v1/v2c/v3 labels but has no production transport and no persistent metric/profile configuration; it cannot deliver a real SNMP integration and has no OID policy.

## Discovery, reconciliation, concurrency and idempotency

**STATIC ANALYSIS.** Discovery upserts on `(integration_id, external_identifier)` and does not overwrite authoritative asset fields. But the select-then-insert implementation is race-prone: concurrent first sightings can both observe no row; the unique violation is not handled as an idempotent update. A concurrent acceptance of the same diff relies on application observation of `pending`, not a row lock/version/conditional update. No site consistency is checked between a discovered device's integration and `matched_managed_asset_id`.

The ingestion path claims one existing idempotency record per collector/dedup key and commits accepted records individually. It achieves at-least-once transport handling only where the prior claim completes. It has no durable edge queue, retry/backoff/restart behavior, ordering/staleness policy, or telemetry persistence. `batch_id` is not deduplicated. Per-record exception text is returned to the collector (`error=str(exc)`), exposing database/application details on failed records. Reconciliation decisions create audit records but no outbox event; accepted/rejected decisions therefore lack the stated transactional event handoff.

## Audit, outbox, observability and performance

**STATIC ANALYSIS.** Registration, integration creation/update, capability declaration, assignment and reconciliation write audits; registration/integration/discovery/heartbeat have some outbox events. Credential plaintext is omitted from normal integration output/audit/outbox payloads, but `config` is returned in full and arbitrary headers placed there can be sensitive. Fernet encryption is at rest only; one environment key decrypts every integration and collector credential, with no KMS or rotation endpoint.

The two self-reported N+1 corrections are present: collector health uses a `DISTINCT ON` bulk query and integration lists bulk-load assignments. No execution/query-count proof was possible. `poll-now` processes assigned integrations serially with no per-user/collector rate limit and can tie up request workers on slow outbound calls.

## Frontend, migration and regression execution

**EXECUTED — VERIFIED:** `uv run --directory backend --extra dev ruff check app tests` ran and reported 14 repository-wide violations, all outside Phase 8 files in the displayed output. It did not pass.

**NOT EXECUTED — ENVIRONMENT LIMITATION:** Docker is not installed. PostgreSQL test setup failed before the first Phase 8 unit test with `ConnectionRefusedError [WinError 1225]` from asyncpg. Therefore migrations (fresh/upgrade/downgrade/re-upgrade), API attacks, genuine concurrency, DB constraints/query plans, full pytest suite, and frontend lint/typecheck/build/browser validation were not run. `npm` is broken in this host (`npm-cli.js` missing), so frontend commands could not be started.

## Claude’s five claimed corrections

| Claim | Independent result |
|---|---|
| N+1 queries | Static code confirms bulk health/assignment queries; not measured. Equivalent N+1 risk remains in per-record ingestion. |
| Cross-site assignment | Explicit collector/integration mismatch is rejected, but null-site bypass and all user-facing site IDOR remain. Incomplete. |
| Oversized payloads | Per-record `raw_attributes` cap and record-count cap exist; total pre-parse request size, nesting and response buffering are unbounded. Incomplete. |
| Collector authorization | Current assignment is checked per record. Site ownership and assignment race behavior are not fully enforced. Partially corrected. |
| Audit logging | Several operations audit, but reconciliation has no outbox and failed-record errors leak internals. Partially corrected. |

## Findings and required corrections

| ID | Severity | File / evidence | Required correction |
|---|---|---|---|
| H1 | HIGH | `backend/app/application/drivers/rest.py:connect/poll`; `api/v1/integrations.py` | Enforce canonical HTTP(S) URL parsing, DNS/IP revalidation, deny loopback/private/link-local/multicast/metadata ranges, constrained ports/methods/redirects, egress policy and streamed bounded response reads. |
| H2 | HIGH | `application/rbac.py:get_auth_context`; all new list/get/mutate routes | Implement and test site-scoped predicates/object authorization, including reconciliation asset/device same-site checks. |
| H3 | HIGH | `domain/integration/models.py`; `drivers/snmp.py` | Implement or explicitly remove unsupported claims: persistent VendorProfile/DeviceProfile/MetricMapping, validated configuration, and real secure SNMP transport before accepting Phase 8. |
| M1 | MEDIUM | `api/v1/collectors.py:get_current_collector/ingest_batch` | Enforce a total request Content-Length/body cap before buffering/parsing; cap nesting and batch bytes; add rate/backpressure limits. |
| M2 | MEDIUM | `discovery_service.py:ingest_discovery/accept_reconciliation` | Use DB-native upsert/locking or conditional updates; make concurrent discovery/decision results deterministic and test them against PostgreSQL. |
| M3 | MEDIUM | `api/v1/collectors.py:ingest_batch` | Do not return raw exception strings; use stable public errors and structured secure logs. |
| M4 | MEDIUM | `domain/integration/models.py:CollectorRequestNonce` | Prune nonce rows after the replay window with an indexed retention job; bound table growth. |
| M5 | MEDIUM | `discovery_service.py`, reconciliation endpoints | Emit a transactional reconciliation decision outbox event; enforce asset/device site compatibility. |
| L1 | LOW | `rest.py:poll` | Stream response bytes rather than accessing full `response.content`; constrain decompression/redirect behavior. |
| L2 | LOW | `drivers/icmp.py`; integration models | Validate targets, add IPv6 behavior or reject it explicitly, and apply concurrency/rate controls. |

## Final verdict

**PHASE 8 CODEX INDEPENDENT RED-TEAM FAILED — CORRECTIONS REQUIRED**

Acceptance must wait for H1–H3, PostgreSQL migration/concurrency verification, and the unavailable full backend/frontend regression suite.
