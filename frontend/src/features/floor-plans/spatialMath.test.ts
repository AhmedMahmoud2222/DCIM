import { describe, expect, it } from "vitest";

import {
  boundsOf,
  explainIncomplete,
  fitViewport,
  formatLength,
  gridTicks,
  mmToPx,
  mmToSource,
  niceGridInterval,
  pxToMm,
  rackFootprint,
  rackHeightMm,
  rectCorners,
  rectMmToSource,
  rectSourceToMm,
  sceneBounds,
  snapToGrid,
  sourceToMm,
} from "./spatialMath";
import type { RoomSpatialView } from "@/types";

const dxfCal = { mm_per_unit: 1, origin_x: 0, origin_y: 4000, y_axis: "up" as const, rotation_quadrants: 0 };

describe("calibration transform (mirrors the backend)", () => {
  it("maps a Y-up DXF point to room-local mm with Y flipped", () => {
    expect(sourceToMm(dxfCal, 0, 4000)).toEqual([0, 0]);
    expect(sourceToMm(dxfCal, 1000, 1000)).toEqual([1000, 3000]);
  });
  it("applies scale and clockwise quadrant rotation", () => {
    const cal = { mm_per_unit: 5, origin_x: 10, origin_y: 10, y_axis: "down" as const, rotation_quadrants: 1 };
    // (110, 10) -> u=500, v=0 -> rotate 90 clockwise -> (0, 500)
    expect(sourceToMm(cal, 110, 10)).toEqual([0, 500]);
  });
  it("round-trips every quadrant and axis convention", () => {
    for (const y_axis of ["up", "down"] as const) {
      for (let q = 0; q < 4; q += 1) {
        const cal = { mm_per_unit: 2.5, origin_x: 13, origin_y: -7, y_axis, rotation_quadrants: q };
        const [X, Y] = sourceToMm(cal, 321, 654);
        const [x, y] = mmToSource(cal, X, Y);
        expect(x).toBeCloseTo(321, 9);
        expect(y).toBeCloseTo(654, 9);
      }
    }
  });
  it("converts a source rectangle to the canonical top-left rectangle and back", () => {
    const rect = { cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 30 };
    const mm = rectSourceToMm(dxfCal, rect);
    expect(mm).toMatchObject({ x_mm: 1000, y_mm: 2000, width_mm: 600, height_mm: 1000 });
    expect(mm.rotation_deg).toBeCloseTo(330, 9); // Y-up CCW 30 -> clockwise 330
    const back = rectMmToSource(dxfCal, mm);
    expect(back.cx).toBeCloseTo(1300, 9);
    expect(back.cy).toBeCloseTo(1500, 9);
    expect(back.rotation_deg).toBeCloseTo(30, 9);
    expect(back.width).toBeCloseTo(600, 9);
  });
  it("adds quadrant rotation to the object rotation", () => {
    const cal = { mm_per_unit: 1, origin_x: 0, origin_y: 0, y_axis: "down" as const, rotation_quadrants: 1 };
    expect(rectSourceToMm(cal, { cx: 0, cy: 0, width: 1, height: 1, rotation_deg: 30 }).rotation_deg).toBeCloseTo(120);
  });
});

describe("engineering grid", () => {
  it("chooses 1-2-5 steps that stay at least minPx wide", () => {
    expect(niceGridInterval(0.05, 40)).toBe(1000); // 800 mm raw -> 1 m
    expect(niceGridInterval(0.2, 40)).toBe(200);
    expect(niceGridInterval(1, 40)).toBe(50);
    expect(niceGridInterval(5, 40)).toBe(10);
    for (const ppm of [0.013, 0.07, 0.3, 2, 11]) {
      const step = niceGridInterval(ppm, 40);
      expect(step * ppm).toBeGreaterThanOrEqual(40);
      expect(step * ppm).toBeLessThan(40 * 2.6);
    }
    expect(niceGridInterval(0)).toBe(1000);
  });
  it("snaps to the configured interval and tolerates a zero interval", () => {
    expect(snapToGrid(1234, 100)).toBe(1200);
    expect(snapToGrid(1250, 100)).toBe(1300);
    expect(snapToGrid(-149, 100)).toBe(-100);
    expect(snapToGrid(77, 0)).toBe(77);
  });
  it("lists aligned ticks and caps their number", () => {
    expect(gridTicks(-50, 450, 100)).toEqual([0, 100, 200, 300, 400]);
    expect(gridTicks(0, 1e9, 1, 50)).toHaveLength(50);
    expect(gridTicks(0, 10, 0)).toEqual([]);
  });
  it("formats millimetres and metres", () => {
    expect(formatLength(600)).toBe("600 mm");
    expect(formatLength(1200)).toBe("1.20 m");
    expect(formatLength(-2500)).toBe("-2.50 m");
  });
});

describe("viewport", () => {
  it("fits the bounds with a uniform scale and round-trips px <-> mm", () => {
    const vp = fitViewport({ minX: 0, minY: 0, maxX: 6000, maxY: 4000 }, 640, 20);
    expect(vp.scale).toBeCloseTo(600 / 6000);
    expect(vp.widthPx).toBe(640);
    const [px, py] = mmToPx(vp, 3000, 2000);
    const [x, y] = pxToMm(vp, px, py);
    expect(x).toBeCloseTo(3000);
    expect(y).toBeCloseTo(2000);
    expect(vp.heightPx).toBe(Math.round(4000 * vp.scale + 40));
  });
  it("keeps a real 600 x 1000 mm rack at the true proportion on screen", () => {
    const vp = fitViewport({ minX: 0, minY: 0, maxX: 6000, maxY: 4000 }, 960, 30);
    expect((600 * vp.scale) / (1000 * vp.scale)).toBeCloseTo(0.6);
    expect(1000 * vp.scale).toBeCloseTo(1000 * ((960 - 60) / 6000));
  });
});

describe("geometry helpers", () => {
  it("computes oriented corners about the centre", () => {
    const c = rectCorners(0, 0, 100, 50, 90);
    const b = boundsOf(c);
    expect(b.maxX - b.minX).toBeCloseTo(50);
    expect(b.maxY - b.minY).toBeCloseTo(100);
    expect(rectCorners(10, 20, 100, 50, 0)[0]).toEqual([10, 20]);
  });
  it("never invents a footprint", () => {
    expect(rackFootprint({ width_mm: 600, depth_mm: 1000 })).toEqual({ width: 600, depth: 1000 });
    expect(rackFootprint({ width_mm: null, depth_mm: 1000 })).toBeNull();
    expect(rackFootprint({})).toBeNull();
  });
  it("derives rack height from U, preferring the authoritative value", () => {
    expect(rackHeightMm({ height_u: 42 })).toBe(1867);
    expect(rackHeightMm({ height_u: 42, height_mm: 2000 })).toBe(2000);
  });
});

const baseView: RoomSpatialView = {
  room_id: "r", room_name: "R", active_floor_plan_id: null, active_floor_plan_revision: null, room_width_mm: null, room_height_mm: null,
  generated_at: "", racks: [], equipment: [], objects: [], rack_equipment: [],
};

describe("scene bounds", () => {
  it("prefers the approved boundary, then room dimensions, then content, else none", () => {
    expect(sceneBounds(baseView)).toEqual({ bounds: null, source: "none" });
    const dims = sceneBounds({ ...baseView, room_width_mm: 5000, room_height_mm: 3000 });
    expect(dims.source).toBe("room_dimensions");
    expect(dims.bounds).toEqual({ minX: 0, minY: 0, maxX: 5000, maxY: 3000 });
    const boundary = sceneBounds({
      ...baseView, room_width_mm: 5000, room_height_mm: 3000,
      boundary: { id: "b", object_type: "room_outline", geometry_type: "rect", x_mm: 100, y_mm: 200, width_mm: 800, height_mm: 400, rotation_deg: 0, label: null, source: "authoritative" },
    });
    expect(boundary.source).toBe("boundary");
    expect(boundary.bounds).toEqual({ minX: 100, minY: 200, maxX: 900, maxY: 600 });
    const content = sceneBounds({
      ...baseView,
      racks: [{ id: "k", asset_tag: "t", name: "K", x_mm: 1000, y_mm: 2000, rotation_deg: 0, spatial_object_id: null, height_u: 42, width_mm: 600, depth_mm: 1000 }],
    });
    expect(content).toEqual({ bounds: { minX: 1000, minY: 2000, maxX: 1600, maxY: 3000 }, source: "content" });
  });
  it("ignores racks without position or dimensions instead of guessing", () => {
    const view = { ...baseView, racks: [{ id: "k", asset_tag: "t", name: "K", x_mm: null, y_mm: null, rotation_deg: null, spatial_object_id: null, height_u: 42, width_mm: 600, depth_mm: 1000 }] };
    expect(sceneBounds(view).source).toBe("none");
  });
});

describe("incomplete-data explanations", () => {
  it("turns backend reasons into plain statements", () => {
    expect(explainIncomplete("no_calibration")).toContain("not calibrated");
    expect(explainIncomplete("rack_position_missing:1")).toBe("1 rack placed in this room has no recorded floor position");
    expect(explainIncomplete("rack_position_missing:3")).toContain("3 racks");
    expect(explainIncomplete("equipment_dimensions_missing:2")).toContain("2 floor-standing equipment items have incomplete");
    expect(explainIncomplete("something_new")).toBe("something_new");
  });
});
