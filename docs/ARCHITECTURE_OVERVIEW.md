# Architecture overview — implemented source

**Source baseline:** `main` at `4a9980bd22dc058850d3106dd329cb208076d37a` (2026-10-23). This is a map of the code at that commit, not a substitute for the detailed historical architecture specification or proof of production deployment. **Later commits can change any statement here; re-verify against the source and tests before relying on it** (see [Architecture document provenance](#architecture-document-provenance)).

## System structure

```text
React / TypeScript web UI
  ├── Dashboard, locations, racks, equipment and floor plans
  ├── Admin catalog designer + front/rear graphic marker editor
  ├── Equipment instantiation / cabling / rack elevation
  └── Telemetry overlays and power/network impact simulation
                      |
                      | /api/v1 (FastAPI; authenticated / RBAC)
                      v
Python modular-monolith application services
  ├── Identity, RBAC (global roles + permission-local site/rack scopes), audit + transactional outbox
  ├── Physical locations, placement, racks/equipment, power topology
  ├── Versioned catalog lifecycle and legacy-model bridge
  ├── Equipment instantiation: snapshot templates into deployed ports/inlets
  ├── Collector/integration/mapping, telemetry and latest-status cache
  └── Read-only bounded failure-impact graph traversal
           |                                  |
           v                                  v
   PostgreSQL 16 (record of truth)    Redis 7 / Celery background work
           ^
           |
Central collectors/drivers and separately packaged Edge Collector
(Edge scheduler + supported SNMP v2c acquisition / store-forward flow)
```

## Source-of-truth boundaries

- **PostgreSQL** holds managed assets, catalog revisions, placement and cabling graphs, mappings, latest-status cache, audit and transactional outbox. Schema evolution is an explicit Alembic deployment operation, not an API startup side effect.
- **Catalog templates are distinct from installed equipment.** Phase 10A revisions move through draft, published and retired lifecycle states. Phase 10B instantiation snapshots network port and power supply templates into equipment-owned instances; later catalog revisions do not rewrite previously instantiated topology.
- **Physical placement and power topology** use their own domain tables and constraints. Real assets and their relationships should be manipulated through application services that preserve the required identity, placement and graph invariants.
- **Telemetry has two distinct representations.** The earlier time-series ingestion/mapping path under `/telemetry/latest` uses series/event ordering, while Phase 10C's `TelemetryLatestStatus` holds one current row per `binding_id`. The Phase 10C atomic upsert only applies a strictly newer `sampled_at`; equal timestamps are first-writer-wins and older samples preserve the currently stored payload and arrival timestamp.
- **Impact simulation is read-only.** The bounded power/network graph traversal reports modeled loss and degraded redundancy; simulation output is not evidence that an actual outage has occurred.
- **Device integration is layered.** The central driver framework, vendor/device profiles, metric mappings, collector assignments and separate Edge Collector should not be collapsed into one abstraction. Consult `PHASE8_ARCHITECTURE_CLARIFICATION.md` for the historical contract and any knowingly deferred adapter work.
- **Audit retention is privileged.** The ordinary `dcim_app` login must not own the database. Privileged retention bootstrap executes separately from normal Alembic migrations.

## Server and UI entry points

- Backend composition: `backend/app/main.py`; API registration: `backend/app/api/v1/router.py`. Registered routers include authentication, users, alarms, locations, managed assets, catalog, catalog designer, racks, equipment, floor plans, spatial, power, dashboard, collectors, integrations, discovery, telemetry, impact and settings.
- Frontend composition: `frontend/src/app/App.tsx`; protected application shell: `frontend/src/components/layout/AppShell.tsx`. Implemented routes include `/admin/catalog/*`, `/equipment/instantiate`, `/racks/:rackId`, `/equipment/:equipmentId`, `/power` and operating-area pages.
- Readiness: `GET /api/v1/health/ready` checks PostgreSQL and Redis; liveness is `GET /api/v1/health/live` and intentionally does not depend on those services.
- Run `/api/v1/openapi.json` or the backend's `/docs` endpoint against the actual running version for exhaustive parameter, permission and response details. This overview does not invent endpoint guarantees not present in source.

## Security and operational constraints

Authorization is enforced in FastAPI dependencies/services, not merely by hiding frontend controls. Catalog authoring is Administrator-gated: it requires the relevant `catalog:*` permission **and** Administrator role membership (`require_catalog_administrator` in `backend/app/application/rbac.py`).

### Authorization model at the baseline SHA

Site/rack-scoped access is implemented, in `backend/app/application/access_control.py`. It was delivered by SEC-ARCH-01 ([#57](https://github.com/AhmedMahmoud2222/DCIM/issues/57), [PR #134](https://github.com/AhmedMahmoud2222/DCIM/pull/134), merged 2026-10-15) and SEC-ARCH-02 ([#163](https://github.com/AhmedMahmoud2222/DCIM/issues/163), [PR #170](https://github.com/AhmedMahmoud2222/DCIM/pull/170), merged 2026-10-19). The behavior is specified in [USER_GROUP_MANAGEMENT.md](USER_GROUP_MANAGEMENT.md); this section only summarizes it.

- **Sources of access.** `RoleAssignment` rows (`scope_type` is `global`, `site` or `organization`) and user groups. A group carries `allow`/`deny` permission grants and site grants (`rack_scope` `all` or `selected`, with explicit racks for `selected`).
- **Permission-local scopes.** Each permission code has its own effective scope. Scopes are united only between sources granting the **same** permission, and each group allow is bound to that group's own site/rack grants. `scope_for(code)` is authoritative for resource decisions; the aggregate `scope` is used only for directory visibility and scoped-role eligibility.
- **Global roles.** A global role makes only its own permissions global. It does not widen unrelated group grants, and it does not activate an unrelated unfiltered permission for a scoped user.
- **Explicit deny.** A user-wide group `deny` removes the permission even when a global role grants it. Codes match exactly; `manage` does not imply `read`.
- **Scoped role assignments** are honoured only when the holder's whole reachable site scope lies inside the assignment's scope; otherwise they contribute neither permissions nor role name. Organization-scoped roles never satisfy role-name checks such as Administrator.
- **Site-aware permissions (exactly ten, pinned by `SCOPE_AWARE_PERMISSIONS`):** `organization:read`, `location:read`, `rack:read`, `rack:manage`, `rack:place`, `equipment:read`, `cable:read`, `cable:manage`, `cooling:read`, `cooling:manage`. Separately, `user:*` and `group:*` administration is exempt from the restriction (`UNSCOPED_ADMIN_RESOURCES`, via `is_scope_independent`): restricted administrators keep these permissions, subject to directory masking (out-of-scope users and groups are 404) and authority-containment checks (refused with 403 when the target is not strictly outranked); see [USER_GROUP_MANAGEMENT.md](USER_GROUP_MANAGEMENT.md).
- **Fail closed.** Any permission code that is neither one of the ten site-aware codes nor part of the `user:*`/`group:*` administration exception, when held by a restricted (non-global) user, is reported inactive and the route returns 403. Out-of-scope resource IDs on site-aware routes return 404. Adding a code to `SCOPE_AWARE_PERMISSIONS` requires router-level filtering first; `backend/tests/api/test_sec_arch_01_deny_all_routers.py` pins the exact set so a change is deliberate.
- **Rack and equipment reach follows current placement**, not a stored site column: a rack moved to another site does not keep a stale grant.

**What this section does not claim.** It does not claim that every endpoint is site-scoped (most data families are deliberately unreachable for restricted users), that endpoint coverage has been exhaustively audited, or that cross-site behavior has been independently penetration-tested. There is no tenant entity. A route-by-route coverage audit with database-backed hostile tests is a proposed, not yet authorized, follow-up tracked in [#195](https://github.com/AhmedMahmoud2222/DCIM/issues/195) (P2).

External device transport follows its configured target allowlists. SNMP v2c communities and test fixtures do not imply SNMPv3 or unrestricted production access. Backups, credentials, TLS/ingress, secret-manager selection, data retention and network segmentation require environment-specific deployment decisions.

## Architecture document provenance

`ARCHITECTURE_REVIEW.md` §§1–50 contain dated design decisions and historical Phase 0 text, including the original global-only RBAC decision (§32a), which the SEC-ARCH-01/02 work above has since superseded in source. §4d documents Phase 10A, and §51 describes merged Phase 10B/10C source at the pre-PR-29 baseline. The post-audit remediation (conditional telemetry upsert, structural SNMP BER validation, expanded CI) was merged to `main` through [PR #29](https://github.com/AhmedMahmoud2222/DCIM/pull/29) on 2026-09-26; `PHASE10_INTEGRATION_REPORT.md` remains its historical record.

This overview was re-baselined on 2026-10-23 against `main` `4a9980bd22dc058850d3106dd329cb208076d37a`. It is a dated source map and must be re-verified after later code changes. Older phase reports are historical and are intentionally unchanged. Read this document and [PROJECT_STATUS.md](PROJECT_STATUS.md) for the status at that baseline.

No line in this overview certifies production readiness, exhaustive security penetration testing or autonomous remediation.
