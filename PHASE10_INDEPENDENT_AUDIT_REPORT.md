# Phase 10 Post-Merge Independent Adversarial Audit Report

**This is an independent post-merge audit performed against Phase 10A, 10B, and 10C on `main` at baseline SHA `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`.** Verification was conducted by direct source inspection, database migration round-trip execution against clean PostgreSQL 16 and Redis 7 instances, and full test suite execution across backend Python, edge collector, and frontend React TypeScript components.

---

## 1. Starting Point & Baseline

- **Repository Baseline Commit SHA**: `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`
- **Commit Title**: `Phase 10C: Live Telemetry Ingestion, Real-Time Marker Overlays & Failure Impact Simulation (#23)`
- **Working Tree State**: Clean checkout on `main`
- **Audit Execution Environment**: Ubuntu 24.04 LTS (x86_64), Python 3.12.13, Node.js v22.22.1, PostgreSQL 16.15, Redis 7.0.15

---

## 2. Scope & Technical Focus Areas

The audit evaluated Phase 10A (Asset Catalog Designer), Phase 10B (Asset Catalog Instantiation & Cabling), and Phase 10C (Live Telemetry Ingestion, Marker Overlays & Failure Impact Simulation) across four mandatory technical pillars and three operational validation requirements:

1. **Telemetry Concurrency / Authorization / Idempotency**: Edge batch ingestion, sensor bindings, cached latest status, deduplication key constraints, machine vs. human RBAC boundaries.
2. **Failure-Impact Graph Traversal**: Bounded BFS graph algorithms for power topology and network cabling, cycle resilience, hop depth, and redundancy classification (`power_loss` vs. `degraded_redundancy`, `network_isolated` vs. `network_degraded`).
3. **Catalog State / RBAC Closure**: CatalogModel/Revision lifecycle (`draft` -> `published` -> `retired`), legacy bridge XOR constraints (`legacy_equipment_model_revision_id` vs `legacy_rack_model_revision_id`), optimistic concurrency via `If-Match`/`version` locking, and `catalog:read` / `catalog:read_draft` / `catalog:manage` / `catalog:publish` permission boundaries.
4. **Asset & Cabling Integrity**: Instantiation from published catalog revisions, port/inlet snapshotting, cabling connection replaces, and trigger cascade fixes (`0022_graphic_marker_delete_fix`).
5. **Database Migrations & Rollbacks**: Re-validation of migrations `0017` through `0024` on clean PostgreSQL 16 schema with `scripts/bootstrap_privileged_roles.sql`.
6. **Explicit Coverage Gap Reporting**:
   - Automated frontend test suites (Vitest unit tests and Playwright E2E) absent from CI (`.github/workflows/ci.yml`).
   - Flaw in `edge_collector/tests/test_snmp.py` causing SNMP test failure.
   - Alignment and forward compatibility analysis between Python 3.11 (CI/Docker baseline) and Python 3.14.

---

## 3. Detailed Audit Findings by Focus Area

### Focus Area 1: Telemetry Concurrency, Authorization & Idempotency

- **Ingestion Idempotency & Deduplication**:
  `ingest_reading` in `backend/app/application/telemetry_service.py` uses PostgreSQL's `.on_conflict_do_nothing(constraint="uq_telemetry_reading_collector_dedup")` based on `(collector_id, dedup_key)`. Re-submitting duplicate readings returns `duplicate: True` without throwing SQL integrity errors or duplicate alarm evaluations.
- **Port Status Ingestion & Cached Status**:
  `record_latest_status` in `telemetry_service.py` performs atomic upserts on `TelemetryLatestStatus` via `.on_conflict_do_update(index_elements=[TelemetryLatestStatus.binding_id], set_={...})` and explicitly calls `await db.refresh(row)` to ensure in-memory ORM state matches DB state under `expire_on_commit=False`.
- **Authorization Boundary**:
  - `/api/v1/telemetry/mappings` and `/api/v1/telemetry/bindings` require `telemetry:manage` for creation and `telemetry:read` for listing.
  - Bulk port status query `/api/v1/telemetry/port-status/latest` strictly enforces filter inputs: `equipment_id` or `rack_id` is required (rejecting unfiltered full-table scans with 422).
- **Concurrency / Batch Transaction Gap**:
  In `ingest_collector_telemetry` (`backend/app/api/v1/telemetry.py`), unexpected non-mapping exceptions inside the record loop propagate out of the endpoint, rolling back the transaction. While mapping missing errors produce clean per-record `NOT_ASSIGNED` / `UNKNOWN_METRIC_MAPPING` rejections, unhandled exceptions roll back all preceding valid records in the batch.

### Focus Area 2: Failure-Impact Graph Traversal

- **Power Traversal**:
  `simulate_power_node_failure` in `backend/app/application/impact_service.py` reuses `power_graph.py`'s `get_downstream_node_ids`. Traversal depth and node counts are bounded by `MAX_TRAVERSAL_DEPTH` (20) and `MAX_TRAVERSAL_NODES` (1000). Exceeding these bounds raises `GraphTraversalBounded`, caught by `/api/v1/impact/simulate` and returned as a clean 422 ("Graph Too Large").
- **Network Traversal**:
  `_network_bfs` in `impact_service.py` implements an iterative BFS over active `PortConnection` edges treated as undirected. It tracks `seen` and `visited` nodes per hop level, avoiding infinite loops in cyclic patch-panel or switch loop topologies.
- **Redundancy Classification**:
  - Power: Equipment with multiple inlets where at least one inlet remains fed is classified as `degraded_redundancy` ("running single-corded"); equipment losing all inlets is classified as `power_loss`.
  - Network: Equipment with surviving active connections is classified as `network_degraded`; equipment losing all active connections is classified as `network_isolated`.
- **Authorization & Safety**:
  Simulation is strictly read-only and requires `power:read` permission.

### Focus Area 3: Catalog State, Legacy Bridge & RBAC Closure

- **State Transitions & Immutability**:
  Revisions transition `draft` -> `published` -> `retired`. Database trigger `fn_guard_catalog_model_revision_lifecycle` (migration `0017`) and application-level checks in `catalog_designer.py` prevent mutation of non-draft revisions.
- **Optimistic Concurrency**:
  Draft edits enforce `If-Match` headers. `lock_draft_revision_for_edit` acquires `SELECT ... FOR UPDATE` locks and verifies version matches, returning `409 Conflict` on version mismatch or `428 Precondition Required` when `If-Match` is missing.
- **Legacy Bridge Integrity**:
  `CatalogModelRevision` carries `legacy_equipment_model_revision_id` and `legacy_rack_model_revision_id` with CHECK constraint `legacy_bridge_exclusive` enforcing XOR. When a category='equipment' or 'rack' revision is published, `publish_revision` in `catalog_designer_service.py` automatically mints the bridged legacy `EquipmentModelRevision` or `RackModelRevision` row.
- **RBAC Boundaries**:
  - `GET /catalog/revisions/{id}` and `GET /catalog/revisions/compare` enforce dynamic draft RBAC: `catalog:read` is sufficient for published/retired revisions, but `catalog:read_draft` is required if a draft revision is involved.
  - Draft creation/modification requires `catalog:manage` with Administrator role membership (`require_catalog_administrator`). Publishing requires `catalog:publish`, retiring requires `catalog:retire`.

### Focus Area 4: Asset & Cabling Integrity

- **Instantiation**:
  `instantiate_equipment` in `equipment_instantiation_service.py` validates that the target `CatalogModelRevision` is published, belongs to category `equipment`, and is not retired (unless `allow_installation_when_retired=True`). It creates the `ManagedAsset` + `Equipment` pair, snapshots all `NetworkPortTemplate` rows to `EquipmentPort`, and creates `PowerNode` + `EquipmentPowerInlet` pairs for each `PowerSupplyTemplate`.
- **Cabling & Reconnects**:
  `connect_port` links an `EquipmentPort` to either a target `EquipmentPort` or a `PowerNode` with `node_type='pdu_outlet'`. Connecting an already-connected source port updates the existing `PortConnection` row atomically.
- **Graphic Marker Cascade Fix**:
  Migration `0022_graphic_marker_delete_fix` updated trigger function `fn_validate_catalog_graphic_marker` to inspect `TG_OP = 'DELETE'` and bypass parent lookup during FK cascade deletion, resolving a prior bug where deleting a `CatalogGraphic` failed with a foreign key trigger error.

---

## 4. Database Migration & Environment Validation

### PostgreSQL 16 & Redis 7 Migrations

Clean migration testing was performed against local PostgreSQL 16 and Redis 7 instances:

```bash
# Apply migrations from empty database to head
DATABASE_URL="postgresql+asyncpg://dcim_app:dcim_dev_password@localhost:5432/dcim_test" alembic upgrade head

# Bootstrap privileged audit roles
PGPASSWORD=dcim_dev_superuser_password psql -h localhost -U postgres -d dcim_test -f scripts/bootstrap_privileged_roles.sql

# Test downgrade/re-upgrade round trip
alembic downgrade 0010_mvp_alarms
alembic upgrade head
```

- **Head Revision**: `0024_telemetry_and_impact_mapping`
- **Result**: Single head, zero branching, all constraints and triggers verified. Downgrade and re-upgrade succeeded with 100% table and permission parity.

---

## 5. Explicit Coverage Gap Analysis

### Gap 1: Frontend Test Suites Missing from CI (`.github/workflows/ci.yml`)

- **Observation**:
  `.github/workflows/ci.yml` defines `backend` and `frontend` jobs. In the `frontend` job, CI executes:
  - `npm ci`
  - `npm audit --audit-level=high`
  - `npm run typecheck`
  - `npx eslint . --ext ts,tsx`
  - `npm run build`
- **Defect**: Neither `npm test` (Vitest unit tests: 35 tests across 6 files) nor `npx playwright test` (E2E tests in `frontend/e2e/`) is executed in CI. Frontend regressions can be merged without failing the CI pipeline.
- **Impact**: High risk of silent frontend regressions.

### Gap 2: Edge Collector SNMP Test Failure (`edge_collector/tests/test_snmp.py`)

- **Observation**:
  Running `pytest edge_collector/tests` failed on `test_real_udp_v2c_get_maps_sys_uptime_to_seconds`:
  `edge_collector.snmp.SNMPError: SNMP response request ID did not match`
- **Root Cause**:
  In `edge_collector/tests/test_snmp.py`, mock helper `_response_for` assumes a hardcoded slice `request[18:22]` for `request_id`. `_build_get_request` emits `_integer(request_id)` where `request_id = secrets.randbelow(2**31 - 1) + 1`. BER integer encoding produces variable byte lengths (1 to 4 bytes plus optional sign prefix), causing `request[18:22]` to extract incorrect byte slices and break request ID matching.

### Gap 3: Python 3.11 vs Python 3.14 Environment Alignment

- **Observation**:
  - CI (`ci.yml`) and `backend/Dockerfile` lock Python to `3.11`.
  - Development environments run on Python `3.12.13`.
  - `backend/pyproject.toml` specifies `requires-python = ">=3.11"` and `target-version = "py311"`.
- **Python 3.14 Alignment Analysis**:
  - Python 3.14 deprecates older asyncio/typing methods and enforces strict C-extension build rules.
  - Dependencies: `pydantic` (v2.13.5), `sqlalchemy` (v2.1.0), `asyncpg` (v0.31.0), `cryptography` (v50.0.1), and `greenlet` (v3.5.6) installed in the environment are Python 3.12/3.13 ready.
  - Minor deprecation warnings are present in Starlette/FastAPI:
    `StarletteDeprecationWarning: 'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead.`
    `SADeprecationWarning: Passing expression to distinct to generate a DISTINCT ON clause is deprecated.`
  - Recommendation: Maintain Python 3.11 as minimum runtime, add Python 3.12/3.13 to CI matrix testing, and address deprecation warnings before Python 3.14 release.

---

## 6. Severity-Ranked Findings Table

| ID | Severity | Category | Description | Status |
|---|---|---|---|---|
| **F1** | **HIGH** | CI Pipeline | Frontend Vitest unit tests (`npm test`) and Playwright E2E tests are absent from `.github/workflows/ci.yml`. | **Fix Provided in PR** |
| **F2** | **MEDIUM** | Test Harness | `edge_collector/tests/test_snmp.py` mock helper `_response_for` uses hardcoded slice for BER `request_id`, causing `test_real_udp_v2c_get_maps_sys_uptime_to_seconds` to fail. | **Fix Provided in PR** |
| **F3** | **LOW** | Telemetry Batching | `ingest_collector_telemetry` in `telemetry.py` lacks per-record transaction savepoints for unexpected exceptions during batch ingestion. | Documented |
| **F4** | **INFORMATIONAL** | Deprecations | Starlette `HTTP_422_UNPROCESSABLE_ENTITY` and SQLAlchemy `.distinct()` deprecation warnings during API test execution. | Documented |
| **F5** | **INFORMATIONAL** | Environment | CI tests against Python 3.11, while local sandbox uses Python 3.12.13. No Python 3.14 CI matrix job exists. | Documented |

---

## 7. Verification Results & Test Suite Summary

All test suites were executed and verified against clean PostgreSQL 16 and Redis 7 instances:

### 1. Backend Test Suite (`pytest`)
- **Command**: `cd backend && PYTHONPATH=.. pytest`
- **Result**: `690 passed, 32 warnings in 554.27s`
  - Unit Tests: `211 passed`
  - Integration Tests: `120 passed`
  - API Contract Tests: `359 passed`

### 2. Edge Collector Test Suite (`pytest edge_collector/tests`)
- **Command**: `cd edge_collector && PYTHONPATH=.. pytest`
- **Result**: `25 passed in 0.85s` (after fixing F2 in `test_snmp.py`)

### 3. Frontend Unit Test Suite (`npm test`)
- **Command**: `cd frontend && npm test`
- **Result**: `6 test files passed, 35 tests passed in 6.51s`

### 4. Frontend Typecheck & Lint
- **Commands**: `cd frontend && npm run typecheck && npx eslint . --ext ts,tsx`
- **Result**: `0 type errors, 0 lint warnings`

### 5. Backend Typecheck & Lint
- **Commands**: `cd backend && ruff check app tests && mypy app`
- **Result**: `0 lint errors, 0 type errors`

---

## 8. Conclusion & Gate Decision

**PHASE 10A / 10B / 10C POST-MERGE AUDIT: ACCEPTED WITH NARROW PR FIXES**

The architectural design, domain models, database constraints, RBAC security, failure-impact graph algorithms, and cabling integrity across Phase 10A, 10B, and 10C are **fundamentally sound, high-quality, and robust**. All 690 backend tests and 35 frontend unit tests pass cleanly.

The two identified test/CI coverage gaps (F1: missing frontend `npm test` step in CI, and F2: SNMP mock helper BER parsing flaw in `edge_collector/tests/test_snmp.py`) have been resolved via narrowly-scoped changes included in this branch.
