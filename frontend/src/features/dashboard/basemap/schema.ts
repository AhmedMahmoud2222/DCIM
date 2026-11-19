/** Basemap data and manifest formats (Issue #365, spec sections 1 and 2) and their strict validators.
 *
 * Both files are untrusted input: an override is whatever the deployment placed under /map-data/. The
 * validators read fixed property names only, test with literal regexes and never index objects with keys
 * taken from the data. */

export const SUPPORTED_SCHEMA_VERSIONS: ReadonlySet<number> = new Set([1]);
export const MAX_COUNTRIES = 400;
export const MAX_POINTS = 250_000;
/** Largest accepted basemap data file, in bytes (4 MiB). */
export const MAX_BASEMAP_BYTES = 4 * 1024 * 1024;
export const MAX_NAME_LENGTH = 128;
const MAX_LON = 18_000;
const MAX_LAT = 9_000;

const ISO2 = /^[A-Z]{2}$/;
const FILE_NAME = /^[A-Za-z0-9._-]{1,128}\.json$/;
const SHA256 = /^[0-9a-f]{64}$/;

export interface BasemapCountry {
  iso2: string | null;
  name: string;
  /** Polygons; each is a list of flat [lon, lat, ...] rings in degrees x 100, outer ring first. */
  polygons: number[][][];
}

export interface Basemap {
  format: "dcim-basemap";
  schema_version: 1;
  dataset_id: string;
  version: string;
  projection: "equirectangular";
  precision: 100;
  countries: BasemapCountry[];
}

export interface BasemapManifest {
  format: "dcim-basemap-manifest";
  schema_version: 1;
  dataset_id: string;
  version: string;
  file: string;
  bytes: number;
  sha256: string;
  created_at?: string;
  source?: { name?: string; url?: string; license?: string; source_sha256?: string };
}

export type Validation<T> = { ok: true; value: T } | { ok: false; reason: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** dataset_id and version: 1 to 64 characters (the same limit as scripts/dcim_basemap.py), no control characters. */
function isIdentifier(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > 64) return false;
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return false;
  }
  return true;
}

const fail = (reason: string): { ok: false; reason: string } => ({ ok: false, reason });

/** The schema_version of a parsed file when it is a number the client does not support, else null. */
export function unsupportedSchemaVersion(value: unknown): number | null {
  if (!isRecord(value)) return null;
  const version = value.schema_version;
  return typeof version === "number" && !SUPPORTED_SCHEMA_VERSIONS.has(version) ? version : null;
}

function checkIdentity(value: Record<string, unknown>): string | null {
  if (typeof value.schema_version !== "number" || !SUPPORTED_SCHEMA_VERSIONS.has(value.schema_version)) {
    return "unsupported schema_version";
  }
  if (!isIdentifier(value.dataset_id)) return "invalid dataset_id";
  if (!isIdentifier(value.version)) return "invalid version";
  return null;
}

function checkRing(ring: unknown): { points: number } | { reason: string } {
  if (!Array.isArray(ring)) return { reason: "ring is not an array" };
  if (ring.length % 2 !== 0) return { reason: "ring has an odd number of values" };
  if (ring.length < 6) return { reason: "ring has fewer than 3 points" };
  for (let i = 0; i < ring.length; i += 1) {
    const n: unknown = ring[i];
    if (typeof n !== "number" || !Number.isInteger(n)) return { reason: "coordinate is not a finite integer" };
    const limit = i % 2 === 0 ? MAX_LON : MAX_LAT;
    if (n < -limit || n > limit) return { reason: i % 2 === 0 ? "longitude out of range" : "latitude out of range" };
  }
  return { points: ring.length / 2 };
}

export function validateBasemap(input: unknown): Validation<Basemap> {
  if (!isRecord(input)) return fail("not an object");
  if (input.format !== "dcim-basemap") return fail("format is not dcim-basemap");
  const identity = checkIdentity(input);
  if (identity) return fail(identity);
  if (input.projection !== "equirectangular") return fail("projection is not equirectangular");
  if (input.precision !== 100) return fail("precision is not 100");
  const countries = input.countries;
  if (!Array.isArray(countries) || countries.length === 0) return fail("countries is not a non-empty array");
  if (countries.length > MAX_COUNTRIES) return fail(`more than ${MAX_COUNTRIES} countries`);

  const seen = new Set<string>();
  let points = 0;
  for (const country of countries) {
    if (!isRecord(country)) return fail("country is not an object");
    const iso2 = country.iso2;
    if (iso2 !== null) {
      if (typeof iso2 !== "string" || !ISO2.test(iso2)) return fail("invalid iso2");
      if (seen.has(iso2)) return fail(`duplicate iso2 ${iso2}`);
      seen.add(iso2);
    }
    const name = country.name;
    if (typeof name !== "string" || name.trim().length === 0 || name.length > MAX_NAME_LENGTH) return fail("invalid country name");
    const polygons = country.polygons;
    if (!Array.isArray(polygons) || polygons.length === 0) return fail("polygons is not a non-empty array");
    for (const polygon of polygons) {
      if (!Array.isArray(polygon) || polygon.length === 0) return fail("polygon is not a non-empty array of rings");
      for (const ring of polygon) {
        const checked = checkRing(ring);
        if ("reason" in checked) return fail(checked.reason);
        points += checked.points;
        if (points > MAX_POINTS) return fail(`more than ${MAX_POINTS} points`);
      }
    }
  }
  return { ok: true, value: input as unknown as Basemap };
}

export function validateManifest(input: unknown): Validation<BasemapManifest> {
  if (!isRecord(input)) return fail("not an object");
  if (input.format !== "dcim-basemap-manifest") return fail("format is not dcim-basemap-manifest");
  const identity = checkIdentity(input);
  if (identity) return fail(identity);
  if (typeof input.file !== "string" || !FILE_NAME.test(input.file) || input.file.includes("..")) return fail("invalid file name");
  const bytes = input.bytes;
  if (typeof bytes !== "number" || !Number.isInteger(bytes) || bytes < 1 || bytes > MAX_BASEMAP_BYTES) return fail("invalid bytes");
  if (typeof input.sha256 !== "string" || !SHA256.test(input.sha256)) return fail("invalid sha256");
  // Provenance fields are informational and optional, but must be well-formed when present.
  if (input.created_at !== undefined && (typeof input.created_at !== "string" || input.created_at.length > 64)) return fail("invalid created_at");
  const source = input.source;
  if (source !== undefined) {
    if (!isRecord(source)) return fail("source is not an object");
    for (const text of [source.name, source.url, source.license]) {
      if (text !== undefined && (typeof text !== "string" || text.length > 512)) return fail("invalid source");
    }
    if (source.source_sha256 !== undefined && (typeof source.source_sha256 !== "string" || !SHA256.test(source.source_sha256))) {
      return fail("invalid source sha256");
    }
  }
  return { ok: true, value: input as unknown as BasemapManifest };
}
