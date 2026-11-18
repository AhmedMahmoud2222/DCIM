import { afterEach, describe, expect, it, vi } from "vitest";

import { BasemapResult, loadBasemap, MANIFEST_URL, overrideFileUrl } from "@/features/dashboard/basemap/loadBasemap";
import { box, encodeJson, manifestFor, tinyBasemap } from "@/features/dashboard/basemap/testFixtures";

/** Lets a test hand loadBasemap a manifest `file` the schema would refuse, to exercise the URL check behind it. */
const schemaBypass = vi.hoisted(() => ({ file: null as string | null }));
vi.mock("@/features/dashboard/basemap/schema", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/dashboard/basemap/schema")>();
  return {
    ...actual,
    validateManifest: (input: unknown) => {
      const checked = actual.validateManifest(input);
      return checked.ok && schemaBypass.file !== null ? { ...checked, value: { ...checked.value, file: schemaBypass.file } } : checked;
    },
  };
});

const BUNDLED = tinyBasemap();
const CUSTOM = tinyBasemap({ dataset_id: "custom-borders", version: "5.2.0", countries: [{ iso2: "AE", name: "UAE", polygons: [[box(51, 22, 57, 27)]] }] });
const CUSTOM_BYTES = encodeJson(CUSTOM);
const bundled = vi.fn(async () => BUNDLED as unknown);

type Reply = Response | Error;

/** A fetch that serves the given replies by URL and records every requested URL and init. */
function serve(replies: Record<string, () => Reply>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const replyFor = new Map(Object.entries(replies));
  const fetchImpl = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const reply = replyFor.get(url)?.() ?? new Response("not found", { status: 404 });
    if (reply instanceof Error) throw reply;
    return reply;
  };
  return { fetchImpl, calls };
}

const json = (bytes: Uint8Array<ArrayBuffer> | string, init: ResponseInit = {}) => () => new Response(bytes, { status: 200, ...init });

function override(manifest: unknown, data: () => Reply = json(CUSTOM_BYTES)) {
  return serve({ [MANIFEST_URL]: json(JSON.stringify(manifest)), "/map-data/basemap-test.json": data });
}

const customManifest = (overrides = {}) => manifestFor(CUSTOM_BYTES, { dataset_id: "custom-borders", version: "5.2.0", ...overrides });

async function expectBundled(result: BasemapResult, note: string | undefined) {
  expect(result.source).toBe("bundled");
  expect(result.basemap).toBe(BUNDLED);
  expect(result.version).toBe("5.1.2-r1");
  expect(result.note).toBe(note);
}

const rejection = (reason: string, version = "5.2.0") =>
  `Custom basemap${version ? ` ${version}` : ""} was rejected (${reason}); using bundled basemap 5.1.2-r1.`;

describe("loadBasemap", () => {
  it("uses the bundled basemap silently when there is no override (404)", async () => {
    const { fetchImpl, calls } = serve({});
    await expectBundled(await loadBasemap({ fetchImpl, bundled }), undefined);
    expect(calls).toEqual([{ url: "/map-data/manifest.json", init: expect.objectContaining({ cache: "no-store", credentials: "omit" }) }]);
  });

  it("treats the SPA's index.html served for the manifest as no override", async () => {
    const { fetchImpl } = serve({ [MANIFEST_URL]: json("<!doctype html><html></html>", { headers: { "content-type": "text/html" } }) });
    await expectBundled(await loadBasemap({ fetchImpl, bundled }), undefined);
  });

  it("treats a network error on the manifest as no override", async () => {
    const { fetchImpl } = serve({ [MANIFEST_URL]: () => new TypeError("Failed to fetch") });
    await expectBundled(await loadBasemap({ fetchImpl, bundled }), undefined);
  });

  it("uses a valid override, fetching only same-origin /map-data/ URLs", async () => {
    const { fetchImpl, calls } = override(customManifest());
    const result = await loadBasemap({ fetchImpl, bundled });
    expect(result).toEqual({ source: "override", datasetId: "custom-borders", version: "5.2.0", basemap: CUSTOM });
    expect(calls.map((c) => c.url)).toEqual(["/map-data/manifest.json", "/map-data/basemap-test.json"]);
    for (const call of calls) expect(call.init).toMatchObject({ cache: "no-store", credentials: "omit" });
  });

  it.each<[string, () => { fetchImpl: (url: string, init?: RequestInit) => Promise<Response> }, string]>([
    ["a hash mismatch", () => override(customManifest({ sha256: "0".repeat(64) })), rejection("checksum mismatch")],
    ["a byte count larger than the file", () => override(customManifest({ bytes: CUSTOM_BYTES.length + 1 })), rejection("size mismatch")],
    ["a byte count smaller than the file", () => override(customManifest({ bytes: CUSTOM_BYTES.length - 1 })), rejection("size mismatch")],
    ["an incompatible manifest schema_version", () => override({ ...customManifest(), schema_version: 2 }), rejection("incompatible schema", "")],
    [
      "an incompatible data schema_version",
      () => {
        const bytes = encodeJson({ ...CUSTOM, schema_version: 2 });
        return override(manifestFor(bytes, { dataset_id: "custom-borders", version: "5.2.0" }), json(bytes));
      },
      rejection("incompatible schema"),
    ],
    ["a file name escaping the directory", () => override(customManifest({ file: "../index.json" })), rejection("invalid manifest: invalid file name", "")],
    [
      "invalid data",
      () => {
        const bytes = encodeJson({ ...CUSTOM, countries: [{ iso2: "AE", name: "UAE", polygons: [[[0, 0, 1, 1]]] }] });
        return override(manifestFor(bytes, { dataset_id: "custom-borders", version: "5.2.0" }), json(bytes));
      },
      rejection("invalid data: ring has fewer than 3 points"),
    ],
    [
      "truncated JSON",
      () => {
        const bytes = CUSTOM_BYTES.slice(0, 40);
        return override(manifestFor(bytes, { dataset_id: "custom-borders", version: "5.2.0" }), json(bytes));
      },
      rejection("invalid data: not JSON"),
    ],
    ["a data fetch that rejects", () => override(customManifest(), () => new TypeError("Failed to fetch")), rejection("fetch failed")],
    ["a data file that is missing", () => override(customManifest(), () => new Response("", { status: 404 })), rejection("fetch failed, HTTP 404")],
    [
      "a declared Content-Length above the manifest bytes",
      () => override(customManifest(), json(CUSTOM_BYTES, { headers: { "content-length": String(10 * 1024 * 1024) } })),
      rejection("size mismatch"),
    ],
    [
      "a dataset/version that differs from the manifest",
      () => override(manifestFor(CUSTOM_BYTES, { dataset_id: "custom-borders", version: "5.3.0" })),
      rejection("dataset/version mismatch", "5.3.0"),
    ],
    ["a manifest server error", () => serve({ [MANIFEST_URL]: () => new Response("", { status: 500 }) }), rejection("manifest fetch failed, HTTP 500", "")],
  ])("falls back to the bundled basemap with a note on %s", async (_, setup, note) => {
    const { fetchImpl } = setup();
    await expectBundled(await loadBasemap({ fetchImpl, bundled }), note);
  });

  it("stops reading a streamed body as soon as it passes the manifest's byte count", async () => {
    let pulled = 0;
    const chunk = new Uint8Array(64 * 1024).fill(0x20);
    const endless = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        controller.enqueue(chunk);
      },
    });
    const { fetchImpl } = override(customManifest({ bytes: 100_000 }), () => new Response(endless, { status: 200 }));
    await expectBundled(await loadBasemap({ fetchImpl, bundled }), rejection("size mismatch"));
    expect(pulled).toBeLessThan(5);
  });

  it("returns an unavailable result, never a throw, when even the bundled basemap is invalid", async () => {
    const { fetchImpl } = serve({});
    const broken = async () => ({ ...BUNDLED, precision: 1 }) as unknown;
    const result = await loadBasemap({ fetchImpl, bundled: broken });
    expect(result).toMatchObject({ source: "unavailable", basemap: null });
    expect(result.note).toMatch(/bundled basemap is unavailable/);

    const failing = async () => Promise.reject(new Error("chunk failed"));
    const rejected = await loadBasemap({ fetchImpl: override(customManifest({ sha256: "1".repeat(64) })).fetchImpl, bundled: failing });
    expect(rejected.source).toBe("unavailable");
    expect(rejected.note).toBe("Custom basemap 5.2.0 was rejected (checksum mismatch). The bundled basemap is unavailable, so only the graticule is drawn.");
  });
});

const REFUSED_FILES = ["../x.json", "%2e%2e%2fx.json", "https://evil.example/x.json", "//evil.example/x.json", "a/b.json", "a\\b.json", "..\\x.json", "\\\\evil.example\\x.json"];

describe("overrideFileUrl", () => {
  afterEach(() => {
    schemaBypass.file = null;
  });

  it("resolves an allowed name to a same-origin path directly inside /map-data/", () => {
    expect(overrideFileUrl("basemap-test.json")).toBe("/map-data/basemap-test.json");
    expect(overrideFileUrl("basemap-test.json", "https://dcim.example")).toBe("/map-data/basemap-test.json");
  });

  it.each(REFUSED_FILES)("refuses %s", (file) => {
    expect(overrideFileUrl(file)).toBeNull();
    expect(overrideFileUrl(file, "https://dcim.example")).toBeNull();
  });

  it("refuses everything without an origin to resolve against", () => {
    expect(overrideFileUrl("basemap-test.json", "")).toBeNull();
    expect(overrideFileUrl("basemap-test.json", "null")).toBeNull();
  });

  it.each(REFUSED_FILES)("never fetches a refused manifest file %s and keeps the bundled basemap", async (file) => {
    schemaBypass.file = file;
    const { fetchImpl, calls } = override(customManifest());
    const spy = vi.fn(fetchImpl);
    await expectBundled(await loadBasemap({ fetchImpl: spy, bundled }), rejection("manifest file name is not allowed"));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(calls.map((c) => c.url)).toEqual([MANIFEST_URL]);
  });
});
