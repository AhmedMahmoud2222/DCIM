# Issue #106: validated CFD / thermal analysis

Document version: 0.2.1 (M1: dependency verification and architecture handoff; documentation only; 0.2.1 records confirmed signature status)
Supersedes: 0.1.0 (the 15-line planning stub from commit `1b544f3`)
Status: DRAFT for independent architecture/dependency review. Not accepted.

Companion documents (all version 0.1.0, all proposals until accepted):

| Document | Content |
|---|---|
| [ADR-0013 CFD solver selection](../architecture/ADR-0013-cfd-solver-selection.md) | Solver comparison and pinning contract (section D) |
| [CFD input contract v1](../architecture/CFD_INPUT_CONTRACT_V1.md) | Versioned computational input design (section E) |
| [CFD service, data and security architecture](../architecture/CFD_SERVICE_SECURITY_ARCHITECTURE.md) | Entities, queueing, isolation, authorization (section F) |
| [CFD verification and validation policy](../architecture/CFD_VERIFICATION_VALIDATION_POLICY.md) | Benchmarks, targets, acceptance states (section G) |

Nothing in this increment executes a solver, adds a migration, changes the frontend, or enables CFD. No validation
evidence exists. Every statement of the form "the system can" below refers to code already on `main` and cites its path.

## A. Live-state report

Collected 2026-10-20 from live GitHub and a fresh fetch. Earlier numbers in the assignment were reference points only.

| Item | Observed |
|---|---|
| `main` | `92be69284d246868bb1c740bd98d4eba0edd7abc` (G7 readings provenance, #141) |
| PR #115 | open, Draft, head `feature/r4-cfd-106` at `1b544f374cd8ce9645e31a0df139a1a5e7076c71` before this increment |
| PR #115 recorded base SHA | `6f4f104240cba8625f418db0caef91207a2e746f`; GitHub reports `mergeable_state: behind` |
| Merge base of `main` and the PR head | `6f4f104240cba8625f418db0caef91207a2e746f`; `main` is 60 commits ahead, the branch 1 commit ahead |
| Files changed by the pre-M1 PR | 1 (`docs/implementation/ISSUE_106_CFD_THERMAL_ANALYSIS.md`, 15 lines added) |
| Commits on the pre-M1 PR | 1 (`1b544f3`, author and committer `AhmedMahmoud2222`, `docs: start issue #106 CFD thermal analysis implementation`) |
| Reviews, review threads, PR comments on #115 | none |
| Issue #106 | open, no assignee, no linked closing PR |
| `AGENTS.md` | none exists in the repository (searched the whole tree); no repository-level agent instruction file applies |
| Workflow guidance files | `.claude/skills/steward` and `.claude/skills/babysit` are absent from the head branch |

Checks on `1b544f3` (run 37216399453, 37216399461, 37216401465): `backend`, `backend suite` on Python 3.12, 3.13 and 3.14,
`frontend`, `edge-collector`, `browser-e2e`, `Compose smoke` and `Deployment validation gate` succeeded.
`github-advanced-security` failed. The check run carries no output text, and this session did not read its log. Earlier PRs
(#126, #130) recorded the same check failing with an external HTTP 402 quota error before analysis. That is a prior
classification of a different run, so it is not evidence for this PR. Treat the scanner as not run until someone reads the log.

Local checkout: `/home/user/DCIM`, `origin/main` and `origin/feature/r4-cfd-106` were fetched and matched the cloud SHAs above
before any file was read.

### Branch handling in this increment

History is preserved. `main` was merged into the PR branch with an ordinary merge commit, so the evidence in this document
refers to files that exist on the branch. Nothing was force-pushed, rebased, amended, retargeted or replaced, and no second PR
exists.

### Signing-policy implications (reported separately from the technical work)

1. The GitHub REST commit endpoint (`commit.verification`) reports `1b544f3`: `verified: false`, `reason: unsigned`. The
   owner's report is confirmed. The same endpoint reports the merge commit `6667c3a712accfecdb394b5729b0248867c65614`
   and the M1 documentation commit `073bfec86c56e79fef54acf18b7074620e59d02e` as `verified: true`, `reason: valid`
   (checked 2026-10-20). Local `git log --format=%G?` reads `N` for every commit because the checkout has no
   `gpg.ssh.allowedSignersFile`; the local value is not evidence.
2. If branch protection requires verified signatures on every commit of a PR branch, an unsigned `1b544f3` blocks a merge
   commit or rebase merge. A squash merge made in the GitHub web UI carries a GitHub signature and sidesteps the rule. The
   owner decides between that, a documented waiver, or a replacement branch. This increment does none of them.
3. Commits added in this increment are signed by the environment's signing helper (`gpgsig` header) and show as verified on
   GitHub. The commits that follow this note are verified the same way before the PR comment reports them.

## B. Dependency readiness matrix

Verdicts: READY (usable as an input source today), PARTIAL (usable with stated limits), GAP (the CFD data path cannot work
without new work). "Accepted" below means independent acceptance evidence found on live GitHub. Closed issues and merged PRs
are not treated as CFD readiness.

### B.1 #99 canonical units (merged PR #108, merge commit `754484d9e6508e528fccdf8d934689f23b53ea6f`)

| Aspect | Evidence |
|---|---|
| Source | `backend/app/domain/telemetry/registry.py` (`REGISTRY_VERSION = "1"` at line 15; metric table around lines 130 to 142), snapshots `registry_v1.snapshot.txt` and `registry_v2.snapshot.txt` in the same directory, `docs/architecture/ADR-0012-canonical-physical-units.md`, migration `0035_units_metric_registry.py` |
| Units | `temperature_c` degC, `supply_air_temperature_c`, `return_air_temperature_c` degC, `airflow_m3_s` m3/s (presented m3/h), `airflow_velocity_m_s` m/s, `differential_pressure_pa` Pa, `cooling_output_kw` kW, `power_kw` kW |
| Provenance | Readings keep `raw_value`, `raw_unit`, `source_scale`, `registry_version` (`domain/telemetry/models.py` lines 168 to 215); historical rows keep nulls |
| Tests | `backend/tests/unit/test_physical_units.py` (16 tests), `test_registry_snapshots.py`, `test_registry_ledger.py`, `backend/tests/integration/test_units_metric_registry_migration.py` |
| Acceptance evidence | PR #108 comment `5999496835`: Codex review "Didn't find any major issues" at `9a4ecae0ca24`, exact-head CI listed in comment `6003769295`. The external scanner failed on quota there |
| Limits | The registry holds no length, volume or density unit for geometry: geometry is stored in integer millimetres by the spatial model, outside the registry. No metric exists for sensor accuracy or measurement uncertainty. The code contains registry version "1" while a v2 snapshot file exists; the CFD contract must pin whichever version the ingest path writes, and M2 must confirm which |
| Verdict | READY for temperatures, airflow, pressure and power. GAP for any quantity the CFD contract needs in a unit the registry lacks (length, density, uncertainty) |

### B.2 #104 calibrated spatial digital twin (merged PR #126, merge commit `eaf0fa01a7ea9cac263f8fce4d7311ed2ba311f6`)

Original planning Draft PR #113 (`feature/r4-spatial-digital-twin-104`, head `de47fbd`) is still open and planning-only. It is not the implementation.

| Aspect | Evidence |
|---|---|
| Source | `domain/spatial/models.py` (`FloorPlan`, `FloorPlanCalibration`, `SpatialLayer`, `SpatialObject`), `domain/placement/models.py` (`RackPlacement`, `EquipmentPlacement`), `application/spatial_import/`, `application/spatial_validation.py`, `application/spatial_overlays.py`, `api/v1/spatial.py`, migration `0044_spatial_digital_twin.py`, `docs/implementation/ISSUE_104_SPATIAL_DIGITAL_TWIN.md` |
| Coordinates | Integer millimetres, room-local origin, X right, Y down, rotation clockwise degrees (`domain/spatial/models.py` lines 143 to 160; `domain/cooling/models.py` line 131 comment for bearings). Calibration is immutable (UPDATE and DELETE rejected by trigger) and stores method, scale, origin, axis, rotation quadrants, `error_bound_mm`, `relative_error`, `confidence` (`FloorPlanCalibration`, lines 85 to 120) |
| Authoritative status | `SpatialObject.source` is `authoritative`, `imported` or `discovered`; placements own rack and equipment position. Accepted imported shapes carry a `provenance` JSON record. `RackPlacement.x_mm/y_mm/rotation_deg` are nullable |
| Geometry present | 2D only. Object types `room_outline`, `wall`, `column`, `obstacle`, `aisle` with `x_mm`, `y_mm`, `width_mm`, `height_mm`, `rotation_deg`. In `SpatialObject` the `height_mm` column is the second planar extent, not a vertical height |
| Rack dimensions | `api/v1/spatial.py` lines 40 to 55 and 116: `width_mm`, `depth_mm` from the catalog revision and `height_mm = height_u x 44.45`; the source comment states frame and plinth overhead is not invented |
| Tests | `tests/unit/test_spatial_calibration.py`, `test_spatial_classify_reconcile.py`, `test_spatial_import_sandbox.py`, `test_spatial_validation.py`; `tests/api/test_spatial.py`, `test_spatial_authority.py`, `test_spatial_import_api.py`, `test_spatial_twin_views.py`; `tests/integration/test_spatial_digital_twin_migration.py` |
| Acceptance evidence | PR #126 comment `6071687945`: Jules, `ACCEPTED FOR MERGE GATE`, frozen head `8237a8a9e240`, 0 blocking findings, scanner failure classified as quota |
| Limits | No room (ceiling) height anywhere in the data model: `Room` has `area_sqm` and `raised_floor` only (`domain/location/models.py` lines 99 to 116). `FloorPlan.room_width_mm/room_height_mm` are plan extents, not vertical height. Walls, columns and obstacles have no vertical extent or elevation. `spatial:read` and `floor_plan:*` are not scope-aware: a site-restricted user gets 403 |
| Verdict | PARTIAL: calibrated plan-view geometry and rack footprints are READY; every vertical dimension is a GAP |

### B.3 #105 cooling / environment model (merged PR #114, merge commit `22dda086dda698b566b05fdafb49f451d8029a0c`)

| Aspect | Evidence |
|---|---|
| Source | `domain/cooling/models.py` (`CoolingUnit`, `CoolingGroup`, `EnvironmentalSensor`, `ThermalZone`, `ContainmentElement`, `CoolingUnitZone`), `application/thermal/{environment,heatmap,interpolation,airflow,capacity,zone_geometry,exceptions}.py`, `api/v1/cooling.py`, `api/v1/thermal.py`, migration `0045_cooling_thermal.py`, `docs/implementation/ISSUE_105_COOLING_THERMAL.md` v1.4 |
| Units | kW, m3/s, degC, percent, millimetres (module docstring). `supply_direction_deg` and `flow_direction_deg` are bearings in the room frame (0 = +X, clockwise) |
| Provenance | `CoolingUnitZone.semantics` is `authoritative`, `configured` or `modelled`. Airflow elements carry `configured_design` or measured provenance; `modelled` is reserved for #106 and never produced. NULL means unknown |
| Cooling fields | Rated and configured capacity (kW), `airflow_capacity_m3_s`, `supply_air_target_c`, `return_air_design_c`, humidity limits, `operating_status`, group. The airflow figure is a design value; there is no field for measured airflow on the unit. Measured airflow arrives only through an `airflow` sensor reading |
| Sensors | `EnvironmentalSensor`: kind, role (`rack_inlet`, `rack_exhaust`, `supply_air`, `return_air`, `ambient`, `other`), `elevation_mm` ("recorded for provenance"; the 2D heat map ignores it), `flow_direction_deg`. Rack-mounted sensors take the rack footprint centre and are flagged `position_exact=false` (`application/thermal/environment.py` around lines 133 to 140 and 218 to 234) |
| Freshness | `classify_reading`: fresh when age <= max(poll interval, 1) x 3; stale; missing; invalid for non-canonical unit or a timestamp more than 120 s in the future |
| Zones and containment | `ThermalZone` is 2D (rect or polygon in mm). `ContainmentElement` is a 2D line segment (`x1,y1,x2,y2`) with kind `boundary` or `opening`. No height, door, roof, leakage area or 3D surface exists |
| Tests | `tests/unit/test_cooling_capacity_rules.py`, `test_cooling_units_registry.py`, `test_thermal_interpolation.py`, `test_thermal_zone_geometry_check.py`; `tests/api/test_cooling_assets.py`, `test_cooling_placement_if_match.py`, `test_thermal_authz.py`, `test_thermal_scope.py`, `test_thermal_views.py`, `test_thermal_alarm_boundary.py`, `test_thermal_exceptions.py`, `test_thermal_ingestion.py`; `tests/integration/test_cooling_concurrency.py`, `test_cooling_thermal_migration.py` |
| Acceptance evidence | Not found. PR #114 has no comments, reviews or review threads on live GitHub, and the #105 issue thread shows only the assignment comment. If an independent acceptance exists, it is not recorded on GitHub. Absence is reported, not assumed |
| Limits | Cooling units have no footprint, elevation, supply/return surface geometry or catalog linkage ("footprint not modelled, not to scale" in `ISSUE_105_COOLING_THERMAL.md` section 1). Heat maps are interpolation, "not validated CFD". Sensor accuracy and calibration state are not stored. No plenum, perforated-tile or raised-floor data beyond the `Room.raised_floor` boolean |
| Verdict | PARTIAL: operating state, design setpoints, sensor roles and 2D zones are READY as inputs; all cooling geometry and measured unit airflow are GAPS. Independent acceptance for the dependency is MISSING |

### B.4 #130 thermal-load completeness (merged, `59bbb8dd8ad926ab5a87e5a38773adb73e1bf781`)

| Aspect | Evidence |
|---|---|
| Source | `application/thermal/capacity.py` (`NON_OPERATIONAL_LIFECYCLE_STATUSES`, `load_room_equipment`, `_without_excluded`), `application/thermal/exceptions.py`, `application/power_rollup*.py`, docs v1.4 sections 6 |
| Semantics | Load is electrical IT load x 1.0 (no COP/PUE). Unknown demand makes the load `incomplete` and the figure null; lower bounds are exposed separately. Planned and reserved equipment are excluded and counted in `pending_equipment_count`. Empty room is `unknown`. Redundant A/B inputs count once |
| Tests | `tests/api/test_thermal_load_completeness.py`, `test_thermal_load_lifecycle.py` (15 tests), `test_thermal_load_population.py` |
| Acceptance evidence | PR #130 comment `6097858769`: Jules report relayed by the owner's assistant, head `0bd4b81`, explicitly "not a native GitHub approval" |
| Limits | The load is a point-in-time total per room, rack or zone. No time-window series. Lifecycle is an asset-record signal, not an electrical state. Floor-standing equipment has no position, so geometry zones stay `incomplete` while any such equipment is placed. Electrical-to-thermal factor 1.0 is an accounting assumption |
| Verdict | PARTIAL: completeness logic is READY and is the correct gate for "known load". The rack-level heat figure for a CFD source term must come from the same completeness classification; per-rack time windows need a decision (see C.2) |

### B.5 #128 follow-up work (only items on the CFD data path)

| Item | State on live GitHub | On the CFD data path? |
|---|---|---|
| G1 mapping revisions (#131), G2 retention/history (#129), G3 lossless source text (#135), G4-A exact aggregate sums (#136), G4-R replay ledger (#137) | merged | Yes, for sensor readings: the validation window uses raw readings whose `mapping_revision_id`, `contract_evidence`, `raw_value_text` fix what a number means. Gate: use raw readings only |
| G6 aggregate lineage (#143) | merged to `main` (`4ce4f64`); the G7 doc says aggregate provenance still waits on independent G6 acceptance and owner authorization | No. CFD validation uses raw readings, not daily aggregates. Aggregates must not feed comparison windows |
| G7 readings provenance (#141, `92be692`) | merged | Yes: `reading_provenance_status` (`api/v1/telemetry.py` line 183) classifies a reading as pinned, inferred, operator_resolved, unverified_legacy or unverified_registry. Sensor eligibility policy in the V&V document consumes it. Docs: `docs/G7_READINGS_PROVENANCE.md` |
| G8 unresolved alarm rules (#139) | merged | No |
| G9a Decimal catalog publication (#140) | open Draft | Not for M1 to M3. It becomes relevant only if M2 reads catalog dimensions or heat figures as exact decimals. Catalog columns today are `Numeric(10,3)` and `Numeric(10,2)` already |
| G10 registry snapshots draft (`docs/implementation/G10_REGISTRY_SNAPSHOTS_DRAFT.md`), #146 | not on the path | No |

### B.6 Readiness summary

CFD is not ready to run. The plan-view geometry, rack footprints, cooling setpoints and the thermal-load completeness gate are
usable. The following block a physically meaningful run: room height, rack enclosure height, cooling-unit geometry,
3D containment, vertical extent of obstacles, equipment airflow, sensor accuracy, and independent acceptance of #105.

## C. Existing-versus-missing capability inventory

### C.1 Inventory

| Capability | Exists on `main` | Missing |
|---|---|---|
| Room boundary (2D) | yes, `room_outline` spatial object plus calibration with error bound | none for plan view |
| Physical room height | no | field, source and error bound |
| Closed 3D fluid domain | no | construction from plan + heights + openings, closure check |
| Rack position, footprint, orientation | yes (`RackPlacement`, catalog width/depth) | none for plan view |
| Rack enclosure height | partial: U x 44.45 mm; catalog `height_value` exists in `CatalogModelRevision` and is reachable through `bridged_from_catalog_revision_id` | rule for choosing catalog height over U height; frame/plinth overhead |
| Inlet/exhaust faces, airflow direction | partial: `airflow_direction` on catalog revision (`front_to_rear`, `front_to_top`, `side_to_side`, `other`); `occupies_front/rear` on placement | per-face area, perforation, blanking, door state |
| Walls, columns, obstacles | 2D shapes only | vertical extent and elevation |
| Raised floor and plenum | `Room.raised_floor` boolean | plenum depth, tile layout, perforation, leakage |
| Containment | 2D boundary/opening segments and aisle kind | height, roof, doors, leakage, openings as 3D surfaces |
| Cooling-unit footprint, elevation, surfaces | no | all of it |
| Cooling supply/return temperature | design setpoint fields; supply/return metrics exist | measured values must come through sensors |
| Cooling airflow | `airflow_capacity_m3_s` (design); `airflow_m3_s` reading via airflow sensor | measured unit airflow; unit-to-sensor binding |
| Cooling capacity and state | rated, configured, `operating_status` | none for gating; used as a limit check, never as a boundary condition |
| Rack heat load | per-room, per-rack current load with completeness (#130/#102) | time-windowed history per rack; uncertainty |
| Sensor position | x/y from placement, rack-centre approximation flagged | z reliability, x/y accuracy |
| Sensor accuracy, calibration, role | role yes; accuracy and calibration date no | accuracy field, calibration evidence |
| Sensor freshness | yes (`classify_reading`) | window-level validity rules (V&V policy) |
| Reading provenance | yes (G1 to G7) | none for raw readings |
| Job state, outbox, Celery | `outbox_event` (dispatcher only logs), Celery queues without a CFD queue, bulk-import lease/attempt pattern, extraction requeue sweepers | CFD queue, handler, leases for CFD |
| Process sandbox | spatial-import child: rlimits, Landlock, seccomp, process group kill (`application/spatial_import/runner.py`, `limits.py`) | container isolation for a native solver; the import sandbox does not run solvers |
| Storage | `StorageBackend` protocol (`save/read/exists/delete/iter_keys`), local backend | checksummed artifact catalogue, size accounting, authorization on download |
| Authorization | `cooling:*` scope-aware; `spatial:read` not scope-aware; RoleAssignment scope enforcement (#170) | `cfd:*` permissions and a whole-room visibility rule |

### C.2 Missing prerequisites: narrow within #115 or owner decision

"Narrow" means an additive, reviewable change that stays inside the CFD feature and does not alter authoritative models.
Any migration is also blocked until this M1 design is accepted.

| Prerequisite | Route | Reason |
|---|---|---|
| Rack enclosure height from the catalog revision through the bridge | narrow, read-only adapter in M2 | data exists; no schema change. Falls back to unknown when no published catalog height exists |
| Room height | owner decision | needs an additive column or room-geometry object, an authoring workflow, an error bound and audit. Touches the authoritative location model |
| Wall, column, obstacle, containment vertical extent | owner decision | schema and UI on the #104 model |
| Cooling-unit footprint, elevation, supply/return surface | owner decision | schema; cooling catalog type deferred in #105 |
| Plenum, perforated tiles, leakage areas | owner decision | new physical data, possibly out of first scope; first scope may declare "no raised-floor plenum modelling" and refuse rooms with `raised_floor = true` unless the owner approves a stated simplification |
| Equipment/rack airflow and resistance data | owner decision | data entry or catalog fields; cannot be derived from temperature |
| Sensor accuracy and calibration evidence | owner decision | additive columns on `environmental_sensor` or a revision table |
| Per-rack time-windowed heat load | narrow if the existing power history (`application/power_history.py`, `infrastructure/tasks/power_analytics.py`) answers it; owner decision otherwise | M2 spike must show which; not verified in M1 |
| Independent acceptance of #105/#114 | owner decision | acceptance evidence is absent on GitHub |
| Signing policy for `1b544f3` | owner decision | section A |

## D to G. Architecture documents

Sections D (solver ADR), E (input contract), F (service/data/security) and G (verification and validation) are in the companion documents listed at the top. Summary of the decisions they propose:

1. Solver: OpenFOAM, one distribution, version-pinned and container-digest-pinned, steady buoyant RANS, run in a dedicated
   isolated worker. Primary-source verification and the digest are outstanding (ADR section "Verification status").
2. Contract: one immutable, content-hashed, unit-explicit JSON input document per scenario revision, with a tested
   transform from the DCIM frame (X right, Y down, clockwise) to a right-handed SI solver frame.
3. Service: PostgreSQL owns job and evidence rows; an atomic outbox hands work to a dedicated CFD queue; execution,
   numerical convergence, measurement validation and engineering acceptance are four independent state machines.
4. Validation: gates are project policy, not industry standards. Insufficient data blocks validation instead of passing it.

## H. Ordered implementation increments

Each increment is its own reviewed step. None begins before M1 and the input-contract design are accepted. Each needs signed
commits, exact-head CI, and independent review. Whether an increment needs its own PR is an owner decision; this document
creates no extra PRs.

| Increment | Scope | Exit criteria |
|---|---|---|
| M1 (this) | Documents only | Codex verdict ACCEPTED on the exact head; owner decisions in C.2 answered or deferred explicitly |
| M2 | Input assembler, no solver: pure functions that read existing models and emit the v1 contract; coordinate transform and golden-vector tests; completeness and refusal reasons (unknown stays unknown); canonical serialization and hash; fixtures | Transform tests include handedness and rotation; the same DB state yields byte-identical documents and hashes; any unknown required input yields a refusal with named reasons; no schema migration unless the owner approved one in C.2; hidden-asset tests prove a scoped user gets a refusal, not a partial room |
| M3 | Solver image and case generation, offline: pinned image with recorded digest, server-owned case templates, case generator, mesh and checkMesh gates, run on repository fixtures from a developer or CI container only; no API | Image digest and distribution recorded from the registry; template rejects unsafe directives (test list in the service document); analytic and buoyancy verification cases pass the V&V acceptance table; reproducible case hash |
| M4 | Persistence and job state: migrations for revisions, runs, attempts, artifacts; outbox handler, CFD queue, leases, heartbeats, cancellation, retention | Duplicate delivery and worker death tests on PostgreSQL; four state machines enforced by CHECK constraints; artifact checksum verified on read; authorization tests including revocation mid-run |
| M5 | API, operator report and read-only UI: run submission, status, report with assumptions, convergence, warnings, "not validated" labelling | No result is labelled validated without a stored validation assessment and acceptance; failed or non-converged runs render as such; no write path to inventory |
| M6 | Sensor validation and scenario comparison: calibration/holdout split, frozen windows, error metrics, acceptance workflow | Insufficient-data cases block validation; metrics reproduce on frozen fixtures; at least one real measured-versus-predicted comparison is reported with its limits, or the report states none exists |

## Restrictions honoured in this increment

Documentation and design only. No solver code, no migration, no frontend change, no fabricated data or validation evidence.
CFD stays disabled. The PR stays Draft. Nothing was merged, deployed or closed, and no protection was bypassed.
