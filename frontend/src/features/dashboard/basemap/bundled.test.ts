import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { loadBasemap } from "@/features/dashboard/basemap/loadBasemap";
import { validateBasemap, validateManifest } from "@/features/dashboard/basemap/schema";
import { sha256Hex } from "@/features/dashboard/basemap/sha256";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const bytes = readFileSync(path.resolve(__dirname, "world-50m.v1.json"));
const manifest = validateManifest(JSON.parse(readFileSync(path.resolve(__dirname, "world-50m.v1.manifest.json"), "utf8")));

describe("the bundled basemap", () => {
  it("has a valid manifest whose bytes and sha256 match the data file", () => {
    if (!manifest.ok) throw new Error(manifest.reason);
    expect(manifest.value.file).toBe("world-50m.v1.json");
    expect(bytes.length).toBe(manifest.value.bytes);
    expect(sha256Hex(bytes)).toBe(manifest.value.sha256);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(manifest.value.sha256);
  });

  it("is a valid basemap that matches its manifest and covers the operating countries", () => {
    const checked = validateBasemap(JSON.parse(new TextDecoder().decode(bytes)));
    if (!checked.ok) throw new Error(checked.reason);
    if (!manifest.ok) throw new Error(manifest.reason);
    expect(checked.value.dataset_id).toBe(manifest.value.dataset_id);
    expect(checked.value.version).toBe(manifest.value.version);
    expect(checked.value.countries.length).toBeGreaterThanOrEqual(170);
    const codes = new Set(checked.value.countries.map((c) => c.iso2));
    for (const code of ["AE", "SA", "EG", "SG", "BH"]) expect(codes.has(code), code).toBe(true);
  });

  it("is what the loader falls back to, through the code-split import", async () => {
    const result = await loadBasemap({ fetchImpl: async () => new Response("", { status: 404 }) });
    expect(result.source).toBe("bundled");
    expect(result.version).toBe("5.1.2-r1");
    expect(result.datasetId).toBe("natural-earth-admin0-50m");
    expect(result.note).toBeUndefined();
  });
});
