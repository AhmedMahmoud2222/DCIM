# PHASE2_TRACEABILITY_MATRIX.md

Maps each Phase 2 prompt requirement to its implementation and its test evidence.
"Test" cells name the actual test function(s); "DB-enforced" means proven at the
PostgreSQL level, not merely in application code.

## Domain model

| Requirement | Implementation | Test evidence |
|---|---|---|
| Rack as ManagedAsset subtype, shared PK | `app/domain/physical/models.py::Rack` | `tests/integration/test_phase2_placement_constraints.py::_make_rack` (helper used by 12 tests) |
| Equipment as ManagedAsset subtype, shared PK | `app/domain/physical/models.py::Equipment` | `_make_equipment` (helper used by 10 tests) |
| Rack/Equipment model + immutable revision | `app/domain/catalog/models.py` | `tests/api/test_racks.py::test_create_rack_with_nonexistent_model_revision_is_404` |
| RackPlacement (room + drawn position) | `app/domain/placement/models.py::RackPlacement` | `test_create_rack_with_initial_placement` |
| EquipmentPlacement (polymorphic placement_type) | `app/domain/placement/models.py::EquipmentPlacement` | `test_move_equipment_floor_standing_requires_no_rack`, `test_move_equipment_rack_mounted_requires_rack_u_range_and_side` |
| Temporal placement history, effective_from/to | Both placement tables | `test_a_historical_closed_placement_does_not_block_a_new_current_one` |
| DB-enforced no-contradictory-simultaneous-placements | `rack_placement_one_timeline_per_asset`, `equipment_placement_one_timeline_per_asset` exclusion constraints | `test_rack_cannot_have_two_simultaneous_current_placements`, `test_equipment_cannot_have_two_simultaneous_current_placements` (DB-enforced) |
| v1.3 `side IS NOT NULL` correction carried forward | CHECK `rack_mounted_requires_rack_u_range_and_side` | `test_rack_mounted_with_null_side_is_rejected_the_v1_3_f1_correction` (DB-enforced) |
| Do NOT reintroduce the NULL bypass | `occupies_front`/`occupies_rear` COALESCE-based generated columns | `test_floor_standing_with_null_side_and_null_rack_succeeds` (proves the legitimate NULL case still works, DB-enforced) |
| Rack elevation is a projection, never stored | `app/api/v1/racks.py::get_rack_elevation` computes live from `EquipmentPlacement` | `test_rack_elevation_reflects_mounted_equipment_sorted_by_u_position`, `test_rack_elevation_excludes_retired_equipment` |
| Floor/room spatial coordinates, canonical mm | `app/domain/spatial/models.py::SpatialObject.x_mm/y_mm/rotation_deg` | `test_sanitize_svg_clamps_out_of_range_coordinates_rather_than_propagating_them` (bounds), spatial API tests |
| FloorPlan model, revision + status lifecycle | `app/domain/spatial/models.py::FloorPlan` | `test_second_floor_plan_for_same_room_gets_next_revision`, `test_activating_a_floor_plan_supersedes_the_previously_active_one` |
| Import diagnostics/candidate distinct from authoritative state | `FloorPlanImportCandidate.status`, only `accept_import_candidate` creates a `SpatialObject` | `test_rejecting_a_candidate_never_creates_a_spatial_object`, `test_accepting_a_candidate_creates_an_authoritative_spatial_object` |

## Spatial validation

| Requirement | Implementation | Test evidence |
|---|---|---|
| Distinguish absolute DB rules from advisory warnings | `app/application/spatial_validation.py` module docstring + `ValidationWarning` dataclass for the advisory half | `test_rack_footprint_check_flags_out_of_bounds_footprint` (advisory), DB constraint tests (absolute) |
| U0/negative U reject | `validate_u_range_against_rack_capacity` | `test_u_range_rejects_u_start_below_one`, `test_u_range_rejects_negative_u_start` |
| U-range overflow / implausible height | same | `test_u_range_rejects_implausibly_large_height` |
| Same-side overlap reject (DB) | `no_front_overlap`/`no_rear_overlap` GiST exclusion | `test_same_side_same_u_range_overlap_is_rejected_by_the_db`, `test_same_side_overlap_in_same_rack_is_rejected` (API level) |
| NULL-side bypass blocked | CHECK + generated columns | `test_rack_mounted_with_null_side_is_rejected_the_v1_3_f1_correction` |
| NULL-rack bypass blocked | CHECK `rack_mounted_requires_rack_u_range_and_side` | `test_rack_mounted_with_null_rack_id_is_rejected` |
| Temporal overlap reject | Range-exclusion constraints | `test_rack_cannot_have_two_simultaneous_current_placements` |
| NaN/overflow coordinate reject | `svg_sanitizer._safe_float` | `test_sanitize_svg_treats_nan_and_infinity_coordinates_as_default` |

## Import security (adversarial matrix)

| Attack | Implementation | Test evidence |
|---|---|---|
| Malicious SVG (script) | Tag stripped, never executed | `test_sanitize_svg_strips_script_tag_and_does_not_recurse_into_it`, `test_malicious_svg_is_sanitized_not_rejected_outright` |
| Oversized SVG/raster | Two-tier cap (endpoint blanket + format-specific) | `test_upload_beyond_the_endpoints_blanket_size_cap_is_rejected_before_any_parsing`, `test_svg_over_the_svg_specific_cap_is_accepted_then_fails_async_with_a_reason`, `test_sanitize_svg_rejects_oversized_content`, `test_validate_raster_image_rejects_oversized_content` |
| Malformed SVG | Rejected as unparseable | `test_sanitize_svg_rejects_malformed_xml` |
| Embedded script / external refs | Stripped with warnings | `test_sanitize_svg_strips_event_handler_attributes`, `test_sanitize_svg_strips_external_href_references` |
| Entity expansion (XXE) | `defusedxml` DTD/entity/external forbidden | `test_sanitize_svg_rejects_dtd_declaration_xxe_attempt`, `test_xxe_attempt_is_rejected_outright` |
| Path traversal | Structurally impossible (in-memory only, filename never used for a path) | Design property; no on-disk write path exists to test against |
| Content/extension mismatch | Content-sniffed, extension ignored | `test_upload_content_sniffed_svg_is_accepted_regardless_of_filename`, `test_upload_unrecognized_content_is_rejected` |
| Pathological element count (DoS via parse cost) | Hard cap enforced mid-walk | `test_sanitize_svg_caps_element_count_without_hanging` |

## API

| Requirement | Implementation | Test evidence |
|---|---|---|
| Clean versioned APIs under /api/v1 | All Phase 2 routers registered in `app/api/v1/router.py` | Full API test suite exercises every route |
| Rack CRUD + placement + elevation | `app/api/v1/racks.py` | `tests/api/test_racks.py` (18 tests) |
| Equipment CRUD + placement | `app/api/v1/equipment.py` | `tests/api/test_equipment.py` (13 tests) |
| FloorPlan CRUD/upload/import/diagnostics/candidates | `app/api/v1/floor_plans.py` | `tests/api/test_floor_plans.py` (18 tests) |
| Spatial query endpoint | `app/api/v1/spatial.py` | `tests/api/test_spatial.py` (5 tests) |
| Idempotency reused, not reinvented | `app/application/idempotency` imported as-is | `test_duplicate_idempotency_key_for_rack_create_replays_the_same_result`, equipment equivalent |
| Optimistic concurrency on own-field edits | `app/application/concurrency` reused | `test_rack_update_requires_if_match`, `test_rack_update_with_stale_version_is_409` |
| Locked placement move transaction | `app/application/placement_service.py` | `test_rack_move_with_wrong_if_match_version_is_409`, `test_rack_move_concurrent_movers_only_one_wins_the_other_gets_409` (genuine 2-session race) |
| RBAC — global only, no site scoping | `app/application/rbac.py::DEFAULT_ROLE_PERMISSIONS` | `test_viewer_cannot_move_or_retire_a_rack`, `test_engineer_cannot_accept_or_reject_candidates` |

## Database design

| Requirement | Implementation | Test evidence |
|---|---|---|
| NULL semantics explicitly tested | COALESCE fix (§25 D1 of the implementation report) | `test_floor_standing_with_null_side_and_null_rack_succeeds` |
| Proper constraints/indexes for scale | Partial indexes on `(rack_id, effective_to)`/`(equipment_id, effective_to)`; GiST exclusion constraints double as indexes | Migration DDL review; no dedicated load test (disclosed, §24 of implementation report) |
| Migration tested clean-install/upgrade/downgrade | `alembic upgrade head` from empty, from `0003_correction`, and `downgrade`+re-`upgrade` | Manual validation pass, documented in implementation report §21 (all three passes green, zero duplicate rows after re-upgrade) |
| Do not edit already-applied migrations | `0004_phase2_physical_spatial_model` is new; `0003_correction`'s NEW-2 naming issue left untouched | `git log` — no modification to migrations 0001–0003 |

## Frontend

| Requirement | Implementation | Test evidence |
|---|---|---|
| Rack inventory + detail + elevation | `frontend/src/features/racks/{RacksPage,RackDetailPage,RackElevationView}.tsx` | Manual browser smoke test, screenshots `/tmp/smoke_04..08*.png` (session-local, not committed) |
| Equipment inventory + detail | `frontend/src/features/equipment/{EquipmentPage,EquipmentDetailPage}.tsx` | Manual browser smoke test, screenshots `/tmp/smoke_06..08*.png` |
| Floor plan list/upload/2D viewer/import diagnostics | `frontend/src/features/floor-plans/{FloorPlansPage,RoomFloorPlanPage,RoomSpatialCanvas}.tsx` | Manual browser smoke test, screenshots `/tmp/smoke_09..15*.png`; full upload→parse→accept→activate→render cycle exercised against a live Celery worker |
| No authoritative state in the canvas | `RoomSpatialCanvas` renders `RoomSpatialView` query results only, no local mutable position state | Code inspection — no `useState` holding position data in that component |
| TanStack Query for server state, no Redux | All Phase 2 features use `useQuery`/`useMutation` exclusively | `frontend/src/features/**/*.tsx` |
| Optimistic-concurrency conflict feedback | Move forms surface a 409 as a specific "someone else moved this" message | `RackDetailPage.tsx`, `EquipmentDetailPage.tsx` move-form error handling |

## Traceability to Phase 1 carry-forward items

| Item | Status | Where addressed |
|---|---|---|
| NEW-1 (idempotency stale-reclaim race, Medium) | **CARRY-FORWARD — NOT FIXED** (correction prompt explicitly excluded it from this pass) | Implementation report §27; PHASE2_CORRECTION_REPORT.md §29.3 |
| NEW-2 (constraint naming, Low/cosmetic) | **CARRY-FORWARD — NOT FIXED** (correction prompt explicitly excluded it from this pass) | Implementation report §27; PHASE2_CORRECTION_REPORT.md §29.3 |

## Independent red-team findings (PHASE2_INDEPENDENT_RED_TEAM_REPORT.md) — correction status

| Finding | Original defect | Remediation | Enforcement layer | Regression test | Validation evidence | Status |
|---|---|---|---|---|---|---|
| RT-1 (HIGH) | Unlocked SELECT-then-INSERT of the canonical "Imported" `SpatialLayer` in `accept_import_candidate` let concurrent accepts of different candidates each create their own layer; once duplicated, every subsequent accept for that floor plan hit `MultipleResultsFound` → permanent 500 | Migration `0005_correction_spatial_layer_race.py` adds a DB-enforced partial unique index (`uq_spatial_layer_one_imported_per_floor_plan`, `WHERE layer_type = 'imported'`), deduplicating any pre-existing violating rows first (oldest row canonical, `SpatialObject`s re-pointed, never deleted); `app/api/v1/floor_plans.py::_get_or_create_imported_layer` claims the row via `INSERT ... ON CONFLICT (floor_plan_id) WHERE layer_type = 'imported' DO NOTHING` (index inference, literal predicate — see below); both `accept_import_candidate` and `reject_import_candidate` additionally lock the candidate row (`SELECT ... FOR UPDATE`) to close the adjacent same-candidate race | Database (partial unique index + row lock) — not an application-level lock, holds across multiple Uvicorn workers/processes | `tests/api/test_floor_plans.py`: `test_rt1_sequential_acceptance_creates_exactly_one_imported_layer` (A), `test_rt1_20_concurrent_accepts_of_the_same_candidate_exactly_one_wins` (B, real 20-way concurrency), `test_rt1_20_concurrent_accepts_of_different_candidates_no_duplicate_layer_no_500` (C+D, real 20-way concurrency + post-race recovery), `test_rt1_direct_db_bypass_of_duplicate_imported_layer_is_rejected` (E, direct ORM bypass rejected by PostgreSQL `IntegrityError`) | Full suite (191 tests) passed 3× consecutively; migration validated fresh-install / downgrade / re-upgrade / dedup-of-pre-existing-duplicates (with `SpatialObject` re-pointing verified, zero data loss); mandatory §23 live-Uvicorn+Celery+PostgreSQL acceptance test run against 2 independently-created fresh datasets, later repeated 3× more (6 datasets total, same long-lived server process to specifically stress connection/plan-cache reuse) — all runs: 20/20 concurrent accepts succeeded, 0 HTTP 500s, exactly 1 canonical layer, 0 orphaned `SpatialObject`s, floor plan remained usable after the race (409 on re-reject, 200 on a fresh accept); manual real-browser validation (real login form, real UI navigation, real "Accept" button, then a 19-way concurrent race, then real client-side re-navigation) showed no raw 500 page and correct TanStack Query cache refresh | **CLOSED — DB-enforced, tested at real concurrency, revalidate independently** |
| RT-1 sub-finding (found during this correction's own live-server/browser validation, not in the original red-team report) | The literal fix above initially targeted the partial index via `index_where=(table.c.layer_type == "imported")` — a Python comparison that SQLAlchemy compiles as a **bound parameter** (`WHERE layer_type = $7`). This is syntactically valid and passed every pytest run and an initial live-server run, but PostgreSQL's extended query protocol re-plans a prepared statement *generically* after ~5 executions on the same connection, and a generic plan cannot statically verify a parameter's runtime value against a partial index's predicate — so, once a pooled connection had reused this exact statement enough times, it began intermittently throwing `there is no unique or exclusion constraint matching the ON CONFLICT specification` (a 500) for a large fraction of concurrent accepts, while fresh/lightly-used connections kept succeeding. First surfaced by the mandated real-browser validation, not by any pytest run (no single pytest test drives one physical connection through this statement anywhere near 5+ times) | `index_where=text("layer_type = 'imported'")` — a genuine SQL literal, not a bound parameter, so there is no runtime value left for a generic plan to fail to verify; confirmed via direct compiled-SQL inspection that this removes the parameter from the compiled statement entirely | Database (same partial index; only the arbiter-target expression's compilation changed) | Same tests as RT-1 above (they do not by themselves reproduce this connection-reuse-count-dependent failure at pytest's low per-connection reuse counts — it needed a long-lived server process handling many requests in a row to surface) | Same long-lived dcim_correction server re-used across 3 consecutive full acceptance-test runs (6 datasets, well past the ~5-execution generic-plan threshold on every pooled connection) — 0 HTTP 500s across all 6 | **CLOSED — see RT-1 note above; the live-server/browser validation step this correction added is precisely why this was caught rather than shipped** |
| RT-2 (MEDIUM) | `svg_sanitizer.py`/`PHASE2_IMPLEMENTATION_REPORT.md` §14 claimed upload processing was "entirely in-memory... nothing is ever written to disk" — false above 1MB (Starlette's `SpooledTemporaryFile(max_size=1MB)` spools to a real, immediately-unlinked OS temp file before the endpoint or sanitizer ever see the bytes) | **Option A (documentation correction)** chosen over Option B (re-engineering to force true in-memory-only handling for all sizes): the actual security property the original claim was protecting — immunity to path traversal — does not depend on avoiding disk at all, since the temp file's path is generated by Python's `tempfile` module from process-random state, never from the attacker-controlled filename; re-engineering to avoid a real, already-narrow spooling mechanism (which happens after `path_traversal` is already structurally impossible) would not have closed any actual gap, only preserved an inaccurate claim. Corrected the claim in `app/application/svg_sanitizer.py`'s docstring, `app/infrastructure/tasks/floorplan_import.py`'s docstring, `app/api/v1/floor_plans.py`'s inline comment, and `PHASE2_IMPLEMENTATION_REPORT.md` §14, to disclose the >1MB spool-to-disk behavior honestly and state the property that does hold | Documentation only — no code-level enforcement was ever missing; path-traversal immunity was already structurally enforced by `tempfile`'s own random-path generation, independent of this project's code | None new (this is a documentation fix, not a behavior change) | Independently re-verified empirically (not inferred from framework docs), via live `/proc/<pid>/fd` monitoring during real uploads at 0.5MB (control, stays in memory), 1.5MB, 5MB (the SVG cap), and 20MB (the raster cap, the project's actual max configured upload size) against a real running Uvicorn process — confirmed spooling begins above 1MB and occurs at every size at or above the project's real caps | **CLOSED — documentation corrected to match verified behavior; no functional change** |
| RT-3 (Low, N+1 query pattern in `list_racks`) | `list_racks` calls `get_current_rack_placement` once per rack in a Python loop rather than a single JOIN | Not fixed this pass — the correction prompt scoped RT-3 to "only fix if trivial and local to the code path already being modified by RT-1"; `list_racks`/`get_current_rack_placement` are unrelated to `accept_import_candidate`, and fixing it would be an unrelated performance refactor outside this correction's scope | N/A | N/A | N/A | **CARRY-FORWARD — NOT FIXED, non-blocking observation, unrelated to this correction's code paths** |
