import { describe, expect, it } from "vitest";

import { occupiesFace, racksMissingCoordinates, uRangeToPlacement } from "./layout3dMath";
import type { RoomRack } from "@/types";

describe("uRangeToPlacement", () => {
  it("derives position from u_start/u_end, not array order", () => {
    // A 12U rack, item at U1 (the physical bottom, per the established "U1 at base"
    // convention — see RackElevationView.tsx).
    const bottom = uRangeToPlacement(1, 2, 12);
    expect(bottom.irregular).toBe(false);
    expect(bottom.topPct).toBeCloseTo((11 / 12) * 100, 5);
    expect(bottom.heightPct).toBeCloseTo((1 / 12) * 100, 5);
  });

  it("places the topmost U at the top of the rack face", () => {
    const top = uRangeToPlacement(12, 13, 12);
    expect(top.topPct).toBeCloseTo(0, 5);
    expect(top.heightPct).toBeCloseTo((1 / 12) * 100, 5);
  });

  it("represents a multi-U item's full occupied height", () => {
    const tall = uRangeToPlacement(3, 7, 12); // occupies U3-U6, 4U tall
    expect(tall.heightPct).toBeCloseTo((4 / 12) * 100, 5);
    expect(tall.irregular).toBe(false);
  });

  it("fills the entire rack for a full-height item at the boundary", () => {
    const full = uRangeToPlacement(1, 13, 12);
    expect(full.topPct).toBeCloseTo(0, 5);
    expect(full.heightPct).toBeCloseTo(100, 5);
  });

  it("clamps and flags inverted ranges instead of producing a negative layout", () => {
    const inverted = uRangeToPlacement(5, 3, 12);
    expect(inverted.irregular).toBe(true);
    expect(inverted.topPct).toBeGreaterThanOrEqual(0);
    expect(inverted.heightPct).toBeGreaterThan(0);
  });

  it("clamps and flags out-of-range U values instead of overflowing the rack", () => {
    const overflow = uRangeToPlacement(10, 999, 12);
    expect(overflow.irregular).toBe(true);
    expect(overflow.topPct).toBeGreaterThanOrEqual(0);
    expect(overflow.topPct + overflow.heightPct).toBeLessThanOrEqual(100.001);
  });

  it("flags a placement below U1 as irregular but still renders a safe layout", () => {
    const belowFloor = uRangeToPlacement(0, 1, 12);
    expect(belowFloor.irregular).toBe(true);
    expect(belowFloor.topPct).toBeGreaterThanOrEqual(0);
    expect(belowFloor.topPct).toBeLessThanOrEqual(100);
  });
});

describe("occupiesFace", () => {
  it("front-only equipment renders only on the front face", () => {
    expect(occupiesFace("front", "front")).toBe(true);
    expect(occupiesFace("front", "rear")).toBe(false);
  });

  it("rear-only equipment renders only on the rear face", () => {
    expect(occupiesFace("rear", "rear")).toBe(true);
    expect(occupiesFace("rear", "front")).toBe(false);
  });

  it("'both' equipment renders on both faces", () => {
    expect(occupiesFace("both", "front")).toBe(true);
    expect(occupiesFace("both", "rear")).toBe(true);
  });
});

describe("racksMissingCoordinates", () => {
  const base: RoomRack = { id: "r", asset_tag: "T", name: "Rack", x_mm: null, y_mm: null, rotation_deg: null, spatial_object_id: null, height_u: 42 };

  it("flags a placed rack with null coordinates as coordinate-incomplete, not unplaced", () => {
    expect(racksMissingCoordinates([base]).map((r) => r.id)).toEqual(["r"]);
  });

  it("does not flag a rack that has coordinates", () => {
    expect(racksMissingCoordinates([{ ...base, x_mm: 5, y_mm: 5 }])).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------------ real-scale scene
import { buildScene, SCENE_BASE_PX } from "./layout3dMath";
import type { RoomSpatialView } from "@/types";

const view = (over: Partial<RoomSpatialView> = {}): RoomSpatialView => ({
  room_id: "r", room_name: "R", active_floor_plan_id: "fp", active_floor_plan_revision: 1, room_width_mm: 6000, room_height_mm: 4000,
  generated_at: "", racks: [], equipment: [], objects: [], rack_equipment: [], ...over,
});
const rack = (over = {}) => ({
  id: "k1", asset_tag: "RK-1", name: "Rack 1", x_mm: 1000, y_mm: 2000, rotation_deg: 0, spatial_object_id: null, height_u: 42,
  width_mm: 600, depth_mm: 1000, height_mm: 1867, ...over,
});

describe("buildScene (dimensionally accurate, no fallbacks)", () => {
  it("returns null when there is nothing to scale against", () => {
    expect(buildScene(view({ room_width_mm: null, room_height_mm: null }))).toBeNull();
  });
  it("uses one scale for room, rack footprint and rack height", () => {
    const scene = buildScene(view({ racks: [rack()] }))!;
    expect(scene.scale).toBeCloseTo(SCENE_BASE_PX / 6000);
    const r = scene.racks[0];
    expect(r.width / scene.scale).toBeCloseTo(600);
    expect(r.depth / scene.scale).toBeCloseTo(1000);
    expect(r.height / scene.scale).toBeCloseTo(1867);
    expect(r.left / scene.scale).toBeCloseTo(1000);
    expect(r.top / scene.scale).toBeCloseTo(2000);
    expect(scene.floor.width / scene.scale).toBeCloseTo(6000);
    expect(scene.floor.height / scene.scale).toBeCloseTo(4000);
  });
  it("derives height from rack units when no explicit height is given", () => {
    const scene = buildScene(view({ racks: [rack({ height_mm: null, height_u: 24 })] }))!;
    expect(scene.racks[0].height / scene.scale).toBeCloseTo(Math.round(24 * 44.45));
    expect(scene.racks[0].mm.height).toBe(1067);
  });
  it("preserves authoritative coordinates and orientation exactly", () => {
    const scene = buildScene(view({ racks: [rack({ x_mm: 3210, y_mm: 1450, rotation_deg: 270 })] }))!;
    expect(scene.racks[0].mm).toMatchObject({ x: 3210, y: 1450, width: 600, depth: 1000 });
    expect(scene.racks[0].rotationDeg).toBe(270);
  });
  it("does not draw a rack without a recorded position; it is listed instead", () => {
    const scene = buildScene(view({ racks: [rack(), rack({ id: "k2", x_mm: null, y_mm: null })] }))!;
    expect(scene.racks.map((r) => r.id)).toEqual(["k1"]);
    expect(scene.missingRackPosition.map((r) => r.id)).toEqual(["k2"]);
  });
  it("does not draw a rack whose catalog footprint is unknown", () => {
    const scene = buildScene(view({ racks: [rack({ id: "k3", width_mm: null })] }))!;
    expect(scene.racks).toEqual([]);
    expect(scene.missingRackDimensions.map((r) => r.id)).toEqual(["k3"]);
  });
  it("draws floor equipment only with full dimensions and pins it (not to scale) when a dimension is missing", () => {
    const scene = buildScene(
      view({
        equipment: [
          { id: "e1", asset_tag: "E1", hostname: "ups-1", placement_type: "floor_standing", spatial_object_id: "o1", x_mm: 4000, y_mm: 500, rotation_deg: 90, width_mm: 800, depth_mm: 900, height_mm: 889, position_state: "placed", dimensions_state: "complete" },
          { id: "e2", asset_tag: "E2", hostname: "pdu-1", placement_type: "floor_standing", spatial_object_id: "o2", x_mm: 100, y_mm: 100, width_mm: 800, depth_mm: null, height_mm: null, position_state: "placed", dimensions_state: "partial" },
          { id: "e3", asset_tag: "E3", hostname: "cool-1", placement_type: "floor_standing", spatial_object_id: null, position_state: "missing", dimensions_state: "complete" },
        ],
      }),
    )!;
    expect(scene.floorEquipment.map((e) => e.id)).toEqual(["e1"]);
    expect(scene.floorEquipment[0].height / scene.scale).toBeCloseTo(889);
    expect(scene.pins.map((p) => p.id)).toEqual(["e2"]);
    expect(scene.missingEquipmentPosition.map((e) => e.id)).toEqual(["e3"]);
  });
  it("anchors positions to the boundary's top-left, not to the origin", () => {
    const scene = buildScene(
      view({
        room_width_mm: null, room_height_mm: null,
        boundary: { id: "b", object_type: "room_outline", geometry_type: "rect", x_mm: 500, y_mm: 500, width_mm: 4000, height_mm: 2000, rotation_deg: 0, label: null, source: "authoritative" },
        racks: [rack({ x_mm: 1500, y_mm: 1000 })],
      }),
    )!;
    expect(scene.boundsSource).toBe("boundary");
    expect(scene.racks[0].left / scene.scale).toBeCloseTo(1000);
    expect(scene.racks[0].top / scene.scale).toBeCloseTo(500);
    expect(scene.boundaryPx).toHaveLength(4);
    expect(scene.boundaryPx![0]).toEqual([0, 0]);
  });
  it("keeps the 600 x 1000 proportion for every rack regardless of the room size", () => {
    for (const w of [3000, 12000, 40000]) {
      const s = buildScene(view({ room_width_mm: w, room_height_mm: w / 2, racks: [rack({ x_mm: 100, y_mm: 100 })] }))!;
      expect(s.racks[0].width / s.racks[0].depth).toBeCloseTo(0.6);
    }
  });
});
