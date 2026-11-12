import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const srcDir = path.resolve(__dirname, "..");
const themes = readFileSync(path.resolve(__dirname, "themes.css"), "utf8");
const index = readFileSync(path.resolve(srcDir, "index.css"), "utf8");
const lightStart = themes.indexOf(':root[data-theme="light"]');
const blocks = { Dark: themes.slice(0, lightStart), Normal: themes.slice(lightStart) } as const;

function hex(block: string, name: string): string {
  const match = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`)) ?? index.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`));
  if (!match) throw new Error(`${name} is not a #rrggbb value`);
  return match[1];
}

function luminance(color: string): number {
  const channels = [1, 3, 5].map((i) => {
    const value = parseInt(color.slice(i, i + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

/** Tokens that SVG text may use. Each must reach 4.5:1 on the label halo and on the canvas background
 * (`bg-slate-950`) in both themes, because labels are 8 to 11 px. */
const TEXT_TOKENS = ["--viz-label", "--viz-axis", "--viz-strong", "--viz-muted", "--viz-warn", "--viz-accent"];

describe.each(Object.entries(blocks))("%s theme: SVG text tokens", (_theme, block) => {
  it.each(TEXT_TOKENS)("%s is readable on the halo and on the canvas background", (token) => {
    const text = hex(block, token);
    expect(contrast(text, hex(block, "--viz-halo")), `${token} on halo`).toBeGreaterThanOrEqual(4.5);
    expect(contrast(text, hex(block, "--color-slate-950")), `${token} on canvas`).toBeGreaterThanOrEqual(4.5);
  });
});

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [full] : [];
  });
}

/** Text drawn on a fixed dark or colored shape keeps a fixed foreground. Every other SVG `<text>` must use
 * a text-safe token, never a data hue (status, zone or rack color), which fails on the Normal background. */
const ALLOWED_FIXED = new Set(['"#f1f5f9"', '"#f8fafc"', '"#fecaca"', '{p.state === "measured_fresh" ? "#0f172a" : meta.colour}']);
const SAFE_FILL = new RegExp(`^"var\\((${TEXT_TOKENS.join("|")})\\)"$`);

describe("SVG text foregrounds", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(srcDir)) {
    const source = readFileSync(file, "utf8");
    for (const tag of source.matchAll(/<text\b[^>]*>/g)) {
      const fill = tag[0].match(/\bfill=("[^"]*"|\{(?:[^{}]|\{[^}]*\})*\})/)?.[1];
      if (!fill) continue;
      const braced = fill.startsWith("{") ? fill.slice(1, -1) : fill;
      const ternary = fill.match(/^\{([a-zA-Z]+) \? ("var\(--viz-(?:strong|label)\)") : ("var\(--viz-(?:strong|label)\)")\}$/);
      if (SAFE_FILL.test(braced) || ALLOWED_FIXED.has(fill) || ALLOWED_FIXED.has(braced) || ternary) continue;
      offenders.push(`${path.relative(srcDir, file)}: fill=${fill}`);
    }
  }

  it("uses a text-safe token or an allowed fixed color for every <text> fill", () => {
    expect(offenders).toEqual([]);
  });
});
