import type { RoomRack, RoomRackEquipment } from "@/types";

/** A safe-to-render vertical placement for one item in a rack elevation: percentages of
 * the rack's own face height (0% = top of the rack, matching RackElevationView.tsx's
 * "U1 remains at the bottom" convention — u_end is the authoritative boundary). */
export interface UPlacement {
  topPct: number;
  heightPct: number;
  /** True when the source u_start/u_end/height_u combination was out of range or
   * inverted — the placement below is a clamped, safe approximation, not the literal
   * authoritative value, and callers should flag this visibly rather than pretend it's
   * exact. */
  irregular: boolean;
}

/** Converts an authoritative [u_start, u_end) range (u_end exclusive, same convention
 * the backend/2D elevation already use) into a percentage-of-rack-height placement.
 * Clamps out-of-range or inverted input instead of producing a negative/over-100%
 * layout, and reports that clamping happened via `irregular` so the UI can show a
 * visible "irregular placement" indicator rather than silently misrepresenting it. */
export function uRangeToPlacement(uStart: number, uEnd: number, heightU: number): UPlacement {
  if (!Number.isFinite(heightU) || heightU <= 0) {
    return { topPct: 0, heightPct: 100, irregular: true };
  }
  const rawStart = uStart;
  const rawEnd = uEnd;
  const start = Math.min(uStart, uEnd);
  let end = Math.max(uStart, uEnd);
  if (end <= start) end = start + 1;
  const clampedStart = Math.min(Math.max(start, 1), heightU);
  const clampedEnd = Math.min(Math.max(end, clampedStart + 1), heightU + 1);
  const irregular = rawStart >= rawEnd || rawStart < 1 || rawEnd > heightU + 1 || clampedStart !== start || clampedEnd !== end;

  const topPct = ((heightU - clampedEnd + 1) / heightU) * 100;
  const heightPct = ((clampedEnd - clampedStart) / heightU) * 100;
  return { topPct: Math.max(0, Math.min(100, topPct)), heightPct: Math.max(0, Math.min(100, heightPct)), irregular };
}

/** Which rack face(s) an item's placement.side belongs on — "both" renders on both, so
 * front/rear equipment sharing a U range is represented rather than merged/hidden. */
export function occupiesFace(side: RoomRackEquipment["side"], face: "front" | "rear"): boolean {
  return side === "both" || side === face;
}

/** Racks in the active room's spatial view that have a placement but no drawn x/y yet —
 * distinct from genuinely unplaced inventory: these racks ARE placed in this room (a
 * valid, non-coordinate placement state), just not yet positioned on the floor plan. */
export function racksMissingCoordinates(racks: RoomRack[]): RoomRack[] {
  return racks.filter((rack) => rack.x_mm === null || rack.y_mm === null);
}
