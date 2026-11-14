# Phase 3 Gap Analysis

Baseline: `066860a4effb1abaf36293fb140da9572f5ad6fa` (Phase 2 independently revalidated,
PASSED). This document precedes all Phase 3 implementation work.

## 1. What Already Exists

- **Identity**: `ManagedAsset` (shared-PK-inheritance identity root) already has
  `ASSET_TYPES = ("rack", "equipment", "pdu", "ups", "generator", "power_panel", "sensor",
  "cable")` — `pdu`/`ups`/`generator`/`power_panel` were anticipated in Phase 1 and require
  no change to `ManagedAsset` itself.
- **Placement**: `RackPlacement`/`EquipmentPlacement` (temporal, close-then-open,
  `SELECT...FOR UPDATE`) already generalized for "any placeable ManagedAsset."
- **Optimistic concurrency**: `app/application/concurrency.py`'s `If-Match`/`version`
  helper is generic and its own docstring already names `PowerConnection` as a future
  consumer ("§13a").
- **RBAC**: `app/application/rbac.py`'s `DEFAULT_ROLE_PERMISSIONS` + the migration-time
  idempotent seed function (`_seed_new_permissions` in migration 0004) is a reusable
  pattern, not Phase-2-specific.
- **Audit** (`write_audit_log`), **Outbox** (`write_outbox_event`), **Idempotency**
  (`get_or_claim`/`complete_claim`/`release_claim`), **pagination** (`Page`/`Pagination`),
  and **error handling** (`ApiError`/`ConflictError`/`NotFoundError`/`ForbiddenError`) are
  all generic, reusable as-is.
- **Architecture already specifies the exact Phase 3 schema**, written and reviewed before
  Phase 1 began: `ARCHITECTURE_REVIEW.md` §13 (`PowerNode`/`PowerConnection`), §13a
  (concurrency: `version`/`If-Match` on `PowerConnection`, canonical node-lock ordering on
  create, `FOR UPDATE` for disconnect races — explicitly deferred H4: self-loop/cycle
  prevention, now in scope per this phase's master prompt), §14 (`PowerCapacity`:
  `rated_capacity_kw`/`configured_capacity_kw`/`measured_load_kw`), and §4b (full
  `PDU`/`PDUOutlet`/`UPS`/`Generator`/`PowerPanel` column lists as `ManagedAsset`
  subtypes/sub-components).
- **Frontend**: React/TS/Vite/Tailwind/TanStack Query/Zustand, established feature-folder
  convention (`src/features/<domain>/`), `DashboardPage.tsx` is an explicit placeholder
  shell whose own text names "power/network topology" and "telemetry dashboards" as
  later-phase work not yet present — confirms Phase 3 is exactly the next slice, not a
  redesign.

## 2. What Phase 3 Requires

PowerNode/PowerConnection/PowerCapacity domain, graph traversal (upstream/downstream),
capacity roll-up with explicit units and redundancy-aware de-duplication, a capacity
exception engine, rack/equipment power relationships, a dashboard (site/capacity/rack/
power summaries with drill-down), and a topology visualization page — all built on the
existing architecture, not a new one.

## 3. What Can Be Reused

Everything in §1 without modification: `ManagedAsset`, `RackPlacement`/
`EquipmentPlacement` (unchanged — power nodes attach to existing assets, not a new
placement mechanism), RBAC/audit/outbox/idempotency/concurrency/pagination/error
infrastructure, and the frontend's existing query-client/auth/layout scaffolding.

## 4. What Must Be Extended

- `DEFAULT_ROLE_PERMISSIONS` (new codes: `power:read`, `power:manage`, `capacity:read`,
  `capacity:manage`, `dashboard:read` — additive, same idempotent seed mechanism).
- `RackOut`/rack detail API response and the rack detail frontend page (power feed/
  capacity summary added, no existing field removed).
- Equipment detail API/frontend (power connections added).

## 5. What Must Be Newly Implemented

Domain: `app/domain/power/models.py` (`PDU`, `PDUOutlet`, `UPS`, `Generator`,
`PowerPanel`, `PowerNode`, `PowerConnection`, `PowerCapacity`).
Application: `app/application/power_graph.py` (iterative traversal, cycle detection,
canonical-order node locking), `app/application/power_capacity.py` (unit-explicit
capacity/utilization/redundancy/exception derivation).
API: `app/api/v1/power.py` (PowerNode/PowerConnection CRUD, connect/disconnect, topology
traversal, capacity, rack/equipment power summaries, capacity exceptions),
`app/api/v1/dashboard.py` (summary + drill-down).
Migration `0006_phase3_power_topology_and_capacity.py`.
Frontend: `src/features/power/` (topology page, capacity views), dashboard rebuild, rack/
equipment detail enhancements.

## 6. Existing Architectural Risks (carried into Phase 3's design)

- H4 (self-loop/cycle prevention) was explicitly deferred at the architecture-review
  stage — Phase 3's master prompt puts it back in scope. Addressed here: DB `CHECK
  (source_node_id <> target_node_id)` for self-loops (a simple, reliable constraint);
  cycle prevention is **not** expressible as a single-row constraint (a cycle is a
  graph-wide property), so it is enforced by a transactional service that traverses
  upstream from the proposed target before allowing a new edge, under the canonical
  node-lock ordering §13a already specifies — consistent with the master prompt's own
  instruction ("for graph rules that cannot be represented by a simple constraint, use a
  transactional service with appropriate locking").
- "`PowerNode` creation discipline is a service-layer responsibility, not automatic"
  (ARCHITECTURE_REVIEW.md, risk 5) — addressed by having every power-asset creation
  endpoint create its `PowerNode` row in the same transaction as the asset itself,
  mirroring how `create_rack` opens `RackPlacement` in the same transaction as `Rack`.

## 7. Phase 1 Carry-Forward Findings

- **NEW-1** (Medium, idempotency stale-reclaim race — a claim reclaimed as "stale" while
  the original claimant is merely slow can let both complete the same row): Phase 3 reuses
  `app/application/idempotency.py` exactly as-is for every new mutating endpoint. It does
  not touch the reclaim logic itself. **Does not intersect Phase 3** — carried forward
  unchanged, not silently fixed.
- **NEW-2** (Low, constraint naming/migration downgrade issue): unrelated to power/
  capacity/dashboard code. **Does not intersect Phase 3.**

## 8. Phase 2 Carry-Forward Findings

- **NEW-1** (same finding as Phase 1's, tracked in both phases' reports): as above, not
  touched.
- **NEW-2** (Low): not touched.
- **RT-3** (Low/Observation, `list_racks` N+1): Phase 3 adds `list_power_nodes`/
  `list_power_connections`/dashboard aggregate queries. These are **new** query paths, not
  the existing `list_racks` code RT-3 concerns — RT-3 itself is not touched, but the new
  queries are written N+1-safe from the start (batched joins, not per-row queries) so
  Phase 3 does not add a second instance of the same class of problem. This is a design
  choice, not a fix to RT-3.
- **R2-1** (Low/Observation, `MAX_ELEMENTS` soft cap exceedable via wide `<text>`): lives
  entirely in `app/application/svg_sanitizer.py`. Phase 3 does not touch SVG import at
  all. **Does not intersect Phase 3** — carried forward unchanged per explicit instruction
  not to silently redesign the sanitizer.

## 9. Carry-Forward Intersection Summary

None of NEW-1, NEW-2, RT-3, or R2-1 are touched by Phase 3's diff. All four are carried
forward unchanged and are re-stated, not re-litigated, in the final implementation report.

## 10. Proposed Database Changes (migration 0006)

Additive only, no existing table/column/constraint altered or dropped:
- `pdu`, `ups`, `generator`, `power_panel` (ManagedAsset subtypes, per §4b).
- `power_node` (generic power-graph join target, per §13).
- `power_connection` (topology edges, `version`/`effective_from`/`effective_to`, per §13/
  §13a) with: FK on both ends, `CHECK (source_node_id <> target_node_id)` (self-loop),
  a partial unique index preventing a duplicate *active* identical edge
  (`source_node_id, target_node_id WHERE effective_to IS NULL`), and the two
  `IDX(source_node_id, effective_to)`/`IDX(target_node_id, effective_to)` indexes §13
  specifies.
- `power_capacity` (per §14) with a partial unique index for at most one *current*
  capacity record per node (`power_node_id WHERE effective_to IS NULL`), and `CHECK`
  constraints rejecting negative capacity/threshold values.
- `pdu_outlet` (`PK id FK→power_node UNIQUE`, per §4b — explicitly not a `ManagedAsset`).
- New permission rows via the existing idempotent seed mechanism.

## 11. Proposed APIs (all under `/api/v1`)

`POST/GET/PATCH /power-nodes`, `POST /power-nodes/{id}/retire`,
`POST/GET/PATCH /power-connections`, `POST /power-connections/{id}/disconnect`,
`GET /power-nodes/{id}/upstream`, `GET /power-nodes/{id}/downstream`,
`GET /power-nodes/{id}/capacity`, `PUT /power-nodes/{id}/capacity`,
`GET /racks/{id}/power-summary`, `GET /equipment/{id}/power-summary`,
`GET /capacity-exceptions`, `GET /dashboard/summary`, `GET /dashboard/exceptions`.

## 12. Proposed Frontend Changes

Rebuild `DashboardPage.tsx` from its placeholder into a real summary+drill-down page;
new `src/features/power/` (`PowerTopologyPage.tsx`, node/connection forms, capacity
panel); rack detail gains a power section; equipment detail gains a power-connections
section. No new frontend framework/state library introduced.

## 13. Proposed Tests

Unit (capacity math, unit handling, redundancy classification, graph traversal, cycle
detection), DB-constraint (self-loop, duplicate active edge, orphan FK, negative
capacity — bypassing the API, direct SQL), API (CRUD, auth, idempotency, `If-Match`
concurrency, pagination/filtering), concurrency (genuinely concurrent DB sessions for
connection creation and capacity edits), adversarial graph cases (a reduced, still
meaningful subset of the master prompt's Graph A–L: chain, branch, redundant A/B,
duplicate, self-loop, 2-node cycle, longer cycle, deep valid chain, disconnected node,
missing upstream, overloaded node — every one independently exercised).

## 14. Migration Strategy

One additive migration (`0006`), following 0004's own precedent exactly (SQLAlchemy
`op.create_table` for ordinary tables/columns/FKs/simple `CHECK`s, raw `op.execute` SQL
for the partial unique indexes that DDL doesn't reach as cleanly). Validated: fresh
install, upgrade from the current Phase 2 head, downgrade, re-upgrade.

## 15. Backward Compatibility

Every change is additive. No Phase 1/2 table, column, endpoint, or frontend route is
removed or renamed. Existing Phase 1/2 automated tests are re-run unmodified as the
regression gate (§39 of the master prompt).
