# PHASE1_TRACEABILITY_MATRIX.md

Every Phase 1 requirement (from the Phase 1 implementation prompt, §§7–37) traced from
architecture → implementation → test. "DEFERRED — FUTURE PHASE" marks anything correctly
out of scope.

| Architecture Requirement | Architecture Section | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|
| Async DB session + connection pooling | §7 | `app/db/session.py` | `tests/integration/*` (implicitly, every test uses it) | DONE | `pool_size=10, max_overflow=5`, `pool_pre_ping=True` |
| Migration framework | §7, §28 | `alembic.ini`, `migrations/` | Manual: `alembic upgrade head` run twice from scratch (dev + test DBs) | DONE | Autogenerate verified against real schema |
| Database health check | §7, §22 | `app/db/health.py` | `tests/api/test_health.py` | DONE | |
| TIMESTAMPTZ universal convention | §7 | `app/db/base.py` (`TimestampMixin`), every model | Implicit in all DB tests | DONE | Documented in `Base`'s own docstring |
| Naming convention / FK enforcement | §7 | `app/db/base.py` (`NAMING_CONVENTION`) | `tests/integration/test_db_constraints.py` | DONE | |
| Location hierarchy (Organization→Room) | §8, v1.0 §6.2 | `app/domain/location/models.py`, `app/api/v1/locations.py` | `tests/api/test_locations.py` | DONE | Schema retrieved from git history — see `PHASE1_BASELINE.md` |
| Floor-plan geometry / 2D / 3D | §8 (explicit exclusion) | — | — | DEFERRED — FUTURE PHASE | Correctly not built |
| `ManagedAsset` identity/lifecycle anchor | §9 | `app/domain/identity/models.py` | `tests/integration/test_db_constraints.py`, `tests/api/test_managed_assets.py` | DONE | No subtype tables |
| `ManagedAsset.id` immutability | §9, §12 | PK, never updated; `replaces_asset_id` FK | `tests/integration/test_db_constraints.py::test_asset_replacement_identity_never_collides` | DONE | Full `ReplaceAsset` *workflow* deferred |
| Lifecycle states/transitions | §10 | `app/domain/identity/models.py` (`ALLOWED_LIFECYCLE_TRANSITIONS`) | `tests/unit/test_lifecycle.py`, `tests/api/test_managed_assets.py` | DONE | No invented states |
| Lifecycle transition auditability | §10 | `app/api/v1/managed_assets.py::transition_lifecycle` | `tests/api/test_managed_assets.py` | DONE | |
| Full Asset Replacement workflow | §10 (explicit exclusion) | — | — | DEFERRED — FUTURE PHASE | Identity relationship (`replaces_asset_id`) exists now per §9; workflow later |
| RBAC: global authorization only (H7 Option B) | §11 | `app/application/rbac.py`, `app/domain/auth/models.py` | `tests/api/test_locations.py`, `test_managed_assets.py`, `test_security.py` | DONE | No site-scoping filter anywhere |
| RBAC structured for later site-scoping | §11 | `RoleAssignment.scope_type/scope_id` columns present, unused | — | DONE | Additive when built, per architecture |
| Password hashing (Argon2id) | §12 | `app/core/security.py` | `tests/unit/test_security.py` | DONE | |
| Access/refresh token issuance | §12 | `app/core/security.py`, `app/application/auth_service.py` | `tests/unit/test_security.py`, `tests/api/test_auth.py` | DONE | |
| Refresh-token rotation/revocation | §12 | `app/application/auth_service.py`, `app/domain/auth/models.py::RefreshToken` | `tests/api/test_auth.py` | DONE | Server-side `jti` tracking |
| Secure cookie attributes / CORS | §12 | `app/api/v1/auth.py`, `app/main.py` | `tests/api/test_security.py::test_cors_headers_present_for_configured_origin` | DONE | `HttpOnly/SameSite=Strict`, `Secure` in prod |
| No secret/credential logging | §12 | `app/core/logging.py` (`_redact_sensitive`), `app/application/audit_service.py` (`_redact`) | `tests/api/test_security.py::test_response_never_includes_password_hash` | DONE | |
| Backend-enforced authorization | §13 | `app/application/rbac.py::require_permission` on every mutating route | `tests/api/test_security.py`, `test_locations.py::test_viewer_cannot_create_locations` | DONE | |
| Audit foundation (who/when/what/before/after) | §14 | `app/domain/audit/models.py`, `app/application/audit_service.py` | `tests/integration/test_db_constraints.py`, `test_outbox.py` | DONE | |
| Audit distinct from domain events/Outbox | §14 | Separate tables, separate write paths | `tests/integration/test_outbox.py` | DONE | |
| Request/correlation identity | §15 | `app/core/correlation.py` | Implicit in every API test (headers echoed) | DONE | Propagated into audit + outbox |
| Consistent API error handling (RFC 7807) | §16 | `app/core/errors.py` | `tests/api/test_security.py::test_404_does_not_leak_internal_details` | DONE | |
| 409 on optimistic-concurrency conflict | §16, §17 | `app/application/concurrency.py`, `app/api/v1/locations.py::update_room` | `tests/api/test_locations.py::test_room_update_with_stale_version_is_rejected_with_409` | DONE | |
| No internal stack traces to clients | §16 | `app/core/errors.py` (generic 500 handler) | `tests/api/test_security.py::test_404_does_not_leak_internal_details` | DONE | Not tested for an actual 500 (none induced) |
| Optimistic concurrency (version/If-Match) foundation | §17 | `app/application/concurrency.py` | `tests/unit/test_concurrency.py`, `tests/api/test_locations.py` (3 tests) | DONE | Demonstrated on `Room`, a real entity |
| Concurrency mechanism reusable for RackPlacement/EquipmentPlacement/PowerConnection | §17 | Pattern documented in `concurrency.py`'s module docstring | — | DONE (pattern only) | Those tables don't exist yet |
| Transactional Outbox | §18 | `app/domain/outbox/models.py`, `app/application/outbox_service.py` | `tests/integration/test_outbox.py` (5 tests) | DONE | Atomicity verified via rollback test |
| Outbox dispatcher (at-least-once, retry, recovery) | §19 | `app/infrastructure/tasks/outbox_dispatcher.py` | `tests/integration/test_outbox.py` (reclaim tests), manual Celery-worker smoke test | DONE | Stale-processing reclaim added after a real bug was found live |
| Redis/Celery foundation, named queues | §20 | `app/infrastructure/celery_app.py` | Manual: task registration verified (`celery_app.tasks`), worker boot log inspected | DONE | Only `default`/`maintenance` actually scheduled |
| No premature domain-specific job logic | §20 | No polling/telemetry/alarm/import task code exists | — | DONE (by absence) | |
| Idempotency (request/event/job/external identity distinguished) | §21 | `app/domain/idempotency/models.py`, `app/application/idempotency.py`; event_id (Outbox), jti (tokens), job_id (Celery) all distinct | `tests/unit/test_idempotency.py`, `tests/api/test_managed_assets.py` (2 tests) | DONE | |
| Structured logs with required fields | §22 | `app/core/logging.py` | Manual: log output inspected during smoke tests | DONE | |
| Liveness/readiness split | §22 | `app/api/v1/health.py` | `tests/api/test_health.py` | DONE | Liveness never touches DB/Redis |
| Basic metrics infrastructure | §22 | Not implemented | — | **GAP** | See `PHASE1_DEVIATIONS.md` D2 |
| Environment-based configuration, fail-fast validation | §23 | `app/core/config.py` (pydantic-settings, required fields) | Manual: app fails to import without required env vars | DONE | |
| `.env.example` | §23 | `backend/.env.example`, root `.env.example` | — | DONE | |
| Docker Compose local dev environment | §24 | `docker-compose.yml`, both Dockerfiles | Config review only — **not run** (no Docker daemon) | PARTIAL | See `PHASE1_DEVIATIONS.md` D1 |
| `/api/v1` foundation, OpenAPI, pagination, health | §25 | `app/main.py`, `app/api/pagination.py` | `tests/api/*` (all), manual `app.openapi()` inspection (20 paths) | DONE | |
| Only Phase 1 domain endpoints | §25 | Confirmed: auth, users (bootstrap only), locations, managed-assets, health | — | DONE | |
| React shell: bootstrap, routing, auth state, API client, TanStack Query | §26 | `frontend/src/app/*`, `frontend/src/lib/*`, `frontend/src/features/auth/*` | Manual: `npm run build`/`typecheck`/`eslint` all clean; dev server + API proxy verified live | DONE | |
| No floor-plan/rack/3D/power/network/telemetry/alarm UI | §26 (explicit exclusion) | — | — | DEFERRED — FUTURE PHASE | Correctly not built |
| Frontend authorization is UX-only | §27 | `ProtectedRoute.tsx` (route-level only, no permission-based hiding) | — | DONE | Backend is the only enforcement point |
| Alembic migrations: deterministic, reviewable, no destructive auto-migration | §28 | `migrations/versions/*.py`, both hand-reviewed after autogenerate | Manual: applied twice from scratch, both dev and test DBs | DONE | |
| Only Phase 1 schema (no future tables) | §28 | Confirmed: no Rack/Equipment/PDU/PowerNode/etc. tables | — | DONE | |
| Unit/integration/API/security test tiers | §29 | `tests/unit/`, `tests/integration/`, `tests/api/` | 59 tests total, all passing | DONE | |
| Critical DB invariant tests (identity, FK, authz, audit, outbox, idempotency, concurrency) | §30 | See individual rows above | All present | DONE | U-range logic explicitly not tested (future phase, no such table) |
| Domain/module boundaries preserved | §31 | Separate `domain/*` packages, no cross-module table writes | Code review | DONE | |
| Database ownership / no free-for-all model imports | §32 | Each module owns its own models; `db/models.py` is a registration point, not a shared-access shortcut | Code review | DONE | |
| Security baseline review | §33 | See `PHASE1_IMPLEMENTATION_REPORT.md` §Security | `tests/api/test_security.py` (8 tests) + manual review | DONE | Explicitly not a penetration test |
| Performance baseline (indexes, pooling, pagination, no N+1) | §34 | FK columns indexed, `Page`/`pagination_params`, connection pooling | Code review; no load test performed | DONE (baseline only) | Target-scale benchmarks are a later-phase gate (H8) |
| Migration/startup safety | §35 | Migrations never auto-run at app startup; documented rollback/backup in README | — | DONE | |
| README with full setup instructions | §36 | `README.md` | Manual: every documented command actually run in this session | DONE | |
| `PHASE1_IMPLEMENTATION.md` | §36 | This repository | — | DONE | |
| This traceability matrix | §37 | This file | — | DONE | |
