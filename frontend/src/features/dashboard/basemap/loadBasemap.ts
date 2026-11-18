import { Basemap, unsupportedSchemaVersion, validateBasemap, validateManifest } from "@/features/dashboard/basemap/schema";
import { sha256Hex } from "@/features/dashboard/basemap/sha256";

/** Loads the active basemap (Issue #365, spec section 4).
 *
 * An optional same-origin override lives at /map-data/manifest.json and /map-data/<manifest.file>, placed there
 * by the deployment; there is no upload endpoint. A missing override is silent. Any problem with a present
 * override falls back to the bundled basemap with a short note. Only same-origin relative URLs are requested. */

export const MANIFEST_URL = "/map-data/manifest.json";
const MAP_DATA_DIR = "/map-data/";
/** A manifest is a few hundred bytes; anything far larger is not one. */
const MAX_MANIFEST_BYTES = 64 * 1024;

export type BasemapResult =
  | { source: "override" | "bundled"; datasetId: string; version: string; basemap: Basemap; note?: string }
  | { source: "unavailable"; datasetId: null; version: null; basemap: null; note?: string };

type FetchImpl = (input: string, init?: RequestInit) => Promise<Response>;

export interface LoadBasemapOptions {
  fetchImpl?: FetchImpl;
  /** Resolves to the bundled basemap file's parsed JSON. Defaults to the code-split bundled module. */
  bundled?: () => Promise<unknown>;
}

const REQUEST: RequestInit = { cache: "no-store", credentials: "omit", redirect: "error" };

const importBundled = () => import("./world-50m.v1.json").then((module) => module.default as unknown);

class TooLarge extends Error {}

/** Reads a body without buffering more than `limit` bytes: the declared Content-Length is checked first, then
 * the running total while streaming. */
async function readCapped(response: Response, limit: number): Promise<Uint8Array> {
  const declared = response.headers.get("content-length");
  if (declared !== null && /^\d+$/.test(declared) && Number(declared) > limit) throw new TooLarge();
  const body = response.body;
  if (!body) {
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.length > limit) throw new TooLarge();
    return buffer;
  }
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) {
      await reader.cancel().catch(() => undefined);
      throw new TooLarge();
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes;
}

function parseJson(bytes: Uint8Array): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  } catch {
    return { ok: false };
  }
}

async function loadBundled(bundled: () => Promise<unknown>, note?: string): Promise<BasemapResult> {
  let checked: ReturnType<typeof validateBasemap>;
  try {
    checked = validateBasemap(await bundled());
  } catch {
    checked = { ok: false, reason: "could not be loaded" };
  }
  if (!checked.ok) {
    const unavailable = "The bundled basemap is unavailable, so only the graticule is drawn.";
    return { source: "unavailable", datasetId: null, version: null, basemap: null, note: note ? `${note} ${unavailable}` : unavailable };
  }
  const basemap = checked.value;
  return { source: "bundled", datasetId: basemap.dataset_id, version: basemap.version, basemap, note };
}

/** The same-origin path of an override data file, or null when the name is not allowed. The manifest's `file` is
 * untrusted, so beyond the schema's file-name check it is resolved against `<origin>/map-data/` and refused
 * unless the result stays on that origin, directly inside /map-data/, and is exactly the given name: no other
 * directory, no dot segment, no encoded or back slash, no query or fragment. */
export function overrideFileUrl(file: string, origin: string | undefined = globalThis.location?.origin): string | null {
  if (!origin || file === "" || /[/\\%?#]/.test(file) || file === "." || file === "..") return null;
  let base: URL;
  let resolved: URL;
  try {
    base = new URL(MAP_DATA_DIR, origin);
    resolved = new URL(file, base);
  } catch {
    return null;
  }
  if (resolved.origin !== base.origin || !resolved.pathname.startsWith(MAP_DATA_DIR)) return null;
  if (resolved.search !== "" || resolved.hash !== "" || resolved.pathname.slice(MAP_DATA_DIR.length) !== file) return null;
  return resolved.pathname;
}

/** A rejection of a present override: the bundled basemap is used and the reason is reported. */
async function rejected(bundled: () => Promise<unknown>, reason: string, customVersion?: string): Promise<BasemapResult> {
  const custom = customVersion ? `Custom basemap ${customVersion}` : "Custom basemap";
  const result = await loadBundled(bundled, `${custom} was rejected (${reason}).`);
  if (result.source === "bundled") result.note = `${custom} was rejected (${reason}); using bundled basemap ${result.version}.`;
  return result;
}

export async function loadBasemap({ fetchImpl, bundled = importBundled }: LoadBasemapOptions = {}): Promise<BasemapResult> {
  const doFetch: FetchImpl = fetchImpl ?? ((input, init) => fetch(input, init));

  // Step 1: is there an override at all? Missing, unreachable or not JSON (the SPA's index.html) is silent.
  let manifestResponse: Response;
  try {
    manifestResponse = await doFetch(MANIFEST_URL, REQUEST);
  } catch {
    return loadBundled(bundled);
  }
  if (manifestResponse.status === 404) return loadBundled(bundled);
  if (!manifestResponse.ok) return rejected(bundled, `manifest fetch failed, HTTP ${manifestResponse.status}`);
  let manifestBytes: Uint8Array;
  try {
    manifestBytes = await readCapped(manifestResponse, MAX_MANIFEST_BYTES);
  } catch (error) {
    if (error instanceof TooLarge) return rejected(bundled, "manifest too large");
    return loadBundled(bundled);
  }
  const parsedManifest = parseJson(manifestBytes);
  if (!parsedManifest.ok) return loadBundled(bundled);

  // Step 2 onwards: an override is present, so every failure is reported.
  if (unsupportedSchemaVersion(parsedManifest.value) !== null) return rejected(bundled, "incompatible schema");
  const manifestCheck = validateManifest(parsedManifest.value);
  if (!manifestCheck.ok) return rejected(bundled, `invalid manifest: ${manifestCheck.reason}`);
  const manifest = manifestCheck.value;
  const reject = (reason: string) => rejected(bundled, reason, manifest.version);
  const dataUrl = overrideFileUrl(manifest.file);
  if (dataUrl === null) return reject("manifest file name is not allowed");

  let dataResponse: Response;
  try {
    dataResponse = await doFetch(dataUrl, REQUEST);
  } catch {
    return reject("fetch failed");
  }
  if (!dataResponse.ok) return reject(`fetch failed, HTTP ${dataResponse.status}`);
  let bytes: Uint8Array;
  try {
    // A file larger than the manifest says is a size mismatch, so nothing past `manifest.bytes` is buffered.
    bytes = await readCapped(dataResponse, manifest.bytes);
  } catch (error) {
    return reject(error instanceof TooLarge ? "size mismatch" : "fetch failed");
  }
  if (bytes.length !== manifest.bytes) return reject("size mismatch");
  if (sha256Hex(bytes) !== manifest.sha256) return reject("checksum mismatch");
  const parsedData = parseJson(bytes);
  if (!parsedData.ok) return reject("invalid data: not JSON");
  if (unsupportedSchemaVersion(parsedData.value) !== null) return reject("incompatible schema");
  const dataCheck = validateBasemap(parsedData.value);
  if (!dataCheck.ok) return reject(`invalid data: ${dataCheck.reason}`);
  const basemap = dataCheck.value;
  if (basemap.dataset_id !== manifest.dataset_id || basemap.version !== manifest.version) return reject("dataset/version mismatch");
  return { source: "override", datasetId: basemap.dataset_id, version: basemap.version, basemap };
}
