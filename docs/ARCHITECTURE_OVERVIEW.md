# Architecture overview — implemented source

**Current code snapshot:** PR #29 at `c2d60b34f0ccc546c809a1d3ff15c11befb79a26` (documentation work branches from this commit). This is a map of the present code, not a substitute for the detailed historical architecture specification or proof of production deployment.

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
  ├── Identity, global RBAC, audit + transactional outbox
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

Authorization is enforced in FastAPI dependencies/services, not merely by hiding frontend controls. Catalog authoring is Administrator-gated; the backend checks relevant read/manage/publish/retire permissions. The API's global authorization model and future site-scoped RBAC decision are documented historically in `ARCHITECTURE_REVIEW.md` §32a. Do not present site-scoped authorization as implemented without an implementation change and tests.

External device transport follows its configured target allowlists. SNMP v2c communities and test fixtures do not imply SNMPv3 or unrestricted production access. Backups, credentials, TLS/ingress, secret-manager selection, data retention and network segmentation require environment-specific deployment decisions.

## Architecture document provenance

`ARCHITECTURE_REVIEW.md` §§1–50 contain dated design decisions and historical Phase 0 text. §4d documents Phase 10A, and §51 describes merged Phase 10B/10C source at the pre-PR-29 baseline. Read this document and [PROJECT_STATUS.md](PROJECT_STATUS.md) for today's source implementation. Follow `PHASE10_INTEGRATION_REPORT.md` for the conditional upsert, structural BER fix and expanded CI. Those changes belong to the PR #29 branch until merged into `main`.

No line in this overview certifies production readiness, exhaustive security penetration testing or autonomous remediation.
