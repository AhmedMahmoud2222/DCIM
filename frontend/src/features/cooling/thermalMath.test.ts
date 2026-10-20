import { describe, expect, it } from "vitest";

import type { HeatMap, HeatMapGrid, ThermalSensorPoint } from "@/types";
import {
  MAP_STATE_META,
  SENSOR_STATE_META,
  bearingToVector,
  cellAt,
  cellRects,
  describeSensor,
  explainReason,
  formatAge,
  formatQuantity,
  heatColour,
  legendTicks,
  normalise,
  scaleBounds,
  thresholdBreach,
} from "./thermalMath";

const grid: HeatMapGrid = {
  value_provenance: "interpolated", origin_x_mm: 0, origin_y_mm: 0, cell_mm: 500, columns: 3, rows: 2, unit: "degC", min: 20, max: 24,
  values: [20, 21, null, 22, null, 24], support: [3, 3, 0, 2, 0, 4],
};
const sensor = (over: Partial<ThermalSensorPoint> = {}): ThermalSensorPoint => ({
  sensor_id: "s1", asset_tag: "SN-1", name: "Inlet 1", sensor_kind: "temperature", measurement_role: "ambient", metric: "temperature_c",
  placement_type: "floor_standing", rack_id: null, x_mm: 1000, y_mm: 500, position_source: "placement", position_exact: true,
  state: "measured_fresh", value_provenance: "measured", value: 21.4, unit: "degC", presentation_value: 21.4, presentation_unit: "degC",
  occurred_at: "2026-01-01T00:00:00Z", age_seconds: 12, expected_poll_interval_seconds: 60, invalid_reason: null, active_alarm_count: 0,
  used_in_field: true, excluded_reason: null, source: null, ...over,
});

describe("heat colour scale", () => {
  it("clamps and maps the ends of the ramp to its first and last stops", () => {
    expect(heatColour("temperature_c", -3)).toBe(heatColour("temperature_c", 0));
    expect(heatColour("temperature_c", 7)).toBe(heatColour("temperature_c", 1));
    expect(heatColour("temperature_c", 0)).toBe("rgb(59 130 246)");
    expect(heatColour("temperature_c", 1)).toBe("rgb(239 68 68)");
    expect(heatColour("temperature_c", Number.NaN)).toBe(heatColour("temperature_c", 0));
  });
  it("uses a different ramp for humidity than for temperature", () => {
    expect(heatColour("humidity_percent", 0.5)).not.toBe(heatColour("temperature_c", 0.5));
  });
  it("normalises against the shared bounds, and is neutral for a flat field", () => {
    expect(normalise(22, 20, 24)).toBe(0.5);
    expect(normalise(30, 20, 24)).toBe(1);
    expect(normalise(20, 20, 20)).toBe(0.5);
  });
});

describe("grid helpers", () => {
  it("lists only valued cells, in canonical millimetres", () => {
    const cells = cellRects(grid);
    expect(cells.map((c) => c.index)).toEqual([0, 1, 3, 5]);
    expect(cells[2]).toMatchObject({ column: 0, row: 1, xMm: 0, yMm: 500, sizeMm: 500, value: 22, support: 2 });
  });
  it("finds the cell under a point and returns null for empty or out-of-range cells", () => {
    expect(cellAt(grid, 700, 100)?.value).toBe(21);
    expect(cellAt(grid, 1200, 100)).toBeNull();
    expect(cellAt(grid, -1, 0)).toBeNull();
    expect(cellAt(grid, 5000, 0)).toBeNull();
  });
  it("builds evenly spaced legend ticks", () => {
    expect(legendTicks(20, 30, 5)).toEqual([20, 22.5, 25, 27.5, 30]);
  });
});

describe("text formatting", () => {
  it("formats quantities and never invents a value", () => {
    expect(formatQuantity(21.456, "degC")).toBe("21.5 degC");
    expect(formatQuantity(null, "degC")).toBe("no value");
    expect(formatQuantity(0, "%")).toBe("0 %");
  });
  it("describes ages in readable units", () => {
    expect(formatAge(12)).toBe("12 s ago");
    expect(formatAge(600)).toBe("10 min ago");
    expect(formatAge(7200)).toBe("2 h ago");
    expect(formatAge(null)).toBe("never");
  });
  it("describes a fresh sensor by state, value, age, position and field use", () => {
    expect(describeSensor(sensor())).toBe("Inlet 1: Measured, fresh, 21.4 degC, 12 s ago, at 1000, 500 mm; used in the interpolated field");
  });
  it("describes stale, missing and excluded sensors without presenting them as current", () => {
    expect(describeSensor(sensor({ state: "measured_stale", age_seconds: 900, used_in_field: false, excluded_reason: "measured_stale" }))).toContain("Measured, stale");
    const missing = describeSensor(sensor({ state: "missing", value: null, presentation_value: null, presentation_unit: null, age_seconds: null, used_in_field: false, excluded_reason: "missing" }));
    expect(missing).toContain("Missing, no value, never");
    expect(missing).toContain("not used in the field (missing)");
    expect(describeSensor(sensor({ x_mm: null, y_mm: null, used_in_field: false, excluded_reason: "no_location" }))).toContain("no position");
    expect(describeSensor(sensor({ position_exact: false }))).toContain("approximate, rack footprint");
  });
  it("gives every state a glyph and a label, so colour is never the only cue", () => {
    for (const meta of Object.values(SENSOR_STATE_META)) {
      expect(meta.glyph.length).toBeGreaterThan(0);
      expect(meta.label.length).toBeGreaterThan(3);
    }
    expect(new Set(Object.values(SENSOR_STATE_META).map((m) => m.glyph)).size).toBe(4);
    expect(Object.keys(MAP_STATE_META)).toEqual(["healthy", "partial", "degraded", "unavailable"]);
  });
  it("explains reason codes and falls back to readable text", () => {
    expect(explainReason("age_skew_exceeded")).toMatch(/too far apart in time/);
    expect(explainReason("something_new")).toBe("something new");
  });
});

describe("map level helpers", () => {
  const map = { grid, sensors: [sensor({ value: 18 }), sensor({ value: 30 }), sensor({ state: "measured_stale", value: 99 })], thresholds: [{ rule_type: "threshold_high", threshold: 27, unit: "degC", name: "hot" }, { rule_type: "threshold_low", threshold: 18, unit: "degC", name: "cold" }] } as unknown as HeatMap;
  it("bounds the colour scale by the field and fresh sensors only (a stale value never stretches it)", () => {
    expect(scaleBounds(map)).toEqual({ min: 18, max: 30 });
    expect(scaleBounds({ grid: null, sensors: [sensor({ state: "missing", value: null })] } as unknown as HeatMap)).toBeNull();
  });
  it("reports threshold bands from the alarm rules", () => {
    expect(thresholdBreach(map, 28)).toBe("high");
    expect(thresholdBreach(map, 17)).toBe("low");
    expect(thresholdBreach(map, 22)).toBeNull();
  });
  it("turns a bearing into a screen vector (0 degrees is +X, clockwise)", () => {
    const [x, y] = bearingToVector(90, 10);
    expect(x).toBeCloseTo(0, 6);
    expect(y).toBeCloseTo(10, 6);
  });
});
