import { type BoundsMm, RACK_UNIT_MM, rackFootprint, rackHeightMm, rectCorners, sceneBounds } from "@/features/floor-plans/spatialMath";
import type { RoomEquipment, RoomRack, RoomRackEquipment, RoomSpatialView } from "@/types";

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

// --------------------------------------------------------------------------------------------- real-scale scene
/** The longest side of the room maps to this many CSS pixels; every other length uses the same scale. */
export const SCENE_BASE_PX = 760;

export interface SceneBox {
  id: string;
  kind: "rack" | "equipment";
  label: string;
  assetTag: string;
  /** px, relative to the scene floor's top-left */
  left: number;
  top: number;
  width: number;
  depth: number;
  height: number;
  rotationDeg: number;
  heightU?: number;
  /** authoritative millimetres, for the side panel */
  mm: { x: number; y: number; width: number; depth: number; height: number | null };
}

export interface ScenePin {
  id: string;
  label: string;
  left: number;
  top: number;
  reason: string;
}

export interface SceneModel {
  scale: number;
  floor: { width: number; height: number; boundsMm: BoundsMm };
  boundaryPx: [number, number][] | null;
  racks: SceneBox[];
  floorEquipment: SceneBox[];
  pins: ScenePin[];
  /** Nothing below is drawn: it is listed so missing data is visible rather than papered over. */
  missingRackPosition: RoomRack[];
  missingRackDimensions: RoomRack[];
  missingEquipmentPosition: RoomEquipment[];
  boundsSource: ReturnType<typeof sceneBounds>["source"];
}

/** Builds the 3D scene from authoritative data only. A rack with no recorded position, or no catalog footprint, is
 * not drawn at all (no fallback grid slot). Heights come from rack units (U x 44.45 mm) or catalog values. */
export function buildScene(view: RoomSpatialView, basePx = SCENE_BASE_PX): SceneModel | null {
  const { bounds, source } = sceneBounds(view);
  if (!bounds) return null;
  const span = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY, 1);
  const scale = basePx / span;
  const px = (mm: number, min: number) => (mm - min) * scale;

  const racks: SceneBox[] = [];
  const missingRackPosition: RoomRack[] = [];
  const missingRackDimensions: RoomRack[] = [];
  for (const rack of view.racks) {
    const fp = rackFootprint(rack);
    if (rack.x_mm == null || rack.y_mm == null) {
      missingRackPosition.push(rack);
      continue;
    }
    if (!fp) {
      missingRackDimensions.push(rack);
      continue;
    }
    const height = rackHeightMm(rack);
    racks.push({
      id: rack.id, kind: "rack", label: rack.name, assetTag: rack.asset_tag,
      left: px(rack.x_mm, bounds.minX), top: px(rack.y_mm, bounds.minY), width: fp.width * scale, depth: fp.depth * scale, height: height * scale,
      rotationDeg: rack.rotation_deg ?? 0, heightU: rack.height_u,
      mm: { x: rack.x_mm, y: rack.y_mm, width: fp.width, depth: fp.depth, height },
    });
  }

  const floorEquipment: SceneBox[] = [];
  const pins: ScenePin[] = [];
  const missingEquipmentPosition: RoomEquipment[] = [];
  for (const eq of view.equipment) {
    if (eq.position_state !== "placed" || eq.x_mm == null || eq.y_mm == null) {
      missingEquipmentPosition.push(eq);
      continue;
    }
    if (eq.width_mm && eq.depth_mm && eq.height_mm) {
      floorEquipment.push({
        id: eq.id, kind: "equipment", label: eq.hostname ?? eq.asset_tag, assetTag: eq.asset_tag,
        left: px(eq.x_mm, bounds.minX), top: px(eq.y_mm, bounds.minY), width: eq.width_mm * scale, depth: eq.depth_mm * scale, height: eq.height_mm * scale,
        rotationDeg: eq.rotation_deg ?? 0, mm: { x: eq.x_mm, y: eq.y_mm, width: eq.width_mm, depth: eq.depth_mm, height: eq.height_mm },
      });
    } else {
      pins.push({
        id: eq.id, label: eq.hostname ?? eq.asset_tag, left: px(eq.x_mm, bounds.minX), top: px(eq.y_mm, bounds.minY),
        reason: "position known, catalog dimensions incomplete (not drawn to scale)",
      });
    }
  }

  let boundaryPx: [number, number][] | null = null;
  const boundary = view.boundary;
  if (boundary) {
    const pts = boundary.geometry_data?.points?.length
      ? boundary.geometry_data.points
      : boundary.width_mm && boundary.height_mm
        ? rectCorners(boundary.x_mm, boundary.y_mm, boundary.width_mm, boundary.height_mm, boundary.rotation_deg)
        : null;
    boundaryPx = pts ? pts.map(([x, y]) => [px(x, bounds.minX), px(y, bounds.minY)] as [number, number]) : null;
  }

  return {
    scale,
    floor: { width: (bounds.maxX - bounds.minX) * scale, height: (bounds.maxY - bounds.minY) * scale, boundsMm: bounds },
    boundaryPx, racks, floorEquipment, pins, missingRackPosition, missingRackDimensions, missingEquipmentPosition, boundsSource: source,
  };
}

export { RACK_UNIT_MM };
