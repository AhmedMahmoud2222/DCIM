import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { UNIT_LABEL_COLOUR, UNIT_STATUS } from "./unitStatus";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

function luminance(color: string): number {
  const channels = [1, 3, 5].map((i) => {
    const value = parseInt(color.slice(i, i + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}
const contrast = (a: string, b: string) => (Math.max(luminance(a), luminance(b)) + 0.05) / (Math.min(luminance(a), luminance(b)) + 0.05);

describe("cooling unit marker contrast", () => {
  it.each(Object.entries(UNIT_STATUS))("%s: the label reaches 4.5:1 on the fill (9 px bold text)", (_status, { colour }) => {
    expect(colour).toMatch(/^#[0-9a-f]{6}$/i);
    expect(contrast(UNIT_LABEL_COLOUR, colour)).toBeGreaterThanOrEqual(4.5);
  });

  it("draws the marker with the same fixed label colour and an opaque fill, so the ratio above is the real one", () => {
    const source = readFileSync(path.resolve(__dirname, "ThermalLayers.tsx"), "utf8");
    expect(source).toContain(`fill="${UNIT_LABEL_COLOUR}"`);
    const marker = source.match(/<rect[^>]*fill=\{status\.colour\}[^>]*>/)?.[0] ?? "";
    expect(marker).not.toBe("");
    expect(marker).not.toMatch(/fillOpacity/);
  });
});
