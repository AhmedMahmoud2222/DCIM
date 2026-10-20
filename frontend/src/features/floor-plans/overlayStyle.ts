import type { OverlayItem, OverlayKind, OverlayState, RoomOverlays } from "@/types";

export const OVERLAY_KINDS: { id: OverlayKind; label: string; source: string }[] = [
  { id: "power", label: "Power", source: "Power topology and protection-device state" },
  { id: "network", label: "Network", source: "Authoritative cables (unconfirmed discovery is not shown as topology)" },
  { id: "environment", label: "Environment", source: "Latest temperature / humidity telemetry" },
];

export const STATE_COLORS: Record<OverlayState, string> = {
  normal: "#22c55e",
  warning: "#f59e0b",
  critical: "#ef4444",
  unavailable: "#64748b",
};

/** Non-colour cue so the state never depends on hue alone. */
export const STATE_SYMBOL: Record<OverlayState, string> = { normal: "✓", warning: "!", critical: "✕", unavailable: "?" };

export const STATE_LABEL: Record<OverlayState, string> = {
  normal: "Normal",
  warning: "Warning",
  critical: "Critical",
  unavailable: "No data",
};

export function overlayIndex(overlays: RoomOverlays | null | undefined, kind: OverlayKind | "none"): Map<string, OverlayItem> {
  const index = new Map<string, OverlayItem>();
  if (!overlays || kind === "none") return index;
  for (const item of overlays.overlays[kind]?.items ?? []) {
    // an asset can appear once per kind; the first (rack rollup or equipment) wins deterministically
    if (!index.has(item.asset_id)) index.set(item.asset_id, item);
  }
  return index;
}

export function describeOverlay(kind: OverlayKind, item: OverlayItem | undefined): string {
  if (!item) return `${kind}: no data`;
  const base = `${kind}: ${STATE_LABEL[item.state]} — ${item.reason}`;
  if (kind === "environment" && item.value != null) {
    const quality = item.data_quality === "measured" ? "measured" : item.data_quality === "stale" ? "stale" : String(item.data_quality ?? "");
    return `${base} (${item.metric} ${item.value} ${item.unit ?? ""}, ${quality})`;
  }
  if (kind === "network" && item.discovered_unconfirmed) {
    return `${base} (${item.discovered_unconfirmed} unconfirmed discovered neighbour(s), not authoritative)`;
  }
  return base;
}
