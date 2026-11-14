# PHASE1_IMPLEMENTATION.md

**Scope:** Phase 1 — Foundation, per `ARCHITECTURE_REVIEW.md` §47 and the Phase 1 implementation prompt.
**Status:** Implemented and verified against real PostgreSQL 16 + Redis 7 instances in this session.

**Correction notice:** §7 (Audit) and the Idempotency-Key handling described in §2/§4's
table originally described guarantees that an independent red-team
(`PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md`) found did not actually hold — audit was not
truly append-only (Finding C1) and idempotency broke under true concurrency (Finding H1).
The affected passages below have been updated to describe the corrected implementation;
see `PHASE1_CORRECTION_REPORT.md` for the full before/after and evidence.

---

## 1. Implemented Scope

Authentication (JWT access + HttpOnly-cookie refresh + CSRF double-submit), RBAC
(User/Role/Permission/RoleAssignment, global-only per H7 Option B), audit (partitioned,
append-only `AuditLog`), the transactional Outbox (with a dispatcher and stale-processing
reclaim), the location hierarchy (Organization→Room), the bare `ManagedAsset`
identity/lifecycle anchor (no subtypes), optimistic concurrency (`version`/`If-Match` on
`Room`, the real Phase 1 entity chosen to demonstrate it), idempotency-key handling (on
`ManagedAsset` creation), request/correlation-ID propagation, structured logging,
liveness/readiness health checks, environment-based configuration with fail-fast
validation, Alembic migrations, Docker/Compose definitions, a GitHub Actions CI workflow,
and a minimal React frontend shell (login, authenticated layout, location list,
managed-asset list).

## 2. Architecture Sections Mapped to Implementation

| Architecture section | What it required | Where implemented |
|---|---|---|
| §4/§4a (Identity, Asset Replacement) | `ManagedAsset` shared-PK identity anchor | `app/domain/identity/models.py` — anchor only, no subtype (§9 of the Phase 1 prompt explicitly scopes replacement's *workflow* to a later phase; the schema field (`replaces_asset_id`) exists and is DB-tested, the `ReplaceAsset` operation itself is not built). Self-reference and replacement-cycle prevention added by migration `0003_correction` (Findings M2/M3 — see `PHASE1_CORRECTION_REPORT.md`): a `CHECK` constraint blocks `replaces_asset_id = id`, and a `BEFORE INSERT OR UPDATE` trigger walks the full replacement chain to block any cycle (direct 2-cycle or longer), not just the immediate case |
| v1.0 §6.1 (User/Role/Permission — retrieved from git history per `PHASE1_BASELINE.md`) | RBAC schema | `app/domain/auth/models.py`, seeded in `migrations/versions/0002_...py` |
| v1.0 §6.2 (Location hierarchy — retrieved from git history) | Organization→Room | `app/domain/location/models.py` |
| §7c (Concurrency) | `version`/If-Match, `FOR UPDATE` pattern | `app/application/concurrency.py`, applied to `Room` in `app/api/v1/locations.py` |
| §21 (Idempotency) | Idempotency-Key ledger | `app/domain/idempotency/models.py`, `app/application/idempotency.py` — atomic claim (`INSERT ... ON CONFLICT DO NOTHING`, committed immediately) rather than a check-then-write cache, so concurrent identical requests serialize correctly instead of racing; applied to `POST /managed-assets`. Corrected (Finding H1 — see `PHASE1_CORRECTION_REPORT.md`): the original check-cache-then-write sequence had no serialization, so genuinely concurrent identical requests mostly received an incorrect `409` instead of the idempotent replay |
| §22 (Outbox) | Transactional outbox, at-least-once dispatch | `app/domain/outbox/models.py`, `app/application/outbox_service.py`, `app/infrastructure/tasks/outbox_dispatcher.py` |
| §30/§30a (Audit) | Partitioned, append-only `AuditLog`, privileged retention role | `app/domain/audit/models.py`, `migrations/versions/0002_...py`, `scripts/bootstrap_privileged_roles.sql` |
| §31 (Security Hardening) | Argon2id, 15-min access/7-day rotating refresh, HttpOnly/Secure/SameSite cookie, CSRF | `app/core/security.py`, `app/api/v1/auth.py` |
| §32/§32a (RBAC/H7) | Global authorization (Option B) | `app/application/rbac.py` — no site-scoping enforcement anywhere, by design |
| §36 (API) | `/api/v1`, RFC 7807 errors, request IDs | `app/core/errors.py`, `app/core/correlation.py`, `app/main.py` |
| §37 (Observability) | Structured logs, liveness/readiness split | `app/core/logging.py`, `app/api/v1/health.py`, `app/db/health.py` |
| §38 (Database) | TIMESTAMPTZ convention, naming convention, partitioning | `app/db/base.py`, `app/domain/audit/models.py` |

## 3. Database Objects

18 tables: `organization, country, city, site, building, floor, room, location_type,
managed_asset, app_user, role, permission, role_permission, role_assignment,
refresh_token, audit_log (partitioned: audit_log_default + 3 monthly partitions),
outbox_event, idempotency_key`. Full column-level detail in
`app/domain/*/models.py` and the two Alembic migrations. Verified via
`alembic upgrade head` against a real PostgreSQL 16 instance, `psql \dt`, and 59 passing
tests exercising the actual constraints (not mocked).

## 4. API

20 endpoints under `/api/v1` — see `PHASE1_TRACEABILITY_MATRIX.md` for the full,
requirement-traced list; the complete live list is also obtainable from any running
instance at `/docs` (OpenAPI) or `/api/v1/openapi.json`.

## 5. Authentication

Argon2id password hashing; 15-minute JWT access tokens (never persisted, memory-only on
the frontend); 7-day rotating refresh tokens tracked server-side by `jti` in
`refresh_token` for revocation; refresh token delivered only via an `HttpOnly`,
`SameSite=Strict` cookie (`Secure` in production, relaxed in development for plain-HTTP
localhost); double-submit CSRF token (`X-CSRF-Token` header must match the
non-HttpOnly `dcim_csrf_token` cookie) required on `/auth/refresh` and `/auth/logout`
specifically, since those are the cookie-authenticated endpoints. Verified end-to-end
against a real running `uvicorn` process with `curl` (login → cookie set → CSRF-less
refresh rejected 403 → CSRF-bearing refresh succeeds 200 → old refresh token rejected
401 on reuse), not just through pytest's simulated transport.

## 6. Authorization

Every mutating and every sensitive-read route depends on
`require_permission("resource:action")` (`app/application/rbac.py`); five default roles
seeded (Administrator, DCIM Manager, Engineer, Operator, Viewer) each mapped to an
explicit permission set (a role that can write a resource also explicitly holds read on
it — permission codes are checked for exact match, not inferred hierarchy). No frontend
code makes an authorization decision; `ProtectedRoute` only avoids flashing UI at an
unauthenticated visitor.

## 7. Audit

Every location-hierarchy write, every `ManagedAsset` create/lifecycle-transition, and
every login writes a synchronous `AuditLog` row in the same transaction as the mutation.
Table is monthly-partitioned; the application's DB role (`dcim_app`) has `INSERT`/`SELECT`
only.

**Corrected (Finding C1 — see `PHASE1_CORRECTION_REPORT.md`):** verifying only
`UPDATE`/`DELETE` denial, as the original implementation did, is not sufficient — `dcim_app`
also retained `TRUNCATE` (a separate, distinct privilege the original migration never
revoked) and, because it *owned* `audit_log`, retained `ALTER`/`DROP` rights regardless of
any `REVOKE` (PostgreSQL treats those as inherent to ownership, not a grantable privilege).
A deeper layer, found only while validating the fix: PostgreSQL also grants the *database
owner* an implicit `DROP TABLE` right over every table in that database, independent of
per-table ownership — so `dcim_app` being the owner of the `dcim` database itself (the
original setup) could still drop `audit_log`'s partitions even after table-level ownership
was transferred away. audit_log (and every partition) is now owned by a dedicated,
`NOLOGIN` `dcim_retention_admin` role (`scripts/bootstrap_privileged_roles.sql`), `dcim_app`
is granted exactly `INSERT`/`SELECT`, `TRUNCATE` is explicitly revoked as defense-in-depth,
and `dcim_app` is no longer the database owner. Partition creation (which requires
ALTER-level rights on the parent) goes through a narrowly-scoped `SECURITY DEFINER` SQL
function `dcim_app` is granted `EXECUTE` on, not direct DDL. Verified against real
`dcim_app` *login* connections (not a superuser session with `SET ROLE`, which can mask the
database-ownership behavior): `TRUNCATE`, `UPDATE`, `DELETE`, `ALTER`, and `DROP` (both the
parent table and any individual partition) all fail with `permission denied`/`must be
owner`; `INSERT`/`SELECT` and partition creation via the scoped function still succeed.

## 8. Outbox

`ManagedAssetCreated`/`ManagedAssetLifecycleChanged` events written atomically alongside
their audit rows; a Celery-based dispatcher claims pending (and stale-`processing`,
reclaimed after a 5-minute timeout) rows with `SELECT ... FOR UPDATE SKIP LOCKED`,
dispatches at-least-once, retries with exponential backoff, dead-letters after 5 attempts.
Verified with a real Celery worker consuming from a real Redis broker against a real
Postgres-backed event.

## 9. Background Processing

Celery app with 8 named queues matching the architecture's eventual isolation needs
(`default, maintenance, polling, telemetry, alarms, imports, reports, notifications,
high_priority`) — Phase 1 only schedules work on `default` (outbox dispatch, every 5s)
and `maintenance` (audit partition creation, daily). No domain-specific work (polling,
telemetry, alarms, imports) is scheduled or implemented, per explicit Phase 1 scope.

## 10. Frontend Foundation

Vite + React 18 + TypeScript (strict) + Tailwind + TanStack Query + Zustand + React
Router 7. Login page, authenticated app shell with permission-agnostic navigation (no
role-based hiding implemented — every nav item is visible; the backend is what actually
enforces access, consistent with §27's "frontend authorization is never security
enforcement"), a locations list/create page, and a read-only managed-assets list page.
No floor-plan editor, rack elevation, 3D twin, power/network UI, telemetry dashboard, or
alarm center — all explicitly out of Phase 1 scope.

## 11. Observability

`structlog` JSON logs with automatic redaction of sensitive field names, `request_id`/
`correlation_id` bound via `contextvars` and propagated into every audit row and outbox
event written during that request. `/health/live` never touches a dependency;
`/health/ready` reports database and Redis state independently rather than collapsing
into one boolean — verified both report `"up"` against real instances.

## 12. Security

See `PHASE1_IMPLEMENTATION_REPORT.md` §Security for the full adversarial review
(unauthorized access, privilege escalation attempts, SQL-injection-shaped input, CORS,
error-leakage, dependency vulnerabilities) and its results.

## 13. Testing

59 tests originally (`pytest`), all passing against real PostgreSQL 16 and Redis 7
instances started for this session — not SQLite, not mocks, not an in-memory substitute.
Breakdown: 15 unit (pure logic), 12 integration (real DB constraints, outbox atomicity,
audit append-only, stale-event reclaim), 32 API (real HTTP contract via FastAPI's ASGI
transport: auth, RBAC, concurrency, security, health).

**Updated by the targeted correction** (`PHASE1_CORRECTION_REPORT.md`): 76 tests total.
The 17 new tests close exactly the gap category the red-team identified — none of the
original 59 attempted `TRUNCATE`/DDL as the `dcim_app` role, and none fired genuinely
concurrent HTTP requests — plus regression coverage for the M2/M3 replacement
self-reference/cycle findings and the M1/L3 findings (oversized input, readiness status
code).

## 14. Known Limitations

1. `alembic upgrade head` cannot create the privileged `dcim_retention_admin` role
   itself (by design — the migration role deliberately lacks `CREATEROLE`); it requires
   the separate, manually-run `scripts/bootstrap_privileged_roles.sql`, documented in
   the README.
2. `docker compose up` was not executed in this session (no Docker daemon available) —
   Dockerfiles and the compose file are written and reviewed but not build-verified.
3. `esbuild`/Vite carry a known moderate, dev-server-only vulnerability
   (`GHSA-67mh-4wv8-2f99`) with no fix available inside the Vite 5.x line; a fix requires
   Vite 8, a breaking major upgrade not attempted in this session given the narrow,
   dev-only blast radius. Tracked, not silently ignored.
4. `ManagedAsset` subtype tables (`Rack`, `Equipment`, `PDU`, ...) do not exist —
   correctly deferred to Phase 2/3/7.

## 15. Deferred Functionality

Everything listed in §39 of the Phase 1 prompt (rack/equipment/floor-plan/power/network/
integration/telemetry/alarm/visualization/thermal/reporting/AI) plus every red-team
finding H2–H9/M2–M10/F2–F6 from the architecture validation chain — all confirmed still
tracked, none accidentally resolved or contradicted by this phase's code. See
`PHASE1_DEVIATIONS.md` and `PHASE1_TRACEABILITY_MATRIX.md`.
