# CFD computational input contract v1 (design only)

Document version: 0.1.0
Status: PROPOSED. Schemas and examples are documentation. No code, migration or API implements them yet.
All numbers in examples are illustrative placeholders, not data from any room. Hashes are shown as `<computed>` because no
document was produced.

## 1. Principles

1. One immutable document describes everything the solver needs. The solver never reads the database.
2. A scenario is a layer of declared deltas on a baseline. A scenario never writes to inventory, placement, calibration,
   catalog or operating state. The assembler reads those tables and the scenario revision only.
3. Unknown stays unknown. A required value that is unknown makes assembly fail with a named reason. A defaulted value is
   allowed only when the default is declared, labelled `assumption`, and listed in the report.
4. Every quantity has an explicit unit and a provenance record. Conversion goes through the units registry. Values are not
   guessed from other quantities. In particular airflow is never derived from a temperature difference.
5. Serialization is deterministic and hashed.

## 2. Identity and linkage

| Field | Meaning |
|---|---|
| `contract_version` | `"cfd-input/1"`. Breaking changes mint `cfd-input/2` |
| `model_revision_id` | Immutable CFD model revision (a snapshot of the baseline inputs) |
| `baseline_revision_id` | The baseline this document derives from, or null for a baseline |
| `scenario_revision_id` | Immutable scenario revision, or null for the baseline run |
| `site_id`, `room_id` | Existing identifiers |
| `floor_plan_id`, `floor_plan_revision_number`, `calibration_id` | The active plan and the calibration row in force at assembly (`FloorPlan`, `FloorPlanCalibration`) |
| `placement_refs[]` | `{asset_id, placement_id, placement_version}` for every rack, cooling unit and sensor included |
| `catalog_refs[]` | `{asset_id, catalog_model_revision_id}` for racks and equipment, where one exists |
| `registry_version` | Telemetry registry version used for every canonical metric in the document |
| `assembled_at`, `assembled_by` | Assembly time (UTC, RFC 3339) and actor |
| `source_snapshot` | `{main_sha, alembic_head}` for traceability, not for logic |

`model_revision_id` and `scenario_revision_id` are the only identities the run points at. Re-running the same ids with a
different database state is impossible, because the document, not the database, is the input.

## 3. Units and conversion provenance

The document uses SI base units in the solver frame: metres, kilograms, seconds, kelvin for solver-side values, and degrees
Celsius where the field is documented as a reporting temperature. Every numeric field is an object:

```json
{"value": "0.600", "unit": "m", "source_unit": "mm", "source_value": "600",
 "conversion": {"registry_version": "<from registry>", "method": "linear", "factor": "0.001"},
 "provenance": {"kind": "catalog|calibrated|measured|configured|assumption|derived",
                "ref": {"table": "rack_model_revision", "id": "<uuid>"}},
 "uncertainty": {"kind": "bound|stddev|unknown", "value": "0.002", "unit": "m"}}
```

Rules: `value` is a decimal string with a stated scale, never a binary float literal. Conversions are exact decimal
operations performed once. `unit` must exist in the registry or in the geometry unit list below. Length, density and
uncertainty units are not in the telemetry registry today (`backend/app/domain/telemetry/registry.py`); the contract keeps its
own closed unit list (`m`, `mm`, `K`, `degC`, `kg/s`, `m3/s`, `Pa`, `W`, `kW`) until the registry gains them, and records
that fact in `conversion.method`.

## 4. Coordinate systems and transform

DCIM frame (authoritative): integer millimetres, room-local origin, X to the right, Y downward, rotation in degrees
clockwise, as stored in `SpatialObject`, `RackPlacement` and `EquipmentPlacement`. Vertical position uses height above the
finished floor in millimetres (the sensor field is `elevation_mm`). X, Y and Z-up in this frame is a left-handed system.

Solver frame: right-handed, metres, x east-equivalent along DCIM X, y along DCIM negative Y, z up.

```
x_s = X_mm / 1000
y_s = -Y_mm / 1000        (flip: DCIM Y points down)
z_s = Z_mm / 1000         (height above finished floor)
theta_s = -theta_dcim     (clockwise in the DCIM plan becomes counter-clockwise about +z)
```

The matrix of the linear part, `diag(1, -1, 1)/1000`, has determinant `-1` in millimetre terms: the reflection is what
turns the left-handed DCIM triple into a right-handed solver triple. The contract stores the transform explicitly:

```json
"frame": {"dcim": {"x": "right", "y": "down", "z": "up", "unit": "mm", "rotation": "clockwise_deg"},
          "solver": {"handedness": "right", "unit": "m", "x": "dcim_x", "y": "minus_dcim_y", "z": "up"},
          "transform_id": "dcim-plan-to-solver/1",
          "origin_offset_m": ["0", "0", "0"]}
```

Required tests (M2): golden vectors for the four axis points and four rotations (0, 90, 180, 270) of an asymmetric L-shaped
room; a determinant check that the assembled solver triple of the unit vectors is right-handed (`(e_x cross e_y) dot e_z > 0`);
round trip of DCIM to solver to DCIM within one millimetre; outward normals of every boundary face point away from the fluid
for the golden room; a rack placed with rotation 90 puts its front face where the catalog `front` is after transformation.

## 5. Geometry

### 5.1 Room and fluid domain

```json
"room": {
  "boundary": {"polygon_m": [[0,0],[12.0,0],[12.0,-8.0],[0,-8.0]], "source": "spatial_object.room_outline",
               "calibration_error_bound_m": {"value": "0.012", "confidence": "high"}},
  "height": {"value": null, "unit": "m", "provenance": {"kind": "missing"}},
  "floor_elevation_m": "0", "raised_floor": {"declared": "none|plenum"}
}
```

`room.height` is currently unavailable in the data model. Assembly refuses to produce a 3D domain while it is null. A
closed fluid domain exists only if the shell is closed: floor, walls, ceiling and every opening are declared surfaces.
Openings that are neither supply, return nor leakage make assembly fail.

### 5.2 Rack

`id`, `asset_id`, `position_m` (footprint origin), `rotation_deg`, `footprint {width, depth}`, `height` with
`provenance.kind` `catalog` (catalog revision `height_value`) or `derived_from_u` (`U x 44.45 mm`, flagged as an
underestimate that excludes frame and plinth), `front_face` and `rear_face` with airflow direction (`front_to_rear`,
`front_to_top`, `side_to_side`, `other`), perforation or blanking fraction (unknown allowed, flagged), `heat_load`,
`airflow`.

`heat_load`:

```json
"heat_load": {"power": {"value": "4200", "unit": "W"},
              "thermal_factor": {"value": "1.0", "provenance": {"kind": "assumption", "note": "electrical to thermal accounting factor used by thermal capacity"}},
              "state": "complete|incomplete|unknown", "window": {"start": "...", "end": "...", "statistic": "mean|p95|max"},
              "kind": "measured|stale_last_known|nameplate_estimate", "uncertainty": {"kind": "unknown"}}
```

A rack whose load is `incomplete` or `unknown` (per the #105/#130 completeness logic) is not silently set to a number. The
scenario may supply an explicit what-if value labelled `scenario_override`. The run report names every override. Double
counting: the heat of rack-mounted equipment is counted once, at the rack; floor-standing equipment without a position blocks
a zone-level model (it is `unattributed`), as in the thermal capacity view. Redundant A/B inputs count once.

`airflow`:

```json
"airflow": {"volumetric": {"value": "0.35", "unit": "m3/s"}, "kind": "measured|catalog|configured|assumption|unknown",
            "provenance": {"ref": "..."}, "temperature_derived": false}
```

`temperature_derived` is always false. If `kind` is `unknown` the rack cannot be modelled as a flow-driven source. The
assembler offers two outcomes only: refuse, or accept a scenario-level declared airflow assumption (kind `assumption`) which
makes the run non-validatable.

### 5.3 Cooling unit

`id`, `asset_id`, `kind`, `footprint` and `elevation` (currently unavailable), `supply_surface` and `return_surface` as
polygons with outward normals, `supply_temperature`, `supply_flow`, `operating_state`, `rated_capacity`, `configured_capacity`.

Operating state comes from `cooling_unit.operating_status`. A unit with state `unknown` makes assembly refuse unless the
scenario states the state. `rated_capacity` and `configured_capacity` are recorded as `capacity_limit_w` for a post-run
check. They are not boundary conditions (section 7).

### 5.4 Other geometry

Walls, columns, obstacles (with vertical extent, currently unavailable), plenum (depth, tile layout, perforation, leakage
areas), containment (boundary, roof, doors, openings, leakage areas), each as surfaces with `kind`, polygon in solver frame,
outward normal, `thermal` (adiabatic or heat transfer coefficient) and provenance. Each geometry object carries
`confidence` from calibration (`high`, `medium`, `low`) and `error_bound_m`. Objects with `low` confidence or an unknown
bound are listed in the report and block validation.

## 6. Boundary surfaces

Each boundary patch is declared once:

```json
{"patch_id": "cu-01-supply", "role": "supply|return|leakage|wall|rack_inlet|rack_exhaust|opening",
 "surface": "<polygon in solver frame>", "outward_normal": [0, 0, -1], "area_m2": "1.44",
 "momentum": {"type": "flow_rate|pressure|zero_gradient|no_slip",  "value": {...}},
 "thermal":  {"type": "fixed_temperature|adiabatic|inflow_temperature|heat_flux|coefficient", "value": {...}},
 "set_by": "equipment_state|scenario|assumption|measurement"}
```

Normals are checked against the transform: they point out of the fluid domain.

## 7. Compatibility and over-constraint rules (assembly refuses on violation)

1. Mass conservation: every connected fluid domain needs at least one pressure-type boundary (return, leakage or
   opening) so the pressure level is defined. If every boundary of a domain prescribes `flow_rate`, the contract is
   over-constrained and rejected, even when the prescribed flows happen to sum to zero.
2. A patch has exactly one momentum and one thermal type. Temperature on an outflow face is zero-gradient or inflow-only
   (back-flow temperature), never a fixed value.
3. A supply surface with `flow_rate` needs a temperature. A return surface with a fixed temperature is rejected.
4. Cooling capacity is not a boundary condition. The assembler never sets a heat-removal value from rated capacity.
   The supply flow and supply temperature define the heat the unit can carry; the solver reports the heat removed,
   and the post-process compares it with `capacity_limit_w` and warns on exceedance.
5. Rack inlet and exhaust faces use the rack's porous or fan model; a rack with `airflow.kind = unknown` is refused.
6. Heat removed by return surface enthalpy flux must be computable from the output patches; the contract requires all
   boundary patches to exist in the field output.
7. Closed domain: net flux of all patches is closed by pressure patches or leakage patches; a sealed room with only
   prescribed flow patches is rejected.
8. Scenario deltas can change load, airflow, unit state, rack position and supply setpoint within declared ranges. They
   cannot introduce a geometry object that violates the calibrated room boundary or collides with another object. Collision
   is checked in solver frame with the geometry error bound added.

## 8. Fluid, turbulence and wall assumptions

```json
"fluid": {"name": "air", "model": "ideal_gas_variable_density|boussinesq", "reference_pressure": {...},
          "cp": {...}, "mu": {...}, "pr": {...}, "prt": {...}, "gravity_m_s2": [0,0,-9.81]},
"turbulence": {"model": "kOmegaSST", "wall_treatment": "wall_function", "intensity": {...}, "length_scale": {...}},
"walls": {"default_thermal": "adiabatic", "roughness": {...}},
"radiation": {"enabled": false},
"assumptions": [{"id": "A1", "text": "...", "scope": "global|object", "ref": "..."}]
```

Every assumption is listed and carried into the operator report. The solver and template versions are fields of the
document (section 11).

## 9. Sensor datasets and comparison windows

```json
"sensor_validation": {
  "dataset_id": "<uuid>", "split": {"method": "hash_stratified", "seed_ref": "<owner-approved id>", "calibration": ["..."], "holdout": ["..."]},
  "window": {"start": "...", "end": "...", "min_duration_s": 900, "stationarity": {"method": "slope_and_range", "max_slope_k_per_h": "0.5"}},
  "sensors": [{"asset_id": "...", "role": "rack_inlet", "position_m": [...], "position_error_m": "...",
               "metric": "temperature_c", "accuracy": {"value": "0.5", "unit": "K", "kind": "unknown|datasheet|calibration_certificate"},
               "calibration_date": null, "readings_ref": {"query": "...", "count": 0, "provenance_status_counts": {}},
               "statistic": "median", "freshness_policy": "classify_reading/1"}]
}
```

The window and eligibility rules are frozen when the dataset is created. Sensors with unknown accuracy or provenance status
`unverified_legacy`/`unverified_registry` are ineligible for validation (they may still be listed as informational).

## 10. Deterministic serialization and hashes

1. Encode as JSON with RFC 8785 canonical rules: UTF-8, sorted keys, no insignificant whitespace, decimal strings for
   numbers with a fixed scale per unit, RFC 3339 UTC timestamps with `Z`, lowercase UUIDs.
2. Arrays that represent sets (for example `placement_refs`, patches) are sorted by their identifier.
3. `content_hash = SHA-256(canonical bytes)` over the document with the `hashes` object removed. Sub-hashes are stored for
   `geometry`, `heat_loads`, `boundaries`, `fluid`, `sensor_validation` so a reviewer can see which section changed.
4. `case_hash` is the hash of the generated solver case tree (sorted relative paths and file hashes) and is stored on the run.
5. Two assemblies of the same revisions in the same database state produce identical bytes and hashes. Test in M2.

## 11. Solver, mesh and convergence-policy versions

```json
"execution_policy": {"solver": {"distribution": "<pinned>", "release": "<pinned>", "image_digest": "sha256:<recorded in M3>",
                                 "template_version": "<semver>", "executables": ["..."]},
                     "mesh": {"policy_version": "mesh/1", "levels": 3, "refinement_ratio_min": "1.3",
                              "max_cells": 8000000},
                     "convergence": {"policy_version": "conv/1"},
                     "limits": {"policy_version": "limits/1"}},
"hashes": {"content": "<computed>", "geometry": "<computed>", "heat_loads": "<computed>", "boundaries": "<computed>"}
```

## 12. Example outline (baseline, abbreviated)

```json
{"contract_version": "cfd-input/1", "model_revision_id": "<uuid>", "baseline_revision_id": null, "scenario_revision_id": null,
 "site_id": "<uuid>", "room_id": "<uuid>", "calibration_id": "<uuid>",
 "frame": {"transform_id": "dcim-plan-to-solver/1"},
 "room": {"height": {"value": "<required>", "unit": "m", "provenance": {"kind": "<required>"}}},
 "racks": [{"id": "r1", "heat_load": {"state": "complete"}, "airflow": {"kind": "<required>"}}],
 "cooling_units": [{"id": "cu1", "operating_state": "online"}],
 "patches": [], "fluid": {}, "turbulence": {}, "sensor_validation": {}, "execution_policy": {}, "hashes": {}}
```

A scenario example changes one rack's `heat_load.power` with `provenance.kind = scenario_override` and lists the delta in
`scenario.deltas[]` with `{path, from, to, reason}`. The base document is unchanged; the scenario document is a separate
immutable revision with its own hash.

## 13. Assembly refusal reasons (closed list, tested)

`room_height_unknown`, `rack_height_unknown`, `cooling_geometry_unknown`, `containment_not_3d`, `obstacle_height_unknown`,
`rack_airflow_unknown`, `rack_load_incomplete`, `floor_equipment_unattributed`, `cooling_state_unknown`,
`boundary_over_constrained`, `domain_not_closed`, `geometry_low_confidence`, `scope_partial_visibility`,
`registry_version_mismatch`, `calibration_missing`, `plan_inactive`. Each refusal names the objects involved and, if the caller
may see them, their ids.
