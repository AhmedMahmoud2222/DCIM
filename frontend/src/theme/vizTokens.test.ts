import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const srcDir = path.resolve(__dirname, "..");
const themes = readFileSync(path.resolve(__dirname, "themes.css"), "utf8");
const lightStart = themes.indexOf(':root[data-theme="light"]');
const darkBlock = themes.slice(0, lightStart);
const lightBlock = themes.slice(lightStart);

const definedIn = (block: string) => new Set(Array.from(block.matchAll(/^\s*(--viz-[a-z0-9-]+)\s*:/gm)).map((match) => match[1]));

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.(tsx?|css)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : [];
  });
}

const used = new Set<string>();
for (const file of sourceFiles(srcDir)) {
  for (const match of readFileSync(file, "utf8").matchAll(/var\((--viz-[a-z0-9-]+)\)/g)) used.add(match[1]);
}

describe("theme-aware visualization tokens", () => {
  it("are used by the SVG, canvas and 3D views", () => {
    expect(used.size).toBeGreaterThan(10);
  });

  it("define the same set in Dark and Normal", () => {
    expect(Array.from(definedIn(lightBlock)).sort()).toEqual(Array.from(definedIn(darkBlock)).sort());
  });

  it.each(Array.from(used).sort())("%s is defined in both themes", (name) => {
    expect(definedIn(darkBlock).has(name), `${name} missing in Dark`).toBe(true);
    expect(definedIn(lightBlock).has(name), `${name} missing in Normal`).toBe(true);
  });

  it("gives Normal values that differ from Dark for the colors drawn on the page background", () => {
    const value = (block: string, name: string) => block.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1].trim();
    for (const name of ["--viz-label", "--viz-axis", "--viz-strong", "--viz-halo", "--viz-accent", "--viz-3d-sky"]) {
      expect(value(lightBlock, name), name).not.toBe(value(darkBlock, name));
    }
  });
});
