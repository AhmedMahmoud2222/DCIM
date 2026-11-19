import { describe, expect, it } from "vitest";

import { projectBasemap, ringArea, ringBounds, ringPath, xOf, yOf } from "@/features/dashboard/basemap/projection";
import { box, tinyBasemap } from "@/features/dashboard/basemap/testFixtures";

describe("basemap projection", () => {
  it("maps degrees x 100 onto the 360 x 180 plane", () => {
    expect(xOf(-18000)).toBe(0);
    expect(xOf(18000)).toBe(360);
    expect(xOf(5530)).toBeCloseTo(235.3);
    expect(yOf(9000)).toBe(0);
    expect(yOf(-9000)).toBe(180);
    expect(yOf(2520)).toBeCloseTo(64.8);
  });

  it("turns a flat ring into a closed SVG path", () => {
    expect(ringPath([0, 0, 1000, 0, 1000, 500])).toBe("M180 90L190 90L190 85Z");
  });

  it("measures ring bounds and area", () => {
    expect(ringBounds(box(10, -5, 20, 5))).toEqual({ minX: 190, minY: 85, maxX: 200, maxY: 95 });
    expect(ringArea(box(10, -5, 20, 5))).toBeCloseTo(100);
  });

  it("projects every country with holes kept as extra rings and indexes them by iso2", () => {
    const projected = projectBasemap(tinyBasemap());
    expect(projected.shapes).toHaveLength(4);
    const gb = projected.byIso2.get("GB");
    expect(gb?.d.match(/M/g)).toHaveLength(3);
    expect(projected.byIso2.has("AE")).toBe(true);
    expect(projected.shapes.find((s) => s.iso2 === null)?.name).toBe("Disputed area");
    expect(projected.byIso2.size).toBe(3);
  });

  it("focuses on a country's main landmass, leaving out small remote islands", () => {
    const gb = projectBasemap(tinyBasemap()).byIso2.get("GB");
    expect(gb?.bounds).toEqual({ minX: 172, minY: 31, maxX: 182, maxY: 40 });
  });

  it("computes once per basemap object", () => {
    const basemap = tinyBasemap();
    expect(projectBasemap(basemap)).toBe(projectBasemap(basemap));
    expect(projectBasemap(tinyBasemap())).not.toBe(projectBasemap(basemap));
  });
});
