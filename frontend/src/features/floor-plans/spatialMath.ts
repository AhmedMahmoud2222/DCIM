import type { Calibration, CandidateGeometry, RoomSpatialView, SourceGeometry } from "@/types";

/** EIA-310 rack unit. The only constant relating U to millimetres; frame/plinth overhead is never invented. */
export const RACK_UNIT_MM = 44.45;

export interface BoundsMm {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface Viewport {
  /** pixels per millimetre */
  scale: number;
  /** pixel position of canonical (0, 0) */
  originX: number;
  originY: number;
  widthPx: number;
  heightPx: number;
}

export const mmToPx = (vp: Viewport, x: number, y: number): [number, number] => [vp.originX + x * vp.scale, vp.originY + y * vp.scale];
export const pxToMm = (vp: Viewport, px: number, py: number): [number, number] => [(px - vp.originX) / vp.scale, (py - vp.originY) / vp.scale];

/** Fits `bounds` (canonical mm) into a canvas `canvasWidthPx` wide, preserving aspect ratio and adding padding. */
export function fitViewport(bounds: BoundsMm, canvasWidthPx: number, paddingPx = 36, maxHeightPx = 720): Viewport {
  const w = Math.max(bounds.maxX - bounds.minX, 1);
  const h = Math.max(bounds.maxY - bounds.minY, 1);
  const scale = Math.min((canvasWidthPx - 2 * paddingPx) / w, (maxHeightPx - 2 * paddingPx) / h);
  return {
    scale,
    originX: paddingPx - bounds.minX * scale,
    originY: paddingPx - bounds.minY * scale,
    widthPx: Math.round(w * scale + 2 * paddingPx),
    heightPx: Math.round(h * scale + 2 * paddingPx),
  };
}

/** Zoom-aware engineering grid interval: the smallest 1/2/5 x 10^n mm step that is at least `minPx` wide on screen. */
export function niceGridInterval(pxPerMm: number, minPx = 40): number {
  if (!(pxPerMm > 0) || !Number.isFinite(pxPerMm)) return 1000;
  const rawMm = minPx / pxPerMm;
  const exponent = Math.floor(Math.log10(rawMm));
  const base = 10 ** exponent;
  for (const factor of [1, 2, 5, 10]) {
    if (factor * base >= rawMm) return factor * base;
  }
  return 10 * base;
}

export const snapToGrid = (valueMm: number, intervalMm: number): number =>
  intervalMm > 0 ? Math.round(valueMm / intervalMm) * intervalMm : valueMm;

/** Tick positions (mm) from `min` to `max`, aligned to multiples of `interval`, capped to keep the DOM small. */
export function gridTicks(min: number, max: number, interval: number, cap = 400): number[] {
  if (!(interval > 0)) return [];
  const ticks: number[] = [];
  for (let v = Math.ceil(min / interval) * interval; v <= max + 1e-9 && ticks.length < cap; v += interval) ticks.push(Math.round(v * 1000) / 1000 + 0);
  return ticks;
}

/** "600 mm", "1.20 m": millimetres below 1 m, metres (2 dp) above. */
export function formatLength(mm: number): string {
  const abs = Math.abs(mm);
  return abs >= 1000 ? `${(mm / 1000).toFixed(2)} m` : `${Math.round(mm)} mm`;
}

export function formatGridInterval(mm: number): string {
  return mm >= 1000 ? `${mm / 1000} m` : `${mm} mm`;
}

export type Point = [number, number];

/** Corners of an oriented rectangle: (x, y) is the top-left of the unrotated rectangle, rotation clockwise degrees about its centre. */
export function rectCorners(x: number, y: number, w: number, h: number, rotationDeg = 0): Point[] {
  const cx = x + w / 2;
  const cy = y + h / 2;
  const rad = (rotationDeg * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  return (
    [
      [-w / 2, -h / 2],
      [w / 2, -h / 2],
      [w / 2, h / 2],
      [-w / 2, h / 2],
    ] as Point[]
  ).map(([dx, dy]) => [cx + dx * cos - dy * sin, cy + dx * sin + dy * cos] as Point);
}

export function boundsOf(points: Point[]): BoundsMm {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return { minX: Math.min(...xs), minY: Math.min(...ys), maxX: Math.max(...xs), maxY: Math.max(...ys) };
}

/** Authoritative rack footprint, or null when the catalog dimensions are not available (never a default size). */
export function rackFootprint(rack: { width_mm?: number | null; depth_mm?: number | null }): { width: number; depth: number } | null {
  return rack.width_mm && rack.depth_mm ? { width: rack.width_mm, depth: rack.depth_mm } : null;
}

export function rackHeightMm(rack: { height_mm?: number | null; height_u: number }): number {
  return rack.height_mm ?? Math.round(rack.height_u * RACK_UNIT_MM);
}

export type BoundsSource = "boundary" | "room_dimensions" | "content" | "none";

/** What the 2D/3D scene is fitted to: the approved boundary, else recorded room dimensions, else the placed content. */
export function sceneBounds(view: RoomSpatialView): { bounds: BoundsMm | null; source: BoundsSource } {
  const boundary = view.boundary;
  if (boundary) {
    const points: Point[] = boundary.geometry_data?.points?.length
      ? boundary.geometry_data.points
      : boundary.width_mm && boundary.height_mm
        ? rectCorners(boundary.x_mm, boundary.y_mm, boundary.width_mm, boundary.height_mm, boundary.rotation_deg)
        : [];
    if (points.length) return { bounds: boundsOf(points), source: "boundary" };
  }
  if (view.room_width_mm && view.room_height_mm) {
    return { bounds: { minX: 0, minY: 0, maxX: view.room_width_mm, maxY: view.room_height_mm }, source: "room_dimensions" };
  }
  const pts: Point[] = [];
  for (const r of view.racks) {
    const fp = rackFootprint(r);
    if (fp && r.x_mm != null && r.y_mm != null) pts.push(...rectCorners(r.x_mm, r.y_mm, fp.width, fp.depth, r.rotation_deg ?? 0));
  }
  for (const o of view.objects) {
    if (o.width_mm && o.height_mm) pts.push(...rectCorners(o.x_mm, o.y_mm, o.width_mm, o.height_mm, o.rotation_deg));
  }
  return pts.length ? { bounds: boundsOf(pts), source: "content" } : { bounds: null, source: "none" };
}

// ---------------------------------------------------------------------------------------------- calibration
type CalibrationLike = Pick<Calibration, "mm_per_unit" | "origin_x" | "origin_y" | "y_axis" | "rotation_quadrants">;

/** Source drawing coordinates -> canonical room-local mm. Mirrors backend CalibrationParams.to_canonical exactly. */
export function sourceToMm(cal: CalibrationLike, x: number, y: number): Point {
  let u = (x - cal.origin_x) * cal.mm_per_unit;
  let v = (y - cal.origin_y) * cal.mm_per_unit;
  if (cal.y_axis === "up") v = -v;
  for (let i = 0; i < cal.rotation_quadrants % 4; i += 1) [u, v] = [-v, u];
  return [u + 0, v + 0]; // `+ 0` normalises -0
}

export function mmToSource(cal: CalibrationLike, X: number, Y: number): Point {
  let u = X;
  let v = Y;
  for (let i = 0; i < cal.rotation_quadrants % 4; i += 1) [u, v] = [v, -u];
  if (cal.y_axis === "up") v = -v;
  return [u / cal.mm_per_unit + cal.origin_x + 0, v / cal.mm_per_unit + cal.origin_y + 0];
}

/** Source-space rectangle centre/size/rotation -> canonical top-left rectangle (matches backend rect_to_canonical). */
export function rectSourceToMm(cal: CalibrationLike, g: { cx: number; cy: number; width: number; height: number; rotation_deg?: number }) {
  const [mx, my] = sourceToMm(cal, g.cx, g.cy);
  const w = g.width * cal.mm_per_unit;
  const h = g.height * cal.mm_per_unit;
  const rot = (cal.y_axis === "down" ? (g.rotation_deg ?? 0) : -(g.rotation_deg ?? 0)) + 90 * (cal.rotation_quadrants % 4);
  return { x_mm: mx - w / 2, y_mm: my - h / 2, width_mm: w, height_mm: h, rotation_deg: ((rot % 360) + 360) % 360 };
}

/** Inverse of rectSourceToMm: an edited canonical rectangle back to source-space centre/size/rotation. */
export function rectMmToSource(cal: CalibrationLike, r: { x_mm: number; y_mm: number; width_mm: number; height_mm: number; rotation_deg: number }) {
  const [cx, cy] = mmToSource(cal, r.x_mm + r.width_mm / 2, r.y_mm + r.height_mm / 2);
  const local = r.rotation_deg - 90 * (cal.rotation_quadrants % 4);
  const rot = cal.y_axis === "down" ? local : -local;
  const norm = ((((rot + 180) % 360) + 360) % 360) - 180;
  return { cx, cy, width: r.width_mm / cal.mm_per_unit, height: r.height_mm / cal.mm_per_unit, rotation_deg: norm };
}

export function describeCalibration(cal: Pick<Calibration, "method" | "mm_per_unit" | "error_bound_mm" | "confidence" | "source_units">): string {
  const scale = `1 ${cal.source_units} = ${Number(cal.mm_per_unit.toPrecision(6))} mm`;
  const bound = cal.error_bound_mm == null ? "error bound unverified" : `error ≤ ±${cal.error_bound_mm} mm`;
  return `${scale} · ${bound} · ${cal.confidence} confidence`;
}

export function isRect(g: CandidateGeometry): g is CandidateGeometry & { cx: number; cy: number; width: number; height: number } {
  return g.shape_type === "rect" && g.cx != null && g.cy != null && g.width != null && g.height != null;
}

export const REASON_LABELS: Record<string, string> = {
  no_active_floor_plan: "No active floor plan for this room",
  no_calibration: "The floor plan is not calibrated",
  no_room_boundary: "No approved room boundary",
};

export function explainIncomplete(reason: string): string {
  if (reason in REASON_LABELS) return REASON_LABELS[reason];
  const [code, count] = reason.split(":");
  const n = Number(count);
  const noun = n === 1 ? "" : "s";
  switch (code) {
    case "rack_position_missing":
      return `${n} rack${noun} placed in this room ${n === 1 ? "has" : "have"} no recorded floor position`;
    case "equipment_position_missing":
      return `${n} floor-standing equipment item${noun} ${n === 1 ? "has" : "have"} no linked position`;
    case "equipment_dimensions_missing":
      return `${n} floor-standing equipment item${noun} ${n === 1 ? "has" : "have"} incomplete catalog dimensions`;
    default:
      return reason;
  }
}

export type DisplayCal = Pick<Calibration, "mm_per_unit" | "origin_x" | "origin_y" | "y_axis" | "rotation_quadrants">;

/** Until a calibration exists the drawing is shown in its own units (Y flipped for Y-up sources so it is upright).
 * Nothing drawn in that mode is stored: it is a viewing aid, labelled as uncalibrated. */
export function provisionalCalibration(source: SourceGeometry | null): DisplayCal {
  const box = source?.bbox;
  return {
    mm_per_unit: 1,
    origin_x: box?.min_x ?? 0,
    origin_y: source?.y_axis === "up" ? (box?.max_y ?? 0) : (box?.min_y ?? 0),
    y_axis: source?.y_axis ?? "down",
    rotation_quadrants: 0,
  };
}
