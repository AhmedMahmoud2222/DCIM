import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

// Vitest routes .css imports (even with ?raw) through its CSS pipeline, and the jsdom
// environment replaces URL, so resolve the file from __dirname and read it directly.
const css =readFileSync(path.resolve(__dirname, "../index.css"), "utf8");

/** Reads a `--color-<name>: #rrggbb;` declaration from index.css, the single source of truth. */
function token(name: string): string {
  const match = css.match(new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`));
  if (!match) throw new Error(`token --color-${name} is not defined in index.css`);
  return match[1];
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => {
    const value = parseInt(hex.slice(index, index + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(foreground: string, background: string): number {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

// WCAG 2.2 AA: 4.5:1 for normal text, 3:1 for icons, control boundaries and focus indicators.
const TEXT = 4.5;
const NON_TEXT = 3;

const PAIRS: ReadonlyArray<[label: string, foreground: string, background: string, minimum: number]> = [
  ["primary text on page", token("fg-primary"), token("surface-page"), TEXT],
  ["primary text on panel", token("fg-primary"), token("surface-panel"), TEXT],
  ["secondary text on panel", token("fg-secondary"), token("surface-panel"), TEXT],
  ["sidebar text on sidebar", token("side-fg"), token("surface-sidebar"), TEXT],
  ["sidebar group label on sidebar", token("side-fg-muted"), token("surface-sidebar"), TEXT],
  ["selected item text on selected background", "#ffffff", token("side-selected"), TEXT],
  ["primary action text on primary action", token("action-primary-fg"), token("action-primary"), TEXT],
  ["link on page", token("link"), token("surface-page"), TEXT],
  ["link on panel", token("link"), token("surface-panel"), TEXT],
  ["selected item marker on sidebar", token("link"), token("surface-sidebar"), NON_TEXT],
  ["focus ring on page", token("focus"), token("surface-page"), NON_TEXT],
  ["focus ring on panel", token("focus"), token("surface-panel"), NON_TEXT],
  ["focus ring on sidebar", token("focus-on-sidebar"), token("surface-sidebar"), NON_TEXT],
  ["focus ring on selected sidebar item", token("focus-on-sidebar"), token("side-selected"), NON_TEXT],
  ["control edge on panel", token("line-control"), token("surface-panel"), NON_TEXT],
  ["small control edge on sidebar", token("side-edge"), token("surface-sidebar"), NON_TEXT],
];

const TONES = ["neutral", "info", "ok", "caution", "serious", "danger", "attention", "unknown"] as const;

const TONE_PAIRS: ReadonlyArray<[label: string, foreground: string, background: string, minimum: number]> = TONES.flatMap((tone) => [
  [`${tone} tone text on its background`, token(`tone-${tone}-fg`), token(`tone-${tone}-bg`), TEXT] as [string, string, string, number],
  // The chip border and icon use the foreground color; they must stand out from the panel behind the chip.
  [`${tone} tone border and icon on panel`, token(`tone-${tone}-fg`), token("surface-panel"), NON_TEXT] as [string, string, string, number],
]);

describe("Control Room palette (index.css)", () => {
  it.each(PAIRS)("%s meets its WCAG AA contrast", (_label, foreground, background, minimum) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(minimum);
  });

  it.each(TONE_PAIRS)("%s meets its WCAG AA contrast", (_label, foreground, background, minimum) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(minimum);
  });

  it("emits every token on :root (theme static) so plain CSS and a future theme attribute can use them", () => {
    expect(css).toMatch(/@theme\s+static\s*\{/);
  });

  it("does not redefine any existing palette scale (slate, red, green and so on)", () => {
    // Only definitions count; reading var(--color-gray-200) elsewhere in the file is fine.
    expect(css).not.toMatch(/^\s*--color-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d+\s*:/m);
  });

  it("defines the focus rule outside any @layer so it wins over focus:outline-hidden utilities", () => {
    const ruleStart = css.indexOf(":focus-visible:not([class*=\"layout3d-\"])");
    expect(ruleStart).toBeGreaterThan(-1);
    // Count braces before the rule: an unlayered top-level rule is at depth zero.
    let depth = 0;
    for (const character of css.slice(0, ruleStart)) {
      if (character === "{") depth += 1;
      if (character === "}") depth -= 1;
    }
    expect(depth).toBe(0);
  });

  it("has a reduced-motion rule", () => {
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
  });
});
