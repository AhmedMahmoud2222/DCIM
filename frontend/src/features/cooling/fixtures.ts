import type { CapacityZone, CoolingLayout, HeatMap, RoomAirflow, RoomCapacity, RoomExceptions, RoomSpatialView, ThermalSensorPoint } from "@/types";

/** Deterministic fixtures for the Issue #105 component tests. */
export const spatialView: RoomSpatialView = {
  room_id: "room-1", room_name: "Hall A", active_floor_plan_id: "fp", active_floor_plan_revision: 1, room_width_mm: 6000, room_height_mm: 4000,
  generated_at: "2026-10-09T10:00:00Z", racks: [], equipment: [], objects: [], rack_equipment: [], layout_state: "validated", incomplete_reasons: [],
  calibration: { id: "c", method: "declared_units", source_units: "mm", mm_per_unit: 1, error_bound_mm: 1, relative_error: 0, confidence: "high", created_at: "" },
  boundary: { id: "b", object_type: "room_outline", geometry_type: "rect", x_mm: 0, y_mm: 0, width_mm: 6000, height_mm: 4000, rotation_deg: 0, geometry_data: null, label: null, source: "authoritative" },
};

export const sensor = (over: Partial<ThermalSensorPoint> = {}): ThermalSensorPoint => ({
  sensor_id: "s1", asset_tag: "SN-1", name: "Inlet 1", sensor_kind: "temperature", measurement_role: "ambient", metric: "temperature_c",
  placement_type: "floor_standing", rack_id: null, x_mm: 1000, y_mm: 1000, position_source: "placement", position_exact: true, state: "measured_fresh",
  value_provenance: "measured", value: 20, unit: "degC", presentation_value: 20, presentation_unit: "degC", occurred_at: "2026-10-09T09:59:50Z", age_seconds: 10,
  expected_poll_interval_seconds: 60, invalid_reason: null, active_alarm_count: 0, used_in_field: true, excluded_reason: null, source: null, ...over,
});

const columns = 12;
const rows = 8;
const values = Array.from({ length: columns * rows }, (_, i) => (i % columns === 11 ? null : 20 + (i % columns) * 0.9));

export const heatMap = (over: Partial<HeatMap> = {}): HeatMap => ({
  room_id: "room-1", room_name: "Hall A", metric: "temperature_c", unit: "degC", presentation_unit: "degC", kind: "interpolated_operational_estimate",
  disclaimer: "Operational interpolation of sensor readings for visualisation. It is not validated CFD, not a measured field, and interpolated cells must not be treated as measurements.",
  generated_at: "2026-10-09T10:00:00Z", as_of: "2026-10-09T10:00:00Z", floor_plan_id: "fp", floor_plan_revision: 1, calibration_id: "c", state: "healthy", state_reasons: [],
  quality: {
    sensor_count: 4, fresh_count: 4, stale_count: 0, missing_count: 0, invalid_count: 0, unlocated_count: 0, contributing_count: 4, coverage_percent: 91.7,
    coverage_class: "good", interpolation_coverage_percent: 91.7, source_time_range: { oldest: "2026-10-09T09:59:50Z", newest: "2026-10-09T09:59:55Z" },
    max_age_skew_seconds: 5, max_age_skew_allowed_seconds: 900, freshness_cutoff: "3 x integration poll interval",
  },
  method: {
    name: "idw", power: 2, radius_mm: 6000, cell_mm: 500, min_sensors: 3, min_neighbours_per_cell: 2, stale_policy: "excluded", missing_policy: "excluded",
    barrier_aware: false, dimensions: 2, assumptions: ["The field is two-dimensional.", "Walls, containment and racks do not block influence."],
  },
  grid: { value_provenance: "interpolated", origin_x_mm: 0, origin_y_mm: 0, cell_mm: 500, columns, rows, unit: "degC", min: 20, max: 29.9, values, support: values.map((v) => (v == null ? 0 : 3)) },
  sensors: [
    sensor({ sensor_id: "s1", name: "Inlet 1", x_mm: 1000, y_mm: 1000, value: 20, presentation_value: 20 }),
    sensor({ sensor_id: "s2", name: "Inlet 2", x_mm: 5000, y_mm: 1000, value: 29, presentation_value: 29 }),
    sensor({ sensor_id: "s3", name: "Inlet 3", x_mm: 1000, y_mm: 3000, value: 24, presentation_value: 24 }),
    sensor({ sensor_id: "s4", name: "Inlet 4", x_mm: 5000, y_mm: 3000, value: 26, presentation_value: 26 }),
  ],
  source_set: { contributing_sensor_ids: ["s1", "s2", "s3", "s4"], as_of: "2026-10-09T10:00:00Z" }, thresholds: [{ rule_type: "threshold_high", threshold: 27, unit: "degC", name: "High inlet" }],
  truncated: false, ...over,
});

export const partialMap = (): HeatMap =>
  heatMap({
    state: "partial", state_reasons: ["some_expected_sensors_not_contributing"],
    quality: { ...heatMap().quality, fresh_count: 2, stale_count: 1, missing_count: 1, contributing_count: 2 },
    sensors: [
      sensor({ sensor_id: "s1", name: "Inlet 1" }),
      sensor({ sensor_id: "s2", name: "Inlet 2", x_mm: 5000, value: 29, presentation_value: 29 }),
      sensor({ sensor_id: "s3", name: "Stale 3", x_mm: 1000, y_mm: 3000, state: "measured_stale", age_seconds: 900, used_in_field: false, excluded_reason: "measured_stale", value: 24, presentation_value: 24 }),
      sensor({ sensor_id: "s4", name: "Missing 4", x_mm: 5000, y_mm: 3000, state: "missing", value: null, presentation_value: null, presentation_unit: null, unit: null, occurred_at: null, age_seconds: null, used_in_field: false, excluded_reason: "missing", value_provenance: "none" }),
    ],
  });

export const unavailableMap = (): HeatMap =>
  heatMap({
    state: "unavailable", state_reasons: ["insufficient_fresh_sensors"], grid: null,
    quality: { ...heatMap().quality, fresh_count: 1, stale_count: 3, contributing_count: 0, coverage_percent: 0, coverage_class: "none", interpolation_coverage_percent: 0 },
    sensors: [
      sensor({ sensor_id: "s1", name: "Inlet 1" }),
      ...["s2", "s3", "s4"].map((id) => sensor({ sensor_id: id, name: `Stale ${id}`, state: "measured_stale", age_seconds: 4000, used_in_field: false, excluded_reason: "measured_stale" })),
    ],
  });

export const layout: CoolingLayout = {
  room_id: "room-1", room_name: "Hall A", generated_at: "2026-10-09T10:00:00Z", floor_plan_id: "fp", calibration_id: "c", layout_reasons: [],
  zones: [
    { id: "z1", name: "Cold 1", zone_kind: "cold_aisle", containment: "none", geometry_type: "rect", x_mm: 500, y_mm: 500, width_mm: 1200, height_mm: 3000, points: null, authority: "operator_configured", elements: [] },
    {
      id: "z2", name: "Hot 1", zone_kind: "hot_aisle", containment: "contained", geometry_type: "rect", x_mm: 2000, y_mm: 500, width_mm: 1200, height_mm: 3000, points: null, authority: "operator_configured",
      elements: [
        { id: "e1", element_kind: "boundary", x1_mm: 2000, y1_mm: 500, x2_mm: 2000, y2_mm: 3500, label: null },
        { id: "e2", element_kind: "opening", x1_mm: 2000, y1_mm: 1000, x2_mm: 2000, y2_mm: 1500, label: "door" },
      ],
    },
  ],
  cooling_units: [
    { id: "u1", asset_tag: "CU-1", name: "CRAH-1", unit_kind: "crah", operating_status: "online", lifecycle_status: "active", x_mm: 500, y_mm: 3600, rotation_deg: 0, supply_direction_deg: 270, rated_cooling_capacity_kw: 80 },
    { id: "u2", asset_tag: "CU-2", name: "CRAC-2", unit_kind: "crac", operating_status: "fault", lifecycle_status: "active", x_mm: 5500, y_mm: 3600, rotation_deg: 0, supply_direction_deg: null, rated_cooling_capacity_kw: null },
  ],
  sensors: [], relations: [],
};

export const airflow: RoomAirflow = {
  room_id: "room-1", generated_at: "2026-10-09T10:00:00Z",
  elements: [
    { id: "u1", kind: "cooling_supply", name: "CRAH-1", unit_kind: "crah", x_mm: 500, y_mm: 3600, position_exact: true, direction_deg: 270, direction_provenance: "configured", magnitude_provenance: "configured_design", magnitude_m3_s: 4.5, state: "configured", drawable: true, not_drawable_reasons: [] },
    { id: "u2", kind: "cooling_supply", name: "CRAC-2", unit_kind: "crac", x_mm: 5500, y_mm: 3600, position_exact: true, direction_deg: null, direction_provenance: "configured", magnitude_provenance: "none", magnitude_m3_s: null, state: "configured", drawable: false, not_drawable_reasons: ["no_configured_direction", "no_design_airflow"] },
    { id: "f1", kind: "sensor_airflow", name: "Flow 1", x_mm: 3000, y_mm: 2000, position_exact: true, direction_deg: 90, direction_provenance: "configured", magnitude_provenance: "measured", volume_flow: sensor({ sensor_id: "f1", name: "Flow 1", metric: "airflow_m3_s", value: 1.25, presentation_value: 4500, presentation_unit: "m3/h", unit: "m3/s" }), velocity: null, state: "measured_fresh", drawable: true, not_drawable_reasons: [] },
  ],
  provenance_summary: { measured_magnitude: 1, configured_design: 1, modelled: 0, not_drawable: 1 },
  note: "No modelled airflow is generated in Issue #105. Streamlines and predicted flow belong to the CFD work package (#106).",
  disclaimer: "Arrows show configured direction and either measured or design magnitude at a point. They are not a CFD result.",
};

export const zone = (over: Partial<CapacityZone> = {}): CapacityZone => ({
  zone_id: "sz1", name: "Hall", geometry: "whole_room",
  units: [{ id: "u1", name: "CRAH-1", kind: "crah", lifecycle_status: "active", operating_status: "online", available: true, unavailable_reason: null, rated_kw: 80, configured_kw: null, effective_kw: 80, capacity_known: true }],
  installed_rated_kw: 160, installed_rated_complete: true, available_kw: 160, available_complete: true, available_units: 2, unit_count: 2,
  thermal_load: { state: "known", electrical_kw: 60, thermal_kw: 60, quality: "measured", rack_count: 4, unassigned_rack_count: 0, missing_load_rack_count: 0, basis: "whole room", assumption: "x", electrical_to_thermal_factor: 1 },
  headroom_kw: 100, utilization_pct: 37.5, level: "ok", redundancy: { state: "redundant", pools: [{ scope: "zone_units", reason: null, n_plus_1_verified: true }] },
  plant: { note: "", units: [], installed_rated_kw: null, available_kw: null }, ...over,
});

export const capacity = (zones: CapacityZone[] = [zone()]): RoomCapacity => ({
  room_id: "room-1", generated_at: "2026-10-09T10:00:00Z", state: "ok", reasons: [], zones,
  load_assumption: "Thermal load = electrical IT load x 1.0 (all IT input power becomes heat). No COP, PUE or efficiency factor is applied.",
});

export const exceptions = (items: RoomExceptions["items"] = []): RoomExceptions => ({
  room_id: "room-1", generated_at: "2026-10-09T10:00:00Z", items, counts: items.reduce<Record<string, number>>((a, i) => ({ ...a, [i.severity]: (a[i.severity] ?? 0) + 1 }), {}), truncated: false,
});
