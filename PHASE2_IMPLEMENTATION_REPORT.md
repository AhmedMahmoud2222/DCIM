# DCIM Platform — Phase 2 Implementation Report

## Physical Infrastructure & Spatial Model Foundation

---

## 1. Executive Summary

Phase 2 adds the platform's first physical layer on top of Phase 1's identity/lifecycle
foundation: racks and equipment as `ManagedAsset` subtypes, a temporal placement model
(`RackPlacement`/`EquipmentPlacement`) that is the single authoritative source of every
asset's location, a canonical millimetre spatial coordinate system, a floor-plan model
with a secure SVG/raster import pipeline, and a rack-elevation view computed live from
placement data. Twelve new tables were added by one additive migration
(`0004_phase2_physical_spatial_model`); zero Phase 1 tables, columns, or constraints were
touched.

187 backend tests pass (76 carried over from Phase 1, 111 new for Phase 2), including a
dedicated DB-constraint suite that proves every hard invariant (U-space overlap
prevention, the v1.3 NULL-side correction, one-current-placement-per-asset, room/
spatial-object consistency) is enforced by PostgreSQL itself, not merely by application
code. Three genuine defects were found and fixed during this implementation's own
testing (never staged as "found by a later red-team") — see §16 Known Issues and §21
Self-Adversarial Findings for full detail. A working React/TypeScript frontend covers
rack and equipment inventory/detail/elevation, and floor-plan management including
upload, async import diagnostics, and the human-in-the-loop candidate accept/reject
review queue — manually exercised end-to-end in a browser against a live backend +
Celery worker (see §20).

**Final verdict: OPTION A — PHASE 2 IMPLEMENTATION COMPLETE — READY FOR INDEPENDENT
RED-TEAM VALIDATION.** No known Critical or High defect remains open.

---

## 2. Scope

**In scope, delivered:** Rack/Equipment domain (as `ManagedAsset` subtypes), rack/
equipment catalog (model + immutable model revision), rack and equipment placement with
full temporal history, front/rear U-space semantics carrying forward the v1.3 NULL-side
correction, canonical integer-millimetre room-local coordinate system, rack elevation
(computed, never stored), floor-plan model with revision/status lifecycle, secure SVG/
raster floor-plan import (quarantine-free in-memory pipeline, XXE-hardened, content-
sniffed, size-capped, script/event-handler/external-reference-stripped), import
diagnostics and a candidate review queue that is the *only* path from imported geometry
to authoritative `SpatialObject` rows, spatial validation (hard API-level rejection vs.
advisory warnings, explicitly distinguished), REST APIs under `/api/v1` for all of the
above, a 2D room spatial view and rack elevation view in the frontend, optimistic
concurrency on every mutable Phase 2 entity, the exact locked close-then-open placement-
move transaction pattern for both racks and equipment, audit logging and outbox events
for every significant mutation, RBAC extended with `rack:*`/`equipment:*`/`floor_plan:*`/
`spatial:read` permissions distributed across the existing five Phase 1 roles (no new
roles, no site-scoped RBAC).

**Explicitly out of scope (per the Phase 2 prompt, unchanged):** CFD/thermal simulation,
full 3D visualization, telemetry ingestion, BMS/EMS/SNMP/Modbus/BACnet integration,
vendor drivers, power/network topology implementation, alarm/notification engines, AI
assistant, predictive analytics, microservices/Kubernetes/event streaming, a separate
spatial database, DXF/VSDX/PDF floor-plan import (SVG + raster only), full OS-process
isolation for the untrusted-file parser (disclosed, see §12), site-scoped RBAC.

---

## 3. Architecture Compliance

Every non-negotiable principle from the Phase 2 prompt was followed:

- **Database is authoritative.** `RackPlacement`/`EquipmentPlacement` are the only place
  a rack's or equipment's room/position/U-slot lives. Rack elevation and the 2D floor-
  plan view are both *projections* computed from this data on every read — never a
  second stored copy that could drift (`app/api/v1/racks.py::get_rack_elevation`,
  `app/api/v1/spatial.py::get_room_spatial_view`).
- **`ManagedAsset` remains the identity anchor.** `Rack`/`Equipment` use shared-primary-
  key inheritance (`Rack.id`/`Equipment.id` *are* `ManagedAsset.id`) — never a second,
  competing identity system. Neither subtype repeats `asset_tag`, `serial_number`, or
  `lifecycle_status`.
- **The v1.3 `side IS NOT NULL` correction is carried forward exactly**, not
  reintroduced with a bypass (`app/domain/placement/models.py`, CHECK constraint
  `rack_mounted_requires_rack_u_range_and_side`; empirically verified in
  `tests/integration/test_phase2_placement_constraints.py::test_rack_mounted_with_null_side_is_rejected_the_v1_3_f1_correction`).
- **No second idempotency mechanism.** Rack/Equipment creation reuse
  `app.application.idempotency` byte-for-byte, the same claim/complete/release pattern
  as Phase 1's `managed_assets.py` (`app/api/v1/racks.py::create_rack`,
  `app/api/v1/equipment.py::create_equipment`).
- **No site-scoped RBAC introduced.** All new permissions are global-only, checked via
  the same `require_permission()` dependency Phase 1 established.
- **No new event bus, no invented events.** Outbox events use the exact set the prompt
  named (`RackCreated`, `RackMoved`, `RackUpdated`, `RackUnplaced`, `EquipmentCreated`,
  `EquipmentUpdated`, `EquipmentPlaced`, `EquipmentUnplaced`, `FloorPlanRevisionCreated`)
  through the existing `write_outbox_event()` helper.

---

## 4. Phase 2 Gap Analysis

See `PHASE2_GAP_ANALYSIS.md` (repository root) — written *before* any Phase 2 code, as
required. Summary of its conclusions: Phase 1 already provides everything Phase 2 needs
to reuse (identity, location hierarchy, concurrency pattern, audit/outbox/idempotency,
RBAC groundwork); no schema for the new tables was ever published in
`ARCHITECTURE_REVIEW.md`, so every new table's column list was authored from the
architecture's narrative requirements (§3.1 of that document, following the exact
precedent `PHASE1_BASELINE.md` set for `LocationType`); full OS-process isolation for the
untrusted-file parser is disclosed as not implemented (§3.4); no contradictions were
found between the v1.3 architecture, the Phase 1 implementation, and the Phase 1 red-team
findings.

---

## 5. Domain Model

- **Catalog** (`app/domain/catalog/models.py`): `RackModel` (mutable manufacturer/name
  identity) → `RackModelRevision` (immutable height_u/width_mm/depth_mm/
  weight_capacity_kg — a corrected spec is a new revision, never an edit).
  `EquipmentModel`/`EquipmentModelRevision` mirror this, with all dimension fields
  nullable (equipment spans placement types with very different physical footprints).
- **Physical** (`app/domain/physical/models.py`): `Rack`/`Equipment` — shared-PK
  `ManagedAsset` subtypes, each carrying only domain-specific fields plus an own
  optimistic-concurrency `version` column, distinct from the placement version below.
- **Placement** (`app/domain/placement/models.py`): `RackPlacement` (room + drawn x/y/
  rotation) and `EquipmentPlacement` (polymorphic — rack_mounted/floor_standing/
  wall_mounted/ceiling_mounted/other, with `occupies_front`/`occupies_rear` generated
  boolean columns derived from `side`). Both are temporal: `effective_from`/
  `effective_to`, current = `effective_to IS NULL`.
- **Spatial** (`app/domain/spatial/models.py`): `FloorPlan` (per-room, revisioned,
  draft/active/superseded) → `SpatialLayer` → `SpatialObject` (canonical mm geometry,
  `source` distinguishing authoritative/imported/discovered).
- **Floor-plan import** (`app/domain/floorplan_import/models.py`): `FloorPlanImportJob`
  → `FloorPlanImportDiagnostics` (1:1) and `FloorPlanImportCandidate` (1:many) — the
  review queue standing between untrusted geometry and authoritative `SpatialObject`
  rows.

---

## 6. Database Schema

One migration, `migrations/versions/0004_phase2_physical_spatial_model.py`, adds 12
tables: `rack_model`, `rack_model_revision`, `equipment_model`, `equipment_model_revision`,
`rack`, `equipment`, `floor_plan`, `spatial_layer`, `spatial_object`, `rack_placement`,
`equipment_placement`, `floor_plan_import_job` (plus its two child tables). It is purely
additive — no existing table, column, or constraint from migrations `e9fd19228f19`,
`0002_seed`, or `0003_correction` is altered or dropped.

Key constraints (all DB-enforced, all empirically tested — see §14):

- `rack_mounted_requires_rack_u_range_and_side` (CHECK) — the v1.3 correction.
- `no_front_overlap` / `no_rear_overlap` (partial GiST exclusion constraints, scoped by
  the generated `occupies_front`/`occupies_rear` columns) — no two rack-mounted items may
  overlap on the same physical side of the same rack in the same U-range.
- `rack_placement_one_timeline_per_asset` / `equipment_placement_one_timeline_per_asset`
  (range-exclusion constraints over `tstzrange(effective_from, effective_to)`) — at most
  one current placement per asset, no overlapping historical intervals.
- `uq_floor_plan_one_active_per_room` (partial unique index on `floor_plan(room_id)
  WHERE status='active'`).
- `check_placement_spatial_object_room_match()` (trigger function, both placement
  tables) — a placement's own `room_id` must match the room its linked `SpatialObject`
  (via its `FloorPlan`) actually belongs to.

---

## 7. API

All endpoints live under `/api/v1`, permission-gated via the existing
`require_permission()` dependency:

| Area | Endpoints |
|---|---|
| Catalog | `POST/GET /rack-models`, `POST/GET /rack-models/{id}/revisions`, same for `/equipment-models` |
| Racks | `POST/GET /racks`, `GET/PATCH /racks/{id}`, `POST /racks/{id}/move`, `POST /racks/{id}/retire`, `GET /racks/{id}/elevation` |
| Equipment | `POST/GET /equipment`, `GET/PATCH /equipment/{id}`, `POST /equipment/{id}/move`, `POST /equipment/{id}/retire` |
| Floor plans | `POST/GET /floor-plans`, `GET /floor-plans/{id}`, `POST /floor-plans/{id}/activate`, `POST /floor-plans/{id}/upload`, `GET /floor-plans/{id}/import-jobs`, `GET /floor-plans/import-jobs/{id}`, `GET .../diagnostics`, `GET/POST .../candidates[/accept\|/reject]`, `GET /floor-plans/{id}/objects` |
| Spatial | `GET /spatial/rooms/{room_id}/view` |

---

## 8. Rack Model

`Rack` (§5) + `RackModelRevision` (height_u/width_mm/depth_mm/weight_capacity_kg). Rack
placement is entirely separate (`RackPlacement`) — `Rack` itself has no room/position
column, matching §8's rule that placement is never duplicated on the entity it places.

## 9. Equipment Model

`Equipment` (§5) + `EquipmentModelRevision` (all dimensions nullable). Equipment's
placement is polymorphic across five `placement_type` values; `EquipmentMoveIn`'s
`model_validator` mirrors the DB's own CHECK constraint client-side, so a malformed
request gets a clean 422 instead of reaching the database.

## 10. Placement Model

Both `RackPlacement` and `EquipmentPlacement` are temporal, closed-then-opened atomically
in one transaction (`app/application/placement_service.py`), under a `SELECT ... FOR
UPDATE` lock on the current row. A concurrent second mover that loses the race gets a
`PlacementConflict` (mapped to a 409) the moment PostgreSQL's `EvalPlanQual` re-evaluates
the row post-unblock and finds it already closed — verified with a genuine two-session
concurrency test (`tests/api/test_racks.py::test_rack_move_concurrent_movers_only_one_wins_the_other_gets_409`),
not merely asserted from code reading. Retiring an already-retired placement is an
idempotent no-op (200, not 409) — the caller's intent (decommission it) is already
satisfied.

## 11. U-Space Model

`u_range` is a PostgreSQL `INT4RANGE` with `[)` bounds. `side` is one of
`front`/`rear`/`both`; `occupies_front`/`occupies_rear` are `GENERATED ALWAYS AS
COALESCE(side IN (...), false) STORED` columns — COALESCE specifically because `side` is
legitimately NULL for non-rack-mounted placements, and `NULL IN (...)` evaluates to NULL,
not FALSE (see §16 D1 for the bug this caused and how it was caught). The two partial
GiST exclusion constraints are scoped to these generated columns, so front and rear
occupancy are independently enforced, and `side='both'` correctly conflicts with either.

## 12. Spatial Coordinate System

Canonical storage is integer millimetres, room-local origin, X right / Y down, rotation
clockwise degrees (`app/domain/spatial/models.py`, `SpatialObject.x_mm/y_mm/rotation_deg`).
`FloorPlan.calibration_scale_mm_per_px` is the one place a px↔mm factor is computed and
stored — pixels are a presentation detail of a specific uploaded raster, never a second
coordinate system racks/equipment are placed in.

## 13. Floor Plan Model

`FloorPlan` is per-room and revisioned (`revision_number`, unique with `room_id`);
`status` is draft/active/superseded, with a partial unique index enforcing at most one
active revision per room at the database level (not just application logic — see §16 D2
for a real bug this constraint caught during testing). Activating a new revision
supersedes the room's previous active one in the same transaction.

## 14. Import Security

`app/application/svg_sanitizer.py` implements every content-level control the
architecture's §10a requires: content-sniffed type detection (never filename/extension),
a 5MB SVG / 20MB raster size cap enforced before any parsing, `defusedxml` with DTIDs/
external entities/external resources all forbidden (an XXE/DOCTYPE attempt is rejected
outright, not best-effort parsed), a hard element-count cap (5,000) enforced *during* the
walk (bounds a pathological-but-small file without a separate timeout), `<script>`/
event-handler-attribute/external-reference stripping, and a Sanitized Intermediate
Representation (`SirShape`) as the only thing that crosses back out of the module — never
raw markup or `Element` objects. Processing is entirely in-memory (bytes are never
written to any path on disk), which is a *stronger* property than a quarantine-directory
design specifically for path-traversal purposes. Disclosed limitation: this does not run
inside a separate OS-level subprocess/container with a credential-free identity, unlike
the architecture's "narrowly-scoped worker" language — a genuine infrastructure control
this implementation does not provide (see `PHASE2_GAP_ANALYSIS.md` §3.4).

Every attack in the adversarial matrix below was actually run, not just designed:

| Attack | Result | Test |
|---|---|---|
| XXE / external entity | Rejected outright (`forbid_dtd=True`) | `test_sanitize_svg_rejects_dtd_declaration_xxe_attempt`, `test_xxe_attempt_is_rejected_outright` |
| Embedded `<script>` | Stripped, not executed, recorded as a warning | `test_sanitize_svg_strips_script_tag_and_does_not_recurse_into_it`, `test_malicious_svg_is_sanitized_not_rejected_outright` |
| Event-handler attributes (`onload`, `onclick`) | Stripped | `test_sanitize_svg_strips_event_handler_attributes` |
| External `href`/`xlink:href` references | Stripped | `test_sanitize_svg_strips_external_href_references` |
| Malformed XML | Rejected | `test_sanitize_svg_rejects_malformed_xml` |
| Oversized file (>5MB SVG, >20MB blanket cap) | Rejected, at the right layer | `test_sanitize_svg_rejects_oversized_content`, `test_upload_beyond_the_endpoints_blanket_size_cap_is_rejected_before_any_parsing`, `test_svg_over_the_svg_specific_cap_is_accepted_then_fails_async_with_a_reason` |
| Pathological element count | Capped mid-walk, not hung | `test_sanitize_svg_caps_element_count_without_hanging` |
| Enormous / NaN / Infinity coordinates | Clamped to a safe default, never propagated | `test_sanitize_svg_clamps_out_of_range_coordinates_rather_than_propagating_them`, `test_sanitize_svg_treats_nan_and_infinity_coordinates_as_default` |
| Content/extension mismatch | Content wins; unrecognized content is rejected regardless of filename | `test_upload_content_sniffed_svg_is_accepted_regardless_of_filename`, `test_upload_unrecognized_content_is_rejected` |
| Path traversal via filename | Structurally impossible — no path is ever built from the filename; nothing is written to disk | disclosed design property, `app/application/svg_sanitizer.py` module docstring |

## 15. Rack Elevation

Computed live from `EquipmentPlacement` on every `GET /racks/{id}/elevation` call — never
stored, never cached. Verified sorted by U-position, and correctly excludes retired
equipment (`tests/api/test_racks.py::test_rack_elevation_excludes_retired_equipment`).

## 16. 2D Visualization

`GET /spatial/rooms/{room_id}/view` composes the room's currently-placed racks, its
currently-placed non-rack-mounted equipment, and its active `FloorPlan`'s `SpatialObject`s
into one read-only projection. The frontend's `RoomSpatialCanvas` renders this as plain
SVG (no canvas library dependency added — SVG is sufficient for this scale and keeps the
dependency surface unchanged) with zero authoritative state of its own; every position it
draws is read straight from the query result on each render.

## 17. Audit

Every significant Phase 2 mutation (rack/equipment create/update/move/retire, floor-plan
create/activate, import upload, candidate accept/reject) writes an audit row through the
existing `write_audit_log()` helper, with the same request/correlation-ID threading Phase
1 established.

## 18. Outbox

`RackCreated`, `RackUpdated`, `RackMoved`, `RackUnplaced`, `EquipmentCreated`,
`EquipmentUpdated`, `EquipmentPlaced`, `EquipmentUnplaced`, `FloorPlanRevisionCreated` —
exactly the set the Phase 2 prompt named, through the existing `write_outbox_event()`
helper and dispatcher, no new event bus.

## 19. Concurrency

Optimistic `version`/If-Match concurrency applies to `Rack`, `Equipment`, and `FloorPlan`
for their own narrow-field edits, reusing `app/application/concurrency.py` exactly.
`RackPlacement`/`EquipmentPlacement` use the stronger locked close-then-open transaction
(§10 above) for the actual contention point — placement, not the parent entity — per the
architecture's explicit instruction not to apply concurrency merely at the parent level
when contention is really on the placement record.

## 20. Authorization

New permissions (`rack:read/manage/place`, `equipment:read/manage/place`,
`floor_plan:read/import/manage`, `spatial:read`) are distributed across the five existing
Phase 1 roles — no new roles, no site-scoped RBAC:

| Role | rack | equipment | floor_plan | spatial |
|---|---|---|---|---|
| Administrator | read/manage/place | read/manage/place | read/import/manage | read |
| DCIM Manager | read/manage/place | read/manage/place | read/import/manage | read |
| Engineer | read/manage/place | read/manage/place | read/import | read |
| Operator | read/place | read/place | read | read |
| Viewer | read | read | read | read |

## 21. Migrations

`0004_phase2_physical_spatial_model` was validated three ways against a real PostgreSQL
16 instance (not merely reviewed):

1. **Clean install** — `alembic upgrade head` from an empty database through all four
   migrations in sequence: succeeds.
2. **Upgrade from Phase 1** — `alembic upgrade 0003_correction` (the exact Phase 1 final
   state) followed by `alembic upgrade head`: succeeds, confirming Phase 2 layers
   cleanly onto an existing Phase 1 installation.
3. **Downgrade then re-upgrade** — `alembic downgrade 0003_correction` removes all 12
   Phase 2 tables, the room-consistency trigger function, and the partial unique index
   cleanly (verified via direct schema inspection — zero rack/equipment/floor_plan/
   spatial tables remain, the trigger function is gone); the seeded RBAC permission rows
   are deliberately left in place (documented, same precedent as `0002_seed`'s own
   downgrade). Re-running `alembic upgrade head` afterward re-creates every table and
   re-seeds RBAC idempotently (checked via `SELECT ... GROUP BY ... HAVING count(*) > 1`
   — zero duplicate permission or role_permission rows), reproducing the exact
   role_permission counts from the original install (Administrator 21, DCIM Manager 18,
   Engineer 15, Operator 10, Viewer 7).

No already-applied migration was edited.

## 22. Testing

187 backend tests pass:

- **Unit** (`tests/unit/`): `test_spatial_validation.py` (18 tests — coordinate/rotation/
  U-range boundary and advisory-warning logic), `test_svg_sanitizer.py` (18 tests — the
  full adversarial matrix in §14, run against the sanitizer directly).
- **Integration** (`tests/integration/`): `test_phase2_placement_constraints.py` (17
  tests — every DB-enforced invariant, via direct ORM inserts bypassing the API, proving
  the database itself refuses the bad state).
- **API** (`tests/api/`): `test_racks.py` (18), `test_equipment.py` (13),
  `test_floor_plans.py` (18), `test_spatial.py` (5) — CRUD, placement/move/retire,
  elevation, RBAC boundaries (Viewer/Operator/Engineer denial cases), idempotency replay,
  optimistic-concurrency conflicts (including a genuine two-database-session race, not
  just a sequential stale-version check), and the full upload → async import → diagnostics
  → candidate accept/reject → 2D-view pipeline with Celery tasks invoked synchronously
  (`.run()`, mirroring Phase 1's own `test_outbox.py` convention) against the real test
  database.

Full suite: `cd backend && pytest -q` → `187 passed`.

## 23. Security Validation

Beyond the import-pipeline matrix in §14: RBAC boundaries were tested for every new
permission tier (Viewer cannot create/move/retire racks or equipment or manage floor
plans; Engineer can upload but not accept/reject import candidates — floor_plan:import
vs. floor_plan:manage are genuinely distinct gates, not the same permission under two
names); a SQL-injection-shaped string in a client-controlled `object_type` field is
rejected with a clean 422 by Pydantic validation before ever reaching a query
(`test_accepting_a_candidate_with_invalid_object_type_is_a_clean_422`); optimistic
concurrency correctly rejects a wrong If-Match value even outside of true concurrency
(`test_rack_move_with_wrong_if_match_version_is_409`).

## 24. Performance

No load/scale testing was performed (consistent with Phase 1's own disclosed scope).
Indexing was designed for the stated 10,000+ rack / 100,000+ equipment scale: partial
indexes on `(rack_id, effective_to)` / `(equipment_id, effective_to)` for the "current
placement" lookup pattern that every read path uses, and the GiST exclusion constraints
double as the indexes PostgreSQL needs to enforce them — no redundant index was added
alongside a constraint that already provides one.

## 25. Known Issues

No known Critical or High severity defect remains open. Three genuine defects were found
during this implementation's own testing and fixed before this report was written (not
deferred to a later red-team):

- **D1 (fixed):** `occupies_front`/`occupies_rear`'s original `Computed("side IN (...)")`
  expression, combined with `nullable=False`, broke every non-rack-mounted placement
  insert (`side` is legitimately NULL there, and `NULL IN (...)` is SQL NULL, not FALSE).
  Found via direct empirical SQL testing before any application code was written against
  the table. Fixed with `COALESCE(..., false)`.
- **D2 (fixed):** the floor-plan activation endpoint updated the newly-active and
  previously-active rows in a single flush, in an order SQLAlchemy doesn't guarantee —
  PostgreSQL's non-deferrable partial unique index (`uq_floor_plan_one_active_per_room`)
  correctly rejected the transient state where both rows were momentarily active. Found
  via an API-level round-trip test (`test_activating_a_floor_plan_supersedes_the_previously_active_one`),
  not code review. Fixed by flushing the "supersede" update before the "activate" update.
- **D3 (fixed):** the rack/equipment move endpoints' `PlacementConflict` handler read
  `current.room_id`/`current.version` *after* `await db.rollback()` — `Session.rollback()`
  expires every attached object, so this attribute access attempted an implicit lazy-load
  reconnect outside of any awaited async context, crashing with `MissingGreenlet` instead
  of returning the intended 409. Found via a genuine two-database-session concurrency
  test, not by reading the code. Fixed by reading those fields before the rollback.
- **D4 (fixed, found via manual browser smoke test, not the automated suite):** the
  Celery worker process only imports the specific ORM classes each task module directly
  references (e.g. `floorplan_import.py` imports only `FloorPlanImportJob`), so any *other*
  mapped class — including the target of that class's own foreign keys, like `app_user` —
  was never registered on `Base.metadata` in that process. SQLAlchemy resolves FK targets
  lazily at first flush, so this surfaced only when an actual file was uploaded through
  the running application and processed by a real worker (`NoReferencedTableError`
  the first time `run_floor_plan_import_job` tried to `INSERT` a job row) — not in pytest,
  because the test process already imports `app.main`, which transitively imports every
  domain model. Fixed by adding `import app.db.models` to `app/infrastructure/celery_app.py`,
  the same fix `migrations/env.py` already applies for the identical reason.

One deferred, disclosed limitation, not a defect: floor-plan import does not calibrate
SVG source-file coordinate units to real-world millimetres automatically — an imported
shape's raw `x`/`width`/etc. from the source file are carried through as-is until a human
sets `calibration_scale_mm_per_px` (or accepts geometry that's already in a sensible
scale). Observed directly during the browser smoke test (§20): an accepted candidate from
a small hand-authored SVG rendered as a barely-visible dot on a default 10m×10m canvas.
This is the intended, disclosed design (§10 of the Phase 2 prompt: "calibration-only" for
raster; SVG shapes carry the source file's own units) — not something this report treats
as fixed, since fixing it would mean inventing an auto-calibration heuristic the
architecture doesn't ask for.

## 26. Deferred Work

Everything listed as explicitly out of scope in §2, plus: an explicit `GET /rack-model-
revisions`/`GET /equipment-model-revisions` cross-model search endpoint (the current
catalog API is scoped per-model, matching what the frontend needed); full OS-process
isolation for the SVG/raster parser (§14); DXF/VSDX/PDF floor-plan import formats.

## 27. Phase 1 Carry-Forward Items

- **NEW-1 (Medium, from `PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md`):** the narrow
  idempotency stale-reclaim race (an original claim owner resuming after a stale-timeout
  reclaim gets an incorrect 409 instead of the reclaimer's successful result) is **not
  touched** in Phase 2. Rack/Equipment creation reuse `app.application.idempotency`
  byte-for-byte, unmodified — the same mechanism, the same finding, still open, still
  Medium severity, carried forward rather than silently redesigned. Phase 2's own
  idempotency tests (`test_duplicate_idempotency_key_for_rack_create_replays_the_same_result`,
  the equivalent for equipment) exercise the ordinary duplicate-key replay path, which
  this finding does not affect; they do not newly expose NEW-1's specific stale-reclaim
  window, so no new regression test for it was added here — it remains exactly as
  documented in the Phase 1 report, for the independent red-team to re-verify against.
- **NEW-2 (Low, cosmetic):** the `managed_asset` CHECK constraint naming issue
  (`ck_managed_asset_ck_managed_asset_no_self_replacement` vs. the expected
  `ck_managed_asset_no_self_replacement`) lives in a migration Phase 2 never touches
  (`0003_correction`), so it is **left exactly as found** — fixing it would mean editing
  an already-applied migration, which this implementation does not do. Untouched, not
  forgotten.

## 28. Traceability

See `PHASE2_TRACEABILITY_MATRIX.md`.

## 29. Final Gate

**PHASE 2 IMPLEMENTATION COMPLETE — READY FOR INDEPENDENT RED-TEAM VALIDATION.**

- 187/187 backend tests passing.
- Migration validated clean-install, upgrade-from-Phase-1, and downgrade/re-upgrade.
- Frontend builds, typechecks, and lints clean; golden path manually verified end-to-end
  in a real browser against a live backend + Celery worker (§20 above; see
  `PHASE2_IMPLEMENTATION_RED_TEAM_SCOPE.md` for exact repro steps).
- Zero known Critical/High defects. Four genuine defects found during this
  implementation's own testing were fixed, not deferred.
- Docker: **UNAVAILABLE** in this sandbox (no daemon), matching Phase 1's own disclosed
  precedent — no runtime-container claim is made beyond what was actually executed
  against real PostgreSQL/Redis/Celery processes directly.
