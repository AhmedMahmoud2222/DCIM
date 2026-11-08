import type { CoolingLayoutZone, HeatMap, HeatMapGrid, HeatMapState, SensorValueState, ThermalMetric, ThermalSensorPoint } from "@/types";

export type Point = [number, number];

/** Presentation rules for cooling / environment data. Pure functions, no React, so they are unit tested directly. */

/** Never relies on colour alone: every state also has a glyph and a text label. */
export const SENSOR_STATE_META: Record<SensorValueState, { label: string; glyph: string; colour: string; description: string }> = {
  measured_fresh: { label: "Measured, fresh", glyph: "●", colour: "#22c55e", description: "Measured value within three poll intervals" },
  measured_stale: { label: "Measured, stale", glyph: "◌", colour: "#f59e0b", description: "Last measured value is older than three poll intervals" },
  missing: { label: "Missing", glyph: "✕", colour: "#ef4444", description: "No reading received from this sensor" },
  invalid: { label: "Invalid", glyph: "⚠", colour: "#a78bfa", description: "Reading rejected: unit or timestamp cannot be trusted" },
};

export const MAP_STATE_META: Record<HeatMapState, { label: string; glyph: string; classes: string }> = {
  healthy: { label: "Healthy", glyph: "✓", classes: "bg-green-900 text-green-100" },
  partial: { label: "Partial", glyph: "!", classes: "bg-yellow-900 text-yellow-100" },
  degraded: { label: "Degraded", glyph: "!", classes: "bg-orange-900 text-orange-100" },
  unavailable: { label: "Unavailable", glyph: "✕", classes: "bg-red-900 text-red-100" },
};

const REASONS: Record<string, string> = {
  no_active_floor_plan: "This room has no active floor plan, so a map cannot be placed.",
  no_calibration: "The active floor plan is not calibrated, so coordinates cannot be trusted.",
  no_room_extent: "No room boundary or recorded size exists to interpolate over.",
  insufficient_fresh_sensors: "Fewer than three fresh, located sensors are available; no field is drawn.",
  age_skew_exceeded: "Fresh readings are too far apart in time to form one coherent snapshot; no field is drawn.",
  sensor_limit_exceeded: "More sensors than the map limit contribute; no field is drawn.",
  some_expected_sensors_not_contributing: "Some expected sensors are stale, missing, invalid or unlocated and are not contributing.",
  sensor_list_truncated: "The sensor list was truncated at the safety limit.",
};
export const explainReason = (code: string): string => REASONS[code] ?? code.replace(/_/g, " ");

const EXCLUDED: Record<string, string> = {
  no_location: "no position",
  outside_room_boundary: "outside the room",
  measured_stale: "stale",
  missing: "missing",
  invalid: "invalid",
};
export const explainExclusion = (code: string | null): string => (code ? (EXCLUDED[code] ?? code.replace(/_/g, " ")) : "");

type Rgb = [number, number, number];
const TEMPERATURE_RAMP: Rgb[] = [[59, 130, 246], [34, 197, 94], [250, 204, 21], [239, 68, 68]];
const HUMIDITY_RAMP: Rgb[] = [[254, 243, 199], [125, 211, 252], [37, 99, 235], [30, 27, 75]];

export const clamp01 = (t: number): number => (Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0);

export function ramp(metric: ThermalMetric): Rgb[] {
  return metric === "humidity_percent" ? HUMIDITY_RAMP : TEMPERATURE_RAMP;
}

/** Linear interpolation along the metric's colour ramp; t is clamped to [0, 1]. */
export function heatColour(metric: ThermalMetric, t: number): string {
  const stops = ramp(metric);
  const scaled = clamp01(t) * (stops.length - 1);
  const i = Math.min(Math.floor(scaled), stops.length - 2);
  const f = scaled - i;
  const [r, g, b] = stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
  return `rgb(${r} ${g} ${b})`;
}

export function normalise(value: number, min: number, max: number): number {
  return max > min ? clamp01((value - min) / (max - min)) : 0.5;
}

/** Colour scale bounds shared by cells and sensor markers, so a marker and the cell beneath it agree. */
export function scaleBounds(map: HeatMap): { min: number; max: number } | null {
  const values = [...(map.grid && map.grid.min != null && map.grid.max != null ? [map.grid.min, map.grid.max] : []), ...map.sensors.filter((s) => s.state === "measured_fresh" && s.value != null).map((s) => s.value as number)];
  if (values.length === 0) return null;
  return { min: Math.min(...values), max: Math.max(...values) };
}

export interface CellRect {
  index: number;
  column: number;
  row: number;
  xMm: number;
  yMm: number;
  sizeMm: number;
  value: number;
  support: number;
}

/** Cells that carry a value, with their canonical-millimetre rectangle. Empty cells (no coverage / outside room) are skipped. */
export function cellRects(grid: HeatMapGrid): CellRect[] {
  const out: CellRect[] = [];
  grid.values.forEach((value, index) => {
    if (value == null) return;
    const column = index % grid.columns;
    const row = Math.floor(index / grid.columns);
    out.push({ index, column, row, xMm: grid.origin_x_mm + column * grid.cell_mm, yMm: grid.origin_y_mm + row * grid.cell_mm, sizeMm: grid.cell_mm, value, support: grid.support[index] ?? 0 });
  });
  return out;
}

export function cellAt(grid: HeatMapGrid, xMm: number, yMm: number): CellRect | null {
  const column = Math.floor((xMm - grid.origin_x_mm) / grid.cell_mm);
  const row = Math.floor((yMm - grid.origin_y_mm) / grid.cell_mm);
  if (column < 0 || row < 0 || column >= grid.columns || row >= grid.rows) return null;
  const index = row * grid.columns + column;
  const value = grid.values[index];
  if (value == null) return null;
  return { index, column, row, xMm: grid.origin_x_mm + column * grid.cell_mm, yMm: grid.origin_y_mm + row * grid.cell_mm, sizeMm: grid.cell_mm, value, support: grid.support[index] ?? 0 };
}

export const legendTicks = (min: number, max: number, count = 5): number[] =>
  Array.from({ length: count }, (_, i) => Math.round((min + ((max - min) * i) / (count - 1)) * 100) / 100);

export function formatQuantity(value: number | null | undefined, unit: string | null | undefined, digits = 1): string {
  if (value == null) return "no value";
  return `${Number(value.toFixed(digits))}${unit ? ` ${unit}` : ""}`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return "never";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

/** Text used for aria-labels and tooltips: state first, then value and age, then why it is or is not used. */
export function describeSensor(p: ThermalSensorPoint): string {
  const meta = SENSOR_STATE_META[p.state];
  const value = p.presentation_value != null ? formatQuantity(p.presentation_value, p.presentation_unit) : "no value";
  const where = p.x_mm == null ? "no position" : `at ${Math.round(p.x_mm)}, ${Math.round(p.y_mm ?? 0)} mm${p.position_exact ? "" : " (approximate, rack footprint)"}`;
  const used = p.used_in_field ? "used in the interpolated field" : `not used in the field${p.excluded_reason ? ` (${explainExclusion(p.excluded_reason)})` : ""}`;
  return `${p.name}: ${meta.label}, ${value}, ${formatAge(p.age_seconds)}, ${where}; ${used}`;
}

export const metricLabel = (metric: ThermalMetric): string => (metric === "humidity_percent" ? "Humidity" : "Temperature");

/** Which threshold bands (from the existing alarm rules) a value falls outside. */
export function thresholdBreach(map: HeatMap, value: number): "high" | "low" | null {
  for (const t of map.thresholds) {
    if (t.rule_type === "threshold_high" && value >= t.threshold) return "high";
    if (t.rule_type === "threshold_low" && value <= t.threshold) return "low";
  }
  return null;
}

export const bearingToVector = (deg: number, lengthPx: number): [number, number] => {
  const rad = (deg * Math.PI) / 180;
  return [Math.cos(rad) * lengthPx, Math.sin(rad) * lengthPx];
};

export function zonePoints(zone: CoolingLayoutZone): Point[] | null {
  if (zone.geometry_type === "rect" && zone.x_mm != null && zone.y_mm != null && zone.width_mm != null && zone.height_mm != null) {
    return [[zone.x_mm, zone.y_mm], [zone.x_mm + zone.width_mm, zone.y_mm], [zone.x_mm + zone.width_mm, zone.y_mm + zone.height_mm], [zone.x_mm, zone.y_mm + zone.height_mm]];
  }
  if (zone.geometry_type === "polygon" && zone.points && zone.points.length >= 3) return zone.points.map((p) => [p[0], p[1]] as Point);
  return null;
}

