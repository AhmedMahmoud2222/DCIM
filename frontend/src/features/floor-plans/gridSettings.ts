import { useEffect, useState } from "react";

/** Grid / snap preferences are editor conveniences only: nothing here is ever stored with the layout. */
export interface GridSettings {
  showGrid: boolean;
  /** 0 = snapping off */
  snapMm: number;
}

export const SNAP_OPTIONS = [0, 10, 50, 100, 500, 1000];
const STORAGE_KEY = "dcim.spatial.grid";

export function useGridSettings(): [GridSettings, (next: GridSettings) => void] {
  const [settings, setSettings] = useState<GridSettings>({ showGrid: true, snapMm: 0 });
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<GridSettings>;
        setSettings({ showGrid: parsed.showGrid !== false, snapMm: SNAP_OPTIONS.includes(parsed.snapMm ?? 0) ? (parsed.snapMm ?? 0) : 0 });
      }
    } catch {
      /* storage unavailable: defaults apply */
    }
  }, []);
  const update = (next: GridSettings) => {
    setSettings(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };
  return [settings, update];
}
