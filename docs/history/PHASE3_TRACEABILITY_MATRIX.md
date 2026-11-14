# Phase 3 Traceability Matrix

Requirement → Implementation → Test → Evidence. Requirement numbers refer to
the section numbering of the Phase 3 master prompt. This matrix covers new
Phase 3 work only; Phase 1/2 requirements are not re-listed.

| # | Requirement | Implementation | Test(s) | Evidence |
|---|---|---|---|---|
| 1 | Repository inspection before coding | N/A (process) | — | PHASE3_GAP_ANALYSIS.md |
| 2 | Gap analysis document | PHASE3_GAP_ANALYSIS.md | — | File present at repo root |
| 3 | PowerNode generic extensible model | `backend/app/domain/power/models.py::PowerNode` | `test_phase3_power_constraints.py` (CHECK constraint tests) | 12/12 passing |
| 4 | Distinguish asset identity vs power identity vs telemetry | `PowerNode.managed_asset_id` / `owning_asset_id` / neither (utility_intake) | `test_asset_reference_exclusive_constraint_rejects_neither` | `ck_power_node_asset_reference_exclusive` enforced at DB level |
| 5 | PowerConnection source→destination model | `PowerConnection` model | `test_power_graph.py`, `test_power.py` (Graph A–L) | 11+27 tests passing |
| 6 | Prevent self-loops (DB) | `ck_power_connection_no_self_loop` CHECK | `test_self_loop_rejected_by_constraint` (integration), `test_self_loop_rejected_by_cycle_check` (unit), Graph E (API) | Constraint name confirmed in IntegrityError |
| 7 | Prevent duplicate identical connections (DB) | `uq_power_connection_active_edge` partial unique index | `test_duplicate_active_edge_rejected_by_constraint`, Graph D (API) | 409 at API, IntegrityError at DB |
| 8 | Prevent illegal cycles (transactional service, not app-only) | `power_graph.assert_would_not_create_cycle` under canonical node lock | `test_two_node_cycle_rejected`, `test_long_cycle_rejected`, Graph F/G (API) | 422 `WouldCreateCycle` |
| 9 | Iterative, bounded, deterministic graph traversal | `power_graph._traverse` (BFS, batched per-level SQL) | `test_traversal_bounded_on_pathological_depth` | `GraphTraversalBounded` raised, mapped to 422 |
| 10 | Capacity model: rated/configured/allocated/available/utilization, explicit units | `power_capacity.CapacityFigures`, `get_capacity_figures` | `test_power_capacity.py` (16 tests) | All fields kW-denominated, documented in module docstring |
| 11 | No fabricated precision; unknown ≠ zero | `data_quality` field ("known"/"unknown"/"not_applicable") throughout | `test_effective_capacity_unknown_when_no_record`, `test_allocated_kw_unknown_when_branch_unresolved` | Explicit assertions on `data_quality` |
| 12 | No division by zero; overload not silently clamped | `get_capacity_figures` utilization branch | `test_zero_effective_capacity_never_divides_by_zero`, `test_overload_preserves_negative_available` | Negative `available_kw` asserted, no exception on zero |
| 13 | Rack power foundation (feeds, A/B, rack PDU) | `RackDetailPage.tsx` "Rack Power Summary" section, reuses `equipment_power_summary` per mounted item | Manual browser validation step 20 | Screenshot `07_rack_detail_with_power.png` |
| 14 | Equipment→PowerNode relationships, redundancy modeling | `power.py::create_equipment_feed`, `power_capacity.equipment_power_summary` | `test_power_capacity.py` redundancy tests, `test_power.py` equipment-feed tests | Classification: single_feed / dual_feed_healthy / degraded / no_power_modeled |
| 15 | Identify single-feed / missing dual-feed / shared-upstream-domain / overload / missing-upstream | `equipment_power_summary` redundancy classification + `derive_node_capacity_exceptions` | Graph K (missing upstream), Graph L (overload), redundancy unit tests | `test_power.py` API-level assertions |
| 16 | Capacity roll-up without double-counting A/B feeds | `compute_allocated_kw` (per-branch, never merges siblings) + `equipment_power_summary.effective_demand_kw` (max() for recognized A+B pair) | `test_allocated_kw_stops_at_own_capacity_record`, `test_effective_demand_kw_uses_max_for_ab_pair` | Explicit design note in module docstring and PHASE3_IMPLEMENTATION_REPORT.md §8 |
| 17 | Capacity exception engine (fixed condition codes) | `power_capacity.derive_node_capacity_exceptions` — CAPACITY_OVERLOAD / CAPACITY_NEAR_LIMIT / CAPACITY_UNKNOWN; `api/v1/dashboard.py` — POWER_PATH_MISSING / REDUNDANCY_DEGRADED | `test_power_capacity.py` exception tests, `test_power.py::test_capacity_exceptions_endpoint*` | 4 exception-derivation unit tests passing |
| 18 | Configurable thresholds, not hard-coded | `PowerCapacity.warning_threshold_pct` / `critical_threshold_pct` columns, defaults only used when node has none | `test_default_thresholds_apply_when_node_has_none` | Per-node override confirmed |
| 19 | Operational dashboard, API-derived, server-side filtered | `api/v1/dashboard.py::get_summary` (site/building/floor/room query params) | `test_dashboard_summary_returns_expected_shape` | Manual validation steps 2, 16, 18 |
| 20 | Dashboard drill-down to filtered inventory / exception detail | `DashboardPage.tsx` (`<Link>` wrapped stat cards, exception list with "Inspect →" link to `/power`) | Manual validation | Screenshots `01_dashboard_initial.png`, `05_dashboard_overload.png` |
| 21 | Power topology UI (practical 2D, not full graph editor) | `PowerTopologyPage.tsx` | Manual validation steps 9–13 | Screenshot `03_topology_wired.png` |
| 22 | Select node, see upstream/downstream/connected, capacity | `PowerTopologyPage.tsx` detail panel + upstream/downstream chain panels | Manual validation steps 12–13 | Upstream/downstream chains rendered and clickable |
| 23 | Rack/equipment detail enhancements | `RackDetailPage.tsx`, `EquipmentDetailPage.tsx` edits | Manual validation steps 19–20 | Screenshots `06_equipment_detail_with_power.png`, `07_rack_detail_with_power.png` |
| 24 | API under `/api/v1`, existing conventions (pagination, filtering, errors, idempotency, optimistic concurrency) | `api/v1/power.py`, `api/v1/dashboard.py` | `test_power.py` (27 tests) | Pagination on `/power/nodes`, `/power/connections`; If-Match on connection PATCH and capacity PUT |
| 25 | Authorization reused, no site-scoped RBAC | `DEFAULT_ROLE_PERMISSIONS` extension in `rbac.py` (`power:read`, `power:manage`, `capacity:read`, `capacity:manage`, `dashboard:read`) | `test_power.py` authorization tests (Viewer 403/200, unauthenticated 401, capacity:read vs capacity:manage) | 5 authorization-specific tests passing |
| 26 | Audit reused for power/capacity changes | `_create_power_asset`, connection create/patch/disconnect, capacity PUT all call existing audit helper | Audit rows created alongside domain rows in same transaction (verified via existing audit test pattern) | Present in `power.py` transaction blocks |
| 27 | Outbox reused | Same transactions as above emit outbox events via existing helper | — | Present in `power.py` transaction blocks |
| 28 | PostgreSQL 16, Alembic migration, upgrade/downgrade/re-upgrade validated | `migrations/versions/0006_phase3_power_topology_and_capacity.py` | Manual migration validation (fresh install, upgrade, downgrade, re-upgrade) | PHASE3_IMPLEMENTATION_REPORT.md §16 |
| 29 | Never silently drop Phase 1/2 data | Migration is fully additive (new tables only, no ALTER on existing tables) | — | Migration diff review |
| 30 | Concurrency: PowerConnection creation races | `lock_node_pair_in_canonical_order` (sorted-UUID lock ordering) | `test_concurrent_connection_creation_between_same_pair_only_one_wins` (genuine multi-session) | 5 concurrent attempts → 1×201, 4×409 |
| 31 | Concurrency: disconnect races | `SELECT ... FOR UPDATE` in `disconnect_power_connection` | `test_disconnect_twice_returns_409` | Second disconnect rejected |
| 32 | Concurrency: competing topology/capacity edits | If-Match / version column on `PowerConnection` and `PowerCapacity` | `test_concurrent_edit_of_same_connection_requires_if_match`, `test_capacity_edit_requires_if_match_once_a_record_exists` | Stale version rejected with 409/412 |
| 33 | Idempotency reused; NEW-1 only touched/tested if Phase 3 code path is touched | Not touched — Phase 3 introduces no new idempotency-key-bearing endpoints beyond existing infra reuse | — | Disclosed as untouched in PHASE3_GAP_ANALYSIS.md |
| 34 | Frontend: React/TS/Vite/Tailwind/TanStack Query/Zustand, no Redux, no duplicated server state | `features/power/api.ts` (TanStack Query hooks only), no new Zustand store added | `npx tsc --noEmit`, `npx eslint` clean | Build succeeds |
| 35 | Dashboard UX: dense, actionable, no decorative charts/fake real-time | `DashboardPage.tsx` — plain stat cards + exception list, no gauges/animations | Manual review | Screenshots |
| 36 | Data quality visible, never fabricated as healthy/zero | `data_quality` badges in `PowerTopologyPage.tsx` capacity grid | Manual validation | Screenshot `04_generator_overloaded.png` |
| 37 | Observability: structured logging for topology mutations, calc failures, invalid topology, expensive traversals | Exceptions (`WouldCreateCycle`, `GraphTraversalBounded`) carry structured fields (`root_id`, `limit_kind`) surfaced in 422 responses | — | `power.py` exception handlers |
| 38 | Performance at realistic scale (1,000 racks / 10,000 equipment / 5,000 power nodes / 10,000 connections) | Not reached at full scale — reduced-scale test performed instead (1,000 power nodes) | `perf_test.py` | PHASE3_IMPLEMENTATION_REPORT.md §14; disclosed as Known Limitation |
| 39 | Security: authorization/IDOR/malformed IDs/graph abuse/injection/mass assignment/invalid values/overflow/NaN | DB CHECK constraints on voltage/current/capacity/percentage; Pydantic validation on request bodies; bounded traversal against pathological graphs | `test_negative_voltage_rejected`, `test_negative_rated_capacity_rejected`, `test_invalid_percentage_threshold_rejected`, Graph I (pathological) | 12 DB-level constraint tests + traversal bound test |
| 40 | Unit tests: capacity, utilization, unit conversion, redundancy, traversal, cycle detection, exceptions, unknown-data semantics | `test_power_graph.py` (11), `test_power_capacity.py` (16) | — | 27 unit tests passing |
| 41 | DB tests bypassing API to prove invariants | `test_phase3_power_constraints.py` (12) | — | 12 tests passing |
| 42 | API tests: CRUD, authorization, idempotency, optimistic concurrency, filtering, pagination, errors | `test_power.py` (27) | — | 27 tests passing |
| 43 | Genuine concurrency tests (real concurrent DB sessions, not sequential simulation) | `per_request_client` pattern (dedicated engine/sessionmaker) | `test_concurrent_connection_creation_between_same_pair_only_one_wins`, `test_concurrent_edit_of_same_connection_requires_if_match` | Verified against `test_idempotency_concurrency.py` precedent |
| 44 | Adversarial Graph A–L test cases | Graphs A, B, D, E, F, G, J, K, L implemented; C (redundant A/B) and H (very deep) covered by unit tests instead; I (pathological) covered by bounded-traversal unit test | `test_power.py`, `test_power_graph.py` | Disclosed as reduced subset in Known Limitations |
| 45 | No telemetry, no alarm engine, no AI, no microservices | None introduced | — | Grep of diff confirms no SNMP/Modbus/Kafka/LLM/vector-DB references |
| 46 | Phase 2 regression protection — full suite run before completion | Full backend suite re-run after Phase 3 changes | pytest full suite | 269/269 passing (203 pre-existing + 66 new) |
| 47 | R2-1 carried forward unchanged (not silently redesigned) | SVG sanitizer untouched by Phase 3 | — | No diff in `floorplan_import` sanitizer code |
| 48 | Documentation: gap analysis, implementation report, traceability matrix, red-team scope | All four files present at repo root | — | This file + 3 others |
| 49 | Manual browser validation against real backend/DB (not solely automated tests) | `browser_validate.py` (20 steps, Playwright + Chromium) | — | 7 screenshots + console log "ALL BROWSER VALIDATION STEPS PASSED" |
| 50 | Red-team preparation document | PHASE3_IMPLEMENTATION_RED_TEAM_SCOPE.md | — | File present at repo root |

## Coverage summary

- New backend tests: 66 (11 unit graph + 16 unit capacity + 12 integration DB constraints + 27 API)
- Full backend suite: 269/269 passing, zero regressions
- Frontend: `tsc --noEmit` clean, `eslint` clean, `npm run build` succeeds
- Manual browser validation: 20/20 steps passed in one clean run
- Migration: fresh install, upgrade, downgrade, re-upgrade all validated
