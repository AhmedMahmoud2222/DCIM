import type { Basemap } from "@/features/dashboard/basemap/schema";

/** Basemap rings on the dashboard's 360 x 180 equirectangular plane: x = lon + 180, y = 90 - lat, where the
 * file stores lon and lat as integers in degrees x 100. */

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface CountryShape {
  key: string;
  iso2: string | null;
  name: string;
  /** SVG path data for every ring of every polygon; draw with fill-rule evenodd so holes stay open. */
  d: string;
  /** Bounds of the country's main landmass(es), for focusing the map on it. */
  bounds: Bounds;
}

export interface ProjectedBasemap {
  shapes: CountryShape[];
  byIso2: Map<string, CountryShape>;
}

/** Polygons smaller than this share of the country's largest polygon (outlying islands, exclaves) are left
 * out of the focus bounds, so focusing on a country does not zoom out to cover a remote island. */
const FOCUS_AREA_SHARE = 0.2;

export const xOf = (lon: number) => lon / 100 + 180;
export const yOf = (lat: number) => 90 - lat / 100;

export function ringPath(ring: ReadonlyArray<number>): string {
  let d = "";
  for (let i = 0; i < ring.length; i += 2) d += `${i === 0 ? "M" : "L"}${xOf(ring[i])} ${yOf(ring[i + 1])}`;
  return `${d}Z`;
}

export function ringBounds(ring: ReadonlyArray<number>): Bounds {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < ring.length; i += 2) {
    const x = xOf(ring[i]);
    const y = yOf(ring[i + 1]);
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return { minX, minY, maxX, maxY };
}

/** Absolute shoelace area of a ring, in squared degrees (only used to compare polygons of one country). */
export function ringArea(ring: ReadonlyArray<number>): number {
  let sum = 0;
  const n = ring.length;
  for (let i = 0; i < n; i += 2) {
    const j = (i + 2) % n;
    sum += ring[i] * ring[j + 1] - ring[j] * ring[i + 1];
  }
  return Math.abs(sum) / 2 / 10_000;
}

export function unionBounds(list: ReadonlyArray<Bounds>): Bounds {
  return {
    minX: Math.min(...list.map((b) => b.minX)),
    minY: Math.min(...list.map((b) => b.minY)),
    maxX: Math.max(...list.map((b) => b.maxX)),
    maxY: Math.max(...list.map((b) => b.maxY)),
  };
}

function project(basemap: Basemap): ProjectedBasemap {
  const shapes = basemap.countries.map((country, index): CountryShape => {
    const outer = country.polygons.map((polygon) => ({ area: ringArea(polygon[0]), bounds: ringBounds(polygon[0]) }));
    const largest = Math.max(...outer.map((p) => p.area));
    const main = outer.filter((p) => p.area >= largest * FOCUS_AREA_SHARE).map((p) => p.bounds);
    return {
      key: `${index}:${country.iso2 ?? "-"}`,
      iso2: country.iso2,
      name: country.name,
      d: country.polygons.map((polygon) => polygon.map(ringPath).join("")).join(""),
      bounds: unionBounds(main),
    };
  });
  const byIso2 = new Map<string, CountryShape>();
  for (const shape of shapes) if (shape.iso2) byIso2.set(shape.iso2, shape);
  return { shapes, byIso2 };
}

const cache = new WeakMap<Basemap, ProjectedBasemap>();

/** Path data and bounds for a basemap, computed once per basemap object. */
export function projectBasemap(basemap: Basemap): ProjectedBasemap {
  let projected = cache.get(basemap);
  if (!projected) {
    projected = project(basemap);
    cache.set(basemap, projected);
  }
  return projected;
}
