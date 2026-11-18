import type { Basemap, BasemapManifest } from "@/features/dashboard/basemap/schema";
import { sha256Hex } from "@/features/dashboard/basemap/sha256";

/** Small hand-made basemaps for unit tests. Imported by tests only. */

/** A lon/lat box in degrees as a flat ring in degrees x 100. */
export function box(west: number, south: number, east: number, north: number): number[] {
  return [west, south, east, south, east, north, west, north].map((degrees) => Math.round(degrees * 100));
}

export function tinyBasemap(overrides: Partial<Basemap> = {}): Basemap {
  return {
    format: "dcim-basemap",
    schema_version: 1,
    dataset_id: "natural-earth-admin0-50m",
    version: "5.1.2-r1",
    projection: "equirectangular",
    precision: 100,
    countries: [
      { iso2: "AE", name: "United Arab Emirates", polygons: [[box(51.5, 22.6, 56.4, 26.1)]] },
      // A big polygon with a hole plus a far, small island: the island is left out of the focus bounds.
      { iso2: "GB", name: "United Kingdom", polygons: [[box(-8, 50, 2, 59), box(-3, 53, -2, 54)], [box(-60, -52, -59.5, -51.5)]] },
      { iso2: "SA", name: "Saudi Arabia", polygons: [[box(34.5, 16.3, 55.7, 32.2)]] },
      { iso2: null, name: "Disputed area", polygons: [[box(10, 10, 11, 11)]] },
    ],
    ...overrides,
  };
}

export const encodeJson = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));

export function manifestFor(bytes: Uint8Array, overrides: Partial<BasemapManifest> = {}): BasemapManifest {
  return {
    format: "dcim-basemap-manifest",
    schema_version: 1,
    dataset_id: "natural-earth-admin0-50m",
    version: "5.1.2-r1",
    file: "basemap-test.json",
    bytes: bytes.length,
    sha256: sha256Hex(bytes),
    created_at: "2026-11-18T00:00:00Z",
    source: { name: "Test", url: "https://example.invalid/source", license: "Public domain", source_sha256: "0".repeat(64) },
    ...overrides,
  };
}
