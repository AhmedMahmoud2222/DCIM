# Phase 8 Gap Analysis — Scope Reconciliation

Baseline: `99fce6555d1d75152b09a61780c6a8e837395ee4` (Phase 3 independently closed,
`PHASE 3 CLOSED — VERIFIED`). This document precedes and governs the Phase 8
implementation work in this same task.

## 1. Mandatory First Step: Architecture-Phase vs. Repository-Phase Reconciliation

The architecture's formal phase sequence (`ARCHITECTURE_REVIEW.md` §47) is:

| # | Architecture phase | Scope |
|---|---|---|
| 1 | Foundation | Repo, Docker Compose, DB + Alembic, auth/RBAC skeleton, logging, CI |
| 2 | Location + Inventory | Organization→Room, `ManagedAsset`, Rack, Equipment |
| 3 | Rack Models | `RackModel`/`RackModelRevision`, dimensions |
| 4 | Equipment + Rack Elevation | `EquipmentModel`/Revision, `EquipmentPlacement` |
| 5 | Spatial | `FloorPlan`, `SpatialObject`, `RackPlacement` |
| 6 | Floor Plan Import | Importer abstraction, review queue, diagnostics |
| 7 | Power | `PowerNode`/`PowerConnection`/`PowerCapacity` |
| 8 | **Integrations + Collectors** | Adapter/driver/vendor-profile framework, ICMP/SNMP/REST, `Collector` identity |
| 9 | Telemetry | Metrics, readings, TimescaleDB evaluation |

The repository's own delivery history (migrations, commits, `PHASE*_IMPLEMENTATION_REPORT.md`
files) does **not** use one migration per architecture phase:

| Migration | Commit(s) | Architecture phases actually covered |
|---|---|---|
| `e9fd19228f19_phase1_initial_schema` + `0002` + `0003_correction` | `PHASE1_IMPLEMENTATION_REPORT.md` | Architecture Phase 1 (Foundation) only |
| `0004_phase2_physical_spatial_model` + `0005_correction` | `PHASE2_IMPLEMENTATION_REPORT.md` | Architecture Phases **2, 3, 4, 5, and 6** combined — `RackModel`/`EquipmentModel` catalogs, `Rack`/`Equipment` as `ManagedAsset` subtypes, `RackPlacement`/`EquipmentPlacement`, `FloorPlan`/`SpatialObject`, and the SVG floor-plan import pipeline all shipped together under the repository's own "Phase 2" label |
| `0006_phase3_power_topology_and_capacity` + `0007_correction` | `PHASE3_IMPLEMENTATION_REPORT.md`, `PHASE3_N1_CORRECTION_REPORT.md`, `PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md` | Architecture Phase **7** (Power) — the repository's "Phase 3" |
| `0008_phase8_integrations_and_collectors` (this task) | — | Architecture Phase **8** (Integrations + Collectors) |

**Conclusion: no reconciliation gap exists for Phase 8 itself.** Architecture Phase 8 has
never been implemented in this repository under any name — the repository's own phase
numbering only diverges from the architecture's numbering for architecture Phases 2–6
(compressed into repo "Phase 2"). There is no prior "repo Phase 4" or "repo Phase 5–7"
whose Equipment/Elevation/Spatial/Import scope this task might mistakenly re-implement:
that work is `RackPlacement`/`EquipmentPlacement`/`FloorPlan` etc., already delivered,
tested, and twice independently red-team-validated as part of the repository's Phase 2,
and is **not** re-touched by this task (verified by the file list in §6 below — Phase 8
adds only new domain modules and one additive migration; it does not modify
`app/domain/physical/models.py`, `app/domain/spatial/models.py`, or their migrations).

The one prior finding that could have looked like a Phase 8 dependency — architecture
§47a's Phase 7 "New prerequisite: `PowerConnection.version` ships with Phase 7" — was
already satisfied when Phase 3 (repo) shipped; it is unrelated to Phase 8 and is not
re-verified here beyond confirming (via `git diff`, §6) that no Phase 3 file changed
during this task.

## 2. What Already Exists (Reused, Not Rebuilt)

- **Identity/audit/outbox/idempotency/RBAC/concurrency/error-handling infrastructure**
  (`app/application/audit_service.py`, `app/application/outbox_service.py`,
  `app/application/idempotency.py`, `app/application/rbac.py`,
  `app/application/concurrency.py`, `app/core/errors.py`) — all generic since Phase 1,
  reused verbatim for every Phase 8 write path (collector registration, capability
  declaration, assignment, heartbeat, discovery ingestion, reconciliation decisions).
- **The temporal close-then-open assignment pattern** (`RackPlacement`/
  `EquipmentPlacement`/`PowerConnection`/`PowerCapacity`'s `effective_from`/
  `effective_to` + partial unique index) — reused for `CollectorAssignment` exactly as
  established, not a new mechanism invented for collectors.
- **Existing password/security module** (`app/core/security.py`) considered and
  rejected for the collector's own secret (Argon2 is one-way; §9's HMAC scheme needs
  the plaintext recoverable server-side — see `app/core/secrets.py`'s docstring for the
  full reasoning).
- **`app/api/v1/managed_assets.py`'s `ManagedAsset`** — reused as the target of
  reconciliation linking (`DiscoveredDevice.matched_managed_asset_id`); Phase 8 never
  defines a competing identity concept.

## 3. What Phase 8 Requires (Net New)

`Integration`, `Collector`, `CollectorCapability`, `CollectorAssignment`,
`CollectorHeartbeat`, `CollectorRequestNonce`, `DiscoveredDevice`, `ReconciliationDiff`
domain models; a protocol/driver abstraction (ICMP/SNMP/REST); a collector
machine-to-machine HMAC trust boundary distinct from user JWT; a discovery/
reconciliation boundary that structurally cannot let discovery write authoritative
inventory; WAN-buffered idempotent batch ingestion; RBAC permissions for
integration/collector/discovery; and a minimum operational UI. None of this existed
anywhere in the repository before this task (confirmed by repo-wide search — no prior
`Collector`, `Integration`, or `DiscoveredDevice` symbol existed).

## 4. What Was Newly Implemented (This Task)

See `PHASE8_IMPLEMENTATION_REPORT.md` for the full file list and `PHASE8_TRACEABILITY_MATRIX.md`
for requirement-by-requirement traceability.

## 5. Explicitly Out of Scope for This Task (Per Master Prompt §17/§18)

- Full `TelemetryReading` pipeline, time-series aggregation, TimescaleDB, alarm engine,
  notification engine, predictive analytics, AI, CFD, 3D twin — architecture Phase 9+.
  A clean contract is documented in `PHASE8_EDGE_COLLECTOR_CONTRACT.md` §4; ownership
  stays with the future Telemetry domain.
- Any modification to Phase 3 (Power) topology/capacity logic. No concrete Phase 8
  dependency was found requiring this (checked: Phase 8's driver/discovery layer never
  needs to read or write `PowerNode`/`PowerConnection`/`PowerCapacity`).
- Site-scoped RBAC enforcement (architecture §32a's "Option B: global authorization"
  remains in force for Phase 8's own new permissions — see §15 of
  `PHASE8_IMPLEMENTATION_REPORT.md` for how the data model still keeps this option open
  for a future decision without a migration).

## 6. Evidence That No Already-Delivered Functionality Was Re-Touched

`git diff --stat 99fce6555d1d75152b09a61780c6a8e837395ee4..HEAD` (see
`PHASE8_IMPLEMENTATION_REPORT.md` §"Files Changed") shows only new files under
`app/domain/integration/`, `app/application/collector_*.py`,
`app/application/discovery_service.py`, `app/application/drivers/`,
`app/api/v1/collectors.py`/`integrations.py`/`discovery.py`, one new migration, new
tests, and new frontend feature files — plus five pre-existing files extended
additively (`app/api/v1/router.py`, `app/application/rbac.py`, `app/core/config.py`,
`app/core/errors.py`, `backend/.env.example`, `backend/pyproject.toml`,
`backend/tests/conftest.py`, `frontend/src/app/App.tsx`,
`frontend/src/components/layout/AppShell.tsx`, `frontend/src/types/index.ts`). No file
under `app/domain/physical/`, `app/domain/spatial/`, `app/domain/power/`, or their
migrations (`0004`–`0007`) was modified.
