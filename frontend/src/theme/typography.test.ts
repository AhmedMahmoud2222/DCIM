import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const srcDir = path.resolve(__dirname, "..");
const css = readFileSync(path.resolve(srcDir, "index.css"), "utf8");
const indexHtml = readFileSync(path.resolve(srcDir, "../index.html"), "utf8");

const faces = css.match(/@font-face\s*\{[^}]*\}/g) ?? [];

function declared(name: string): string {
  const match = css.match(new RegExp(`--font-${name}:\\s*([^;]+);`));
  if (!match) throw new Error(`--font-${name} is not defined in index.css`);
  return match[1];
}

describe("bundled typography (index.css)", () => {
  it("declares the five IBM Plex faces: Sans 400, 500, 600 and Mono 400, 500", () => {
    const summary = faces
      .map((face) => `${face.match(/font-family:\s*"([^"]+)"/)?.[1]} ${face.match(/font-weight:\s*(\d+)/)?.[1]}`)
      .sort();
    expect(summary).toEqual(["IBM Plex Mono 400", "IBM Plex Mono 500", "IBM Plex Sans 400", "IBM Plex Sans 500", "IBM Plex Sans 600"]);
  });

  it("loads every face from a local file that exists and is a WOFF2", () => {
    for (const face of faces) {
      const url = face.match(/url\("([^"]+)"\)/)?.[1];
      expect(url, face).toMatch(/^\.\/assets\/fonts\/[a-z0-9-]+\.woff2$/);
      expect(existsSync(path.resolve(srcDir, url!)), `${url} is missing`).toBe(true);
      expect(face).toContain('format("woff2")');
    }
  });

  it("makes no external font request: no remote URL in the stylesheet or in index.html", () => {
    expect(css).not.toMatch(/url\(\s*["']?(https?:)?\/\//i);
    expect(css).not.toMatch(/@import\s+(url\()?["']?(https?:)?\/\//i);
    expect(indexHtml).not.toMatch(/fonts\.(googleapis|gstatic)\.com|use\.typekit|cdn\.jsdelivr|unpkg\.com|cdnjs/i);
    expect(indexHtml).not.toMatch(/<link[^>]+rel=["']stylesheet["'][^>]+href=["']https?:/i);
  });

  it("never hides text while a font loads (font-display: swap on every face)", () => {
    for (const face of faces) expect(face).toMatch(/font-display:\s*swap/);
  });

  it("keeps the licence texts next to the files", () => {
    for (const name of ["OFL-IBM-Plex-Sans.txt", "OFL-IBM-Plex-Mono.txt", "README.md"]) {
      expect(existsSync(path.resolve(srcDir, "assets/fonts", name)), name).toBe(true);
    }
    expect(readFileSync(path.resolve(srcDir, "assets/fonts/OFL-IBM-Plex-Sans.txt"), "utf8")).toMatch(/SIL OPEN FONT LICENSE Version 1\.1/i);
    expect(readFileSync(path.resolve(srcDir, "assets/fonts/OFL-IBM-Plex-Mono.txt"), "utf8")).toMatch(/SIL OPEN FONT LICENSE Version 1\.1/i);
  });

  it("puts IBM Plex first and keeps a complete system fallback stack ending in a generic family", () => {
    const sans = declared("sans");
    expect(sans.trimStart()).toMatch(/^"IBM Plex Sans",/);
    for (const family of ["ui-sans-serif", "system-ui", "Segoe UI", "Roboto", "Arial"]) expect(sans).toContain(family);
    expect(sans.trimEnd()).toMatch(/sans-serif$/);

    const mono = declared("mono");
    expect(mono.trimStart()).toMatch(/^"IBM Plex Mono",/);
    for (const family of ["ui-monospace", "Menlo", "Consolas"]) expect(mono).toContain(family);
    expect(mono.trimEnd()).toMatch(/monospace$/);
  });

  it("lets the user's text-size setting scale type and lines digits up in tables", () => {
    expect(css).toMatch(/text-size-adjust:\s*100%/);
    expect(css).toMatch(/table\s*\{[^}]*font-variant-numeric:\s*tabular-nums/);
  });
});
