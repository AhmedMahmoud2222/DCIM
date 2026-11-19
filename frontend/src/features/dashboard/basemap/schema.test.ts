import { describe, expect, it } from "vitest";

import { MAX_COUNTRIES, MAX_POINTS, unsupportedSchemaVersion, validateBasemap, validateManifest } from "@/features/dashboard/basemap/schema";
import { box, encodeJson, manifestFor, tinyBasemap } from "@/features/dashboard/basemap/testFixtures";

const reasonOf = (result: { ok: boolean; reason?: string }) => (result.ok ? "ok" : result.reason);

function withCountry(country: unknown) {
  return { ...tinyBasemap(), countries: [country] };
}

describe("validateBasemap", () => {
  it("accepts a well-formed basemap and returns it", () => {
    const basemap = tinyBasemap();
    const result = validateBasemap(basemap);
    expect(result).toEqual({ ok: true, value: basemap });
  });

  it.each<[string, unknown, string]>([
    ["a non-object", [], "not an object"],
    ["null", null, "not an object"],
    ["another format", { ...tinyBasemap(), format: "geojson" }, "format is not dcim-basemap"],
    ["schema_version 2", { ...tinyBasemap(), schema_version: 2 }, "unsupported schema_version"],
    ["a string schema_version", { ...tinyBasemap(), schema_version: "1" }, "unsupported schema_version"],
    ["an empty dataset_id", { ...tinyBasemap(), dataset_id: "" }, "invalid dataset_id"],
    ["a 65-character dataset_id", { ...tinyBasemap(), dataset_id: "d".repeat(65) }, "invalid dataset_id"],
    ["an empty version", { ...tinyBasemap(), version: "" }, "invalid version"],
    ["a numeric version", { ...tinyBasemap(), version: 5 }, "invalid version"],
    ["a version with a control character", { ...tinyBasemap(), version: "5.1\n" }, "invalid version"],
    ["another projection", { ...tinyBasemap(), projection: "mercator" }, "projection is not equirectangular"],
    ["another precision", { ...tinyBasemap(), precision: 10 }, "precision is not 100"],
    ["no countries", { ...tinyBasemap(), countries: [] }, "countries is not a non-empty array"],
    ["countries as an object", { ...tinyBasemap(), countries: {} }, "countries is not a non-empty array"],
    ["a country that is not an object", withCountry("AE"), "country is not an object"],
    ["a lowercase iso2", withCountry({ iso2: "ae", name: "x", polygons: [[box(0, 0, 1, 1)]] }), "invalid iso2"],
    ["a three-letter iso2", withCountry({ iso2: "ARE", name: "x", polygons: [[box(0, 0, 1, 1)]] }), "invalid iso2"],
    ["a missing name", withCountry({ iso2: "AE", polygons: [[box(0, 0, 1, 1)]] }), "invalid country name"],
    ["a blank name", withCountry({ iso2: "AE", name: "  ", polygons: [[box(0, 0, 1, 1)]] }), "invalid country name"],
    ["a 129-character name", withCountry({ iso2: "AE", name: "n".repeat(129), polygons: [[box(0, 0, 1, 1)]] }), "invalid country name"],
    ["no polygons", withCountry({ iso2: "AE", name: "x", polygons: [] }), "polygons is not a non-empty array"],
    ["an empty polygon", withCountry({ iso2: "AE", name: "x", polygons: [[]] }), "polygon is not a non-empty array of rings"],
    ["a ring that is not an array", withCountry({ iso2: "AE", name: "x", polygons: [["0,0"]] }), "ring is not an array"],
    ["an odd-length ring", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, 100, 0, 100, 100, 5]]] }), "ring has an odd number of values"],
    ["a two-point ring", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, 100, 0]]] }), "ring has fewer than 3 points"],
    ["a fractional coordinate", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, 100.5, 0, 100, 100]]] }), "coordinate is not a finite integer"],
    ["a string coordinate", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, "100", 0, 100, 100]]] }), "coordinate is not a finite integer"],
    ["a null coordinate (NaN in JSON)", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, null, 0, 100, 100]]] }), "coordinate is not a finite integer"],
    ["an infinite coordinate", withCountry({ iso2: "AE", name: "x", polygons: [[[0, 0, Infinity, 0, 100, 100]]] }), "coordinate is not a finite integer"],
    ["a longitude past 180", withCountry({ iso2: "AE", name: "x", polygons: [[[18001, 0, 100, 0, 100, 100]]] }), "longitude out of range"],
    ["a latitude past 90", withCountry({ iso2: "AE", name: "x", polygons: [[[0, -9001, 100, 0, 100, 100]]] }), "latitude out of range"],
  ])("rejects %s", (_, input, reason) => {
    expect(reasonOf(validateBasemap(input))).toBe(reason);
  });

  it("rejects a duplicate iso2 but allows several null iso2 entities", () => {
    const shape = { name: "x", polygons: [[box(0, 0, 1, 1)]] };
    expect(reasonOf(validateBasemap({ ...tinyBasemap(), countries: [{ ...shape, iso2: "AE" }, { ...shape, iso2: "AE" }] }))).toBe("duplicate iso2 AE");
    expect(validateBasemap({ ...tinyBasemap(), countries: [{ ...shape, iso2: null }, { ...shape, iso2: null }] }).ok).toBe(true);
  });

  it(`rejects more than ${MAX_COUNTRIES} countries`, () => {
    const countries = Array.from({ length: MAX_COUNTRIES + 1 }, () => ({ iso2: null, name: "x", polygons: [[box(0, 0, 1, 1)]] }));
    expect(reasonOf(validateBasemap({ ...tinyBasemap(), countries }))).toBe(`more than ${MAX_COUNTRIES} countries`);
  });

  it(`rejects more than ${MAX_POINTS} points in total`, () => {
    const ring = Array.from({ length: 2 * 50_001 }, (_, i) => (i % 2 === 0 ? i % 18000 : 0));
    const countries = Array.from({ length: 5 }, () => ({ iso2: null, name: "x", polygons: [[ring]] }));
    expect(reasonOf(validateBasemap({ ...tinyBasemap(), countries }))).toBe(`more than ${MAX_POINTS} points`);
  });
});

describe("validateManifest", () => {
  const good = () => manifestFor(encodeJson(tinyBasemap()));

  it("accepts a well-formed manifest", () => {
    const manifest = good();
    expect(validateManifest(manifest)).toEqual({ ok: true, value: manifest });
    expect(validateManifest({ ...manifest, created_at: "2026-11-18T00:00:00.123+04:00" }).ok).toBe(true);
    // created_at and source are informational and optional, as in scripts/dcim_basemap.py.
    expect(validateManifest({ ...manifest, created_at: undefined, source: undefined }).ok).toBe(true);
  });

  it.each<[string, (m: Record<string, unknown>) => unknown, string]>([
    ["a non-object", () => "manifest", "not an object"],
    ["another format", (m) => ({ ...m, format: "dcim-basemap" }), "format is not dcim-basemap-manifest"],
    ["schema_version 2", (m) => ({ ...m, schema_version: 2 }), "unsupported schema_version"],
    ["a missing dataset_id", (m) => ({ ...m, dataset_id: undefined }), "invalid dataset_id"],
    ["a 65-character version", (m) => ({ ...m, version: "1".repeat(65) }), "invalid version"],
    ["a parent-directory file", (m) => ({ ...m, file: "../basemap.json" }), "invalid file name"],
    ["a dotted file", (m) => ({ ...m, file: "..json" }), "invalid file name"],
    ["a file with a slash", (m) => ({ ...m, file: "dir/basemap.json" }), "invalid file name"],
    ["a URL as the file", (m) => ({ ...m, file: "https://tiles.example.com/x.json" }), "invalid file name"],
    ["a file that is not JSON", (m) => ({ ...m, file: "basemap.js" }), "invalid file name"],
    ["zero bytes", (m) => ({ ...m, bytes: 0 }), "invalid bytes"],
    ["fractional bytes", (m) => ({ ...m, bytes: 10.5 }), "invalid bytes"],
    ["bytes above 4 MiB", (m) => ({ ...m, bytes: 4 * 1024 * 1024 + 1 }), "invalid bytes"],
    ["an uppercase sha256", (m) => ({ ...m, sha256: "A".repeat(64) }), "invalid sha256"],
    ["a short sha256", (m) => ({ ...m, sha256: "a".repeat(63) }), "invalid sha256"],
    ["a numeric created_at", (m) => ({ ...m, created_at: 20261118 }), "invalid created_at"],
    ["a source that is not an object", (m) => ({ ...m, source: "Natural Earth" }), "source is not an object"],
    ["a source with a numeric license", (m) => ({ ...m, source: { name: "NE", url: "u", license: 1, source_sha256: "0".repeat(64) } }), "invalid source"],
    ["a bad source sha256", (m) => ({ ...m, source: { name: "NE", url: "u", license: "PD", source_sha256: "x" } }), "invalid source sha256"],
  ])("rejects %s", (_, mutate, reason) => {
    expect(reasonOf(validateManifest(mutate(good() as unknown as Record<string, unknown>)))).toBe(reason);
  });
});

describe("unsupportedSchemaVersion", () => {
  it("names an unsupported numeric version and ignores everything else", () => {
    expect(unsupportedSchemaVersion({ schema_version: 2 })).toBe(2);
    expect(unsupportedSchemaVersion({ schema_version: 1 })).toBeNull();
    expect(unsupportedSchemaVersion({ schema_version: "2" })).toBeNull();
    expect(unsupportedSchemaVersion("x")).toBeNull();
  });
});
