import { readFileSync } from "node:fs";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resolveTheme } from "@/lib/theme";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const html = readFileSync(path.resolve(__dirname, "../../index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? "";

function runBootstrap() {
  new Function(script)();
}

function mockSystem(prefersDark: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(prefers-color-scheme: dark)" ? prefersDark : false,
  })) as unknown as typeof window.matchMedia;
}

describe("index.html theme bootstrap", () => {
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    vi.restoreAllMocks();
  });

  it("is an inline script that runs before the app bundle", () => {
    expect(script).toContain("data-theme");
    expect(html.indexOf("<script>")).toBeLessThan(html.indexOf('type="module"'));
  });

  it("applies a stored theme", () => {
    window.localStorage.setItem("dcim-theme", "dark");
    runBootstrap();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("falls back to the system setting for an invalid stored value", () => {
    mockSystem(true);
    window.localStorage.setItem("dcim-theme", "sepia");
    runBootstrap();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("does not throw and falls back when localStorage is inaccessible", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(runBootstrap).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("does not throw and picks Normal when matchMedia is missing", () => {
    // @ts-expect-error simulate an environment without matchMedia
    window.matchMedia = undefined;
    expect(runBootstrap).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it.each([
    [null, true],
    [null, false],
    ["light", true],
    ["dark", false],
    ["bogus", true],
  ] as const)("agrees with resolveTheme (stored %s, system dark %s)", (stored, systemDark) => {
    mockSystem(systemDark);
    if (stored) window.localStorage.setItem("dcim-theme", stored);
    runBootstrap();
    expect(document.documentElement.getAttribute("data-theme")).toBe(resolveTheme());
  });
});
