# Issue #105: cooling model, environmental heat maps and airflow visualization

Document version: 1.0
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

Catalog (manufacturer/model) linkage for cooling units is deferred: the catalog has no cooling model type yet, so unit
footprints are not modelled and markers are drawn unscaled and labelled as such.

## 2. Canonical metrics and units (registry version 1, additive)
`supply_air_temperature_c`, `return_air_temperature_c` (degC), `airflow_m3_s` (m3/s, shown m3/h), `airflow_velocity_m_s` (m/s),
`differential_pressure_pa` (Pa), `cooling_output_kw` (kW). New units: m3/s, m3/h, L/s, CFM, m/s, ft/min, km/h, Pa, kPa, inH2O.
Existing keys and stored rows are unchanged; raw value and unit provenance is kept by ingestion. A sensor can only be mapped to
metrics its kind measures.

## 3. Current-value contract and freshness
One function (`classify_reading`) decides freshness for every view, including the legacy `environment_overlay`.
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
New codes `cooling:read` (all roles) and `cooling:manage` (Administrator, DCIM Manager, Engineer). Views also need spatial:read /
telemetry:read as listed in `thermal.py`; thermal load needs power:read; alarms need alarm:read; source integration identity
needs integration:read. The cooling codes are not site-scope-aware, so site-restricted callers are refused (fail closed).
Services are nevertheless scope-aware (sensors are filtered with the existing equipment visibility clause before interpolation, counts,
quality and provenance are computed from the visible set only, hidden rooms are 404, restricted scopes get load withheld) and are
attacked directly in tests so a future scope-aware grant cannot leak.

## 9. Concurrency and history
Mutations use If-Match on the subtype `version`. Placement is close-then-open with monotonic versions. Retire is refused while
referenced. Tested with real concurrent sessions: same-unit edits, simultaneous sensor moves, containment edits, retire vs relate,
membership races, maps during moves.

## 10. Deliberately deferred to #106
CFD, predicted airflow, barrier-aware or vertical interpolation, modelled relationships, cooling catalog linkage and footprints.
