import { useCallback, useSyncExternalStore } from "react";

/** "light" is shown to people as Normal mode, "dark" as Dark mode. */
export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "dcim-theme";
export const THEME_LABELS: Record<Theme, string> = { light: "Normal", dark: "Dark" };

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

function readStored(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    // Storage can be blocked (private windows, site data off). The theme then follows the system.
    return null;
  }
}

function systemTheme(): Theme {
  if (typeof window.matchMedia !== "function") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The stored choice, else the operating system setting, else Normal. index.html runs the same
 * logic before first paint so the page never flashes the wrong theme. */
export function resolveTheme(): Theme {
  return readStored() ?? systemTheme();
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
}

const listeners = new Set<() => void>();

function currentTheme(): Theme {
  const attribute = document.documentElement.getAttribute("data-theme");
  return isTheme(attribute) ? attribute : resolveTheme();
}

export function setTheme(theme: Theme): void {
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Not persisted; the choice still applies until the tab closes.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTheme(): readonly [Theme, (theme: Theme) => void] {
  const theme = useSyncExternalStore(subscribe, currentTheme, () => "light" as Theme);
  const update = useCallback((next: Theme) => setTheme(next), []);
  return [theme, update] as const;
}
