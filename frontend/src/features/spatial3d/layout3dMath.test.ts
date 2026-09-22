import { describe, expect, it } from "vitest";

import { computeUnplacedRacks, occupiesFace, racksMissingCoordinates, uRangeToPlacement } from "./layout3dMath";
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

describe("computeUnplacedRacks", () => {
  it("returns only racks with no placement at all", () => {
    const inventory = [
      { id: "a", placement: null },
      { id: "b", placement: { room_id: "room-1", x_mm: 10, y_mm: 20, rotation_deg: 0, effective_from: "2026-01-01" } },
      { id: "c", placement: { room_id: "room-2", x_mm: null, y_mm: null, rotation_deg: null, effective_from: "2026-01-01" } },
    ];
    expect(computeUnplacedRacks(inventory).map((rack) => rack.id)).toEqual(["a"]);
  });

  it("does not report a rack placed in a different room as unplaced", () => {
    const inventory = [{ id: "elsewhere", placement: { room_id: "some-other-room", x_mm: 1, y_mm: 1, rotation_deg: 0, effective_from: "2026-01-01" } }];
    expect(computeUnplacedRacks(inventory)).toEqual([]);
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
