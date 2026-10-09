# Issue #105: cooling model, environmental heat maps and airflow visualization

Document version: 1.1
Base: `main@eaf0fa01a7ea9cac263f8fce4d7311ed2ba311f6` (includes #99, #101 to #104). Migration: `0045_cooling_thermal`.

**A #105 heat map is an operational interpolation/visualization, not validated CFD.** Interpolated cells are never
measurements, are never stored, and never written back to telemetry.

## 1. Domain model
All cooling assets reuse the ManagedAsset shared-primary-key pattern; no parallel identity exists.

| Table | Role |
|---|---|
| `cooling_unit` | Subtype for `asset_type` crac / crah / chiller. Composite FK `(id, unit_kind) -> managed_asset(id, asset_type)` makes the database reject a wrong-typed asset. Nullable engineering inputs: rated and configured (derated) kW, airflow m3/s, supply/return design degC, humidity limits, supply direction. NULL means unknown, never zero. |
| `cooling_group` | Units that back each other up. Composite FK keeps unit and group in one site. |
| `environmental_sensor` | Subtype for `asset_type='sensor'` (generated type column + composite FK). Kinds: temperature, humidity, airflow, differential_pressure, combined. Roles: ambient, rack_inlet, rack_exhaust, supply_air, return_air, other. |
| `thermal_zone` | Operator-configured region of one room in canonical mm: served zone (no geometry = whole room), supply/return region, hot/cold aisle with optional containment. Aisle type is never inferred from rack orientation. |
| `containment_element` | Boundary or opening segment of a contained aisle. |
| `cooling_unit_zone` | Typed FK relationship: `serves` (served_zone), `supplies` (supply_region, cold_aisle), `returns_from` (return_region, hot_aisle) with semantics authoritative / configured / modelled. |

Placement uses the shared `equipment_placement` (close-then-open history). It gained nullable `x_mm`, `y_mm`,
`position_calibration_id`; each row keeps its own coordinates. Placement versions are now monotonic per asset (ABA safe).
A position needs an active, calibrated floor plan and must lie inside the approved room boundary. Rack-mounted sensors take
their position from the rack footprint centre and are reported as approximate (`position_exact=false`).
Database triggers enforce: placed asset and room share a site; unit and zone share a site; a unit still related to a zone
cannot be decommissioned or removed (also through the generic lifecycle endpoint); relationships take share locks so a
concurrent retire cannot interleave with a new relationship.

Catalog (manufacturer/model) linkage for cooling units is deferred: the catalog has no cooling model type yet. No footprint is
invented: units appear as fixed-size markers that are explicitly labelled "footprint not modelled, not to scale" in the 2D and 3D views,
in their tooltips and in the details panel, and are never drawn as dimensionally accurate equipment.

## 2. Canonical metrics and units (registry version 1, additive)
`supply_air_temperature_c`, `return_air_temperature_c` (degC), `airflow_m3_s` (m3/s, shown m3/h), `airflow_velocity_m_s` (m/s),
`differential_pressure_pa` (Pa), `cooling_output_kw` (kW). New units: m3/s, m3/h, L/s, CFM, m/s, ft/min, km/h, Pa, kPa, inH2O.
Existing keys and stored rows are unchanged; raw value and unit provenance is kept by ingestion. A sensor can only be mapped to
metrics its kind measures.

## 3. Current-value contract and freshness
One function (`classify_reading`) decides freshness for every view, including the #104 `environment_overlay` (which also flags a non-canonical stored unit as invalid, like the thermal views); `test_environment_freshness_policy.py` pins both views to the same answers at the 3 x poll boundary and for different poll intervals.
fresh: age <= max(poll interval, 1) x 3. stale: older. missing: placed, expected, no reading received by the snapshot instant.
invalid: unit differs from the canonical unit (never reinterpreted) or timestamp more than 120 s in the future.
A snapshot (`as_of`, default now) considers readings received by that instant and judges freshness at that instant.
Planned/reserved sensors are not expected to report; decommissioned/removed ones are retired.

## 4. Heat-map method
Inverse-distance weighting, power 2, over fresh, located sensors inside the room, in canonical mm, one room only.
A cell is filled only when at least 2 sensors lie within the influence radius (default 6000 mm, bounds 500 to 20000);
a field needs at least 3 sensors. Stale, missing, invalid and unlocated sensors are excluded, not down-weighted.
Output is deterministic (sensors sorted by id, fixed scan order, 2 decimals) and never leaves the range of its contributors.
Limits: 128 sensors, grid at most 80 x 80 cells (cell size raised automatically), cell size at least 100 mm.
Known limits: 2D only (no height or stratification), no barrier awareness (walls, containment and racks do not block
influence), humidity is interpolated directly (relative humidity depends on temperature).
Map state: healthy (every expected sensor fresh and contributing), partial (field drawn, some sensors not contributing),
degraded (fresh readings further apart than the allowed skew, default 900 s, bounds 60 to 3600; no field), unavailable (no
calibrated plan or extent, or fewer than 3 fresh sensors). The response carries generated_at, as_of, source time range, skew,
counts, coverage percent/class, method, assumptions and the contributing sensor ids. Threshold bands come from existing alarm rules.
Default sensor roles exclude rack_exhaust, supply_air and return_air so hot and cold populations are not blended.

## 5. Airflow
Only inputs that exist are drawn. Cooling units: configured direction and configured design airflow (provenance
`configured_design`). Airflow sensors: measured magnitude with freshness, configured orientation. Elements missing a direction,
magnitude or position are listed with `drawable=false` and a reason. `modelled` provenance is reserved for #106 and never produced.
Arrows are static, so there is nothing to suppress for reduced motion.

## 6. Capacity, headroom and redundancy
Air-side capacity is CRAC + CRAH. Available = unit lifecycle installed/active AND operating status online/standby; effective
capacity = configured if set, else rated; unknown stays unknown and makes dependent totals "incomplete" (headroom is then withheld).
An operating status of `unknown` is missing information, not a unit that is down: the zone's available capacity is then unknown (null), the
level is `unknown` and the pool reports `availability_unknown`; only units all *known* to be down give a real 0 kW and `no_available_unit`.
If no available unit has a known capacity, available and installed totals are null, never 0.
Chillers are reported separately (`plant`) and never added to room headroom (they feed CRAH coils).
Thermal load = #102 electrical IT load x 1.0, stated in every result; no COP, PUE or efficiency factor. Zones with geometry
count racks whose footprint centre is inside; unassigned or load-less racks make the load `incomplete`.
Headroom = available - thermal load. Utilisation warns at 80 %, critical at 95 % or negative headroom.
Redundancy pool = cooling-group members (load = every zone the group serves, counted once) or the zone's ungrouped units:
not_configured, unavailable (none available), single_unit, degraded (a member unavailable, or load exceeds capacity after
losing the largest available unit), redundant (N+1 verified), redundant_unverified (capacity or load unknown).

## 7. Exceptions
Persistent alarms are read from the existing alarm engine (no second alarm store, no new rule types: rules on any registry
metric already work). Derived on request, never persisted: stale/missing/invalid sensor data, unlocated sensor, unavailable
cooling unit, headroom breach, redundancy lost, single unit, incomplete capacity inputs, no cooling assigned, zone without fresh sensors.

## 8. Authorization
Codes: `cooling:read` (all roles) and `cooling:manage` (Administrator, DCIM Manager, Engineer). Both are in
`SCOPE_AWARE_PERMISSIONS`, so a site- or rack-scoped user keeps them and reaches the endpoints; every route is listed in the
reviewed route sets of `test_user_groups_authz.py` and `test_pr69_route_sweep_strict.py`.

| Scope of the caller | What the endpoints return |
|---|---|
| Unrestricted | everything |
| Whole site (`rack_scope=all`) | that site's units, groups, zones, relations, sensors, capacity (with thermal load), maps; another site's room, sensor, unit, zone or group is a 404, identical to a non-existent id |
| Selected racks | only sensors the equipment visibility clause shows (rack-mounted in a granted rack); maps, counts, min/max, quality and provenance are computed from those sensors only; site-level configuration (units, groups, zones, relations, capacity, airflow units) is invisible; no write operation |

Filtering happens in SQL while the sensor set is selected, before interpolation, counting or aggregation, so a hidden sensor cannot be
inferred from a map. Writes need the whole site. `spatial:read`, `telemetry:read`, `power:read` and `alarm:read` are not scope-aware;
inside the thermal views a granted but scope-inactive code counts (the data behind it is already scope-filtered), while an explicit
deny or a missing grant still blocks. `integration:read` (source identity in responses) is never taken from the inactive set.
Hidden-site and hidden-rack cases are tested through HTTP (`test_thermal_scope.py`) and directly on the services (`test_thermal_authz.py`).

## 9. Concurrency and history
Mutations use If-Match on the subtype `version`. Placement is close-then-open with monotonic versions. Retire is refused while
referenced. Tested with real concurrent sessions: same-unit edits, simultaneous sensor moves, containment edits, retire vs relate,
membership races, maps during moves.

## 10. Deliberately deferred to #106
CFD, predicted airflow, barrier-aware or vertical interpolation, modelled relationships, cooling catalog linkage and footprints.
