import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyTheme, resolveTheme, setTheme, THEME_STORAGE_KEY } from "./theme";

function mockSystem(prefersDark: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(prefers-color-scheme: dark)" ? prefersDark : false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

describe("resolveTheme", () => {
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    vi.restoreAllMocks();
    document.documentElement.removeAttribute("data-theme");
  });

  it.each(["light", "dark"] as const)("restores a stored %s choice over the system setting", (stored) => {
    mockSystem(stored === "light");
    window.localStorage.setItem(THEME_STORAGE_KEY, stored);
    expect(resolveTheme()).toBe(stored);
  });

  it("follows the system setting when nothing is stored", () => {
    mockSystem(true);
    expect(resolveTheme()).toBe("dark");
    mockSystem(false);
    expect(resolveTheme()).toBe("light");
  });

  it("ignores an invalid stored value and follows the system setting", () => {
    mockSystem(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(resolveTheme()).toBe("dark");
  });

  it("falls back to Normal when matchMedia is missing", () => {
    // @ts-expect-error simulate an environment without matchMedia
    window.matchMedia = undefined;
    expect(resolveTheme()).toBe("light");
  });

  it("falls back to the system setting when localStorage throws", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(resolveTheme()).toBe("dark");
  });

  it("still applies the choice when localStorage cannot be written", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    expect(() => setTheme("dark")).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("applyTheme sets the data-theme attribute", () => {
    applyTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
