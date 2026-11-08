import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { OverlayKind, RoomOverlays, RoomSpatialView, SpatialObject } from "@/types";
import { STATE_COLORS, STATE_LABEL, STATE_SYMBOL, describeOverlay, overlayIndex } from "./overlayStyle";
import { SceneChrome } from "./SceneChrome";
import {
  type BoundsMm,
  type Point,
  type Viewport,
  explainIncomplete,
  fitViewport,
  formatLength,
  mmToPx,
  rackFootprint,
  rectCorners,
  sceneBounds,
} from "./spatialMath";

const DEFAULT_CANVAS_WIDTH_PX = 880;

const OBJECT_STYLE: Record<string, { stroke: string; fill: string; width: number; dash?: string }> = {
  room_outline: { stroke: "#38bdf8", fill: "none", width: 2.5 },
  wall: { stroke: "#94a3b8", fill: "none", width: 3 },
  column: { stroke: "#64748b", fill: "rgb(100 116 139 / .55)", width: 1 },
  obstacle: { stroke: "#a78bfa", fill: "rgb(167 139 250 / .2)", width: 1 },
  aisle: { stroke: "#2dd4bf", fill: "rgb(45 212 191 / .08)", width: 1, dash: "6 4" },
  rack: { stroke: "#60a5fa", fill: "none", width: 1, dash: "3 3" },
  equipment: { stroke: "#2dd4bf", fill: "none", width: 1, dash: "3 3" },
  annotation: { stroke: "#64748b", fill: "none", width: 1 },
  imported_shape: { stroke: "#64748b", fill: "none", width: 1, dash: "4 2" },
};

function polygonPoints(o: SpatialObject): Point[] | null {
  const pts = o.geometry_data?.points;
  if (pts && pts.length >= 2) return pts as Point[];
  if (o.width_mm && o.height_mm) return rectCorners(o.x_mm, o.y_mm, o.width_mm, o.height_mm, o.rotation_deg);
  return null;
}

/** Dimensionally accurate rendering of authoritative placement: every size on screen is real millimetres x a single
 * scale, never a CSS constant. Racks use their catalog width/depth, floor equipment its catalog footprint; anything
 * whose position or dimensions are not recorded is listed as incomplete instead of being drawn at a guessed place. */
export function RoomSpatialCanvas({
  view,
  overlay = "none",
  overlays = null,
  showGrid = true,
  canvasWidthPx = DEFAULT_CANVAS_WIDTH_PX,
  renderUnderlay,
  renderLayers,
  extraLegend,
}: {
  view: RoomSpatialView;
  overlay?: OverlayKind | "none";
  overlays?: RoomOverlays | null;
  showGrid?: boolean;
  canvasWidthPx?: number;
  /** Extra SVG drawn in the same calibrated millimetre space on top of racks and equipment (Issue #105 thermal layers).
   * It receives the viewport so it shares the one scale; it never changes how racks or equipment are positioned. */
  renderLayers?: (scene: { vp: Viewport; bounds: BoundsMm }) => ReactNode;
  /** Same space, drawn beneath objects, racks and equipment (so it can never intercept their clicks). */
  renderUnderlay?: (scene: { vp: Viewport; bounds: BoundsMm }) => ReactNode;
  extraLegend?: ReactNode;
}) {
  const { bounds, source } = sceneBounds(view);
  const states = overlayIndex(overlays, overlay);
  const unpositionedRacks = view.racks.filter((r) => r.x_mm == null || r.y_mm == null || !rackFootprint(r));
  const unpositionedEquipment = view.equipment.filter((e) => e.position_state !== "placed");
  const reasons = view.incomplete_reasons ?? [];

  if (!bounds) {
    return (
      <div role="status" data-testid="spatial-2d-empty" className="rounded-sm border border-dashed border-slate-700 p-6 text-sm text-slate-400">
        There is no room boundary, recorded room size or positioned rack to draw yet. Set a room boundary, or place racks with coordinates, and the
        scaled plan appears here. {reasons.length > 0 && <span>({reasons.map(explainIncomplete).join("; ")})</span>}
      </div>
    );
  }

  const vp = fitViewport(bounds, canvasWidthPx);
  const calibration = view.calibration;

  return (
    <div data-testid="spatial-2d">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs" data-testid="layout-state" data-layout-state={view.layout_state ?? "incomplete"}>
        {view.layout_state === "validated" ? (
          <span className="rounded-sm bg-green-900 px-2 py-0.5 text-green-100">Validated layout</span>
        ) : (
          <span className="rounded-sm bg-yellow-900 px-2 py-0.5 text-yellow-100">Incomplete layout</span>
        )}
        <span className="text-slate-500" data-testid="scale-source">
          {source === "boundary" && "Fitted to the approved room boundary"}
          {source === "room_dimensions" && "Fitted to the recorded room dimensions (no approved boundary polygon)"}
          {source === "content" && "No boundary or room size recorded: fitted to the placed racks"}
        </span>
        {calibration ? (
          <span className="text-slate-400" data-testid="calibration-summary">
            Calibration: 1 {calibration.source_units} = {Number(calibration.mm_per_unit.toPrecision(6))} mm ·{" "}
            {calibration.error_bound_mm == null ? "error bound unverified" : `error ≤ ±${calibration.error_bound_mm} mm`} · {calibration.confidence} confidence
          </span>
        ) : (
          <span className="text-slate-500">No calibrated import is active for this room.</span>
        )}
      </div>
      {reasons.length > 0 && (
        <ul className="mb-2 list-disc pl-5 text-xs text-yellow-400" data-testid="incomplete-reasons">
          {reasons.map((r) => (
            <li key={r}>{explainIncomplete(r)}</li>
          ))}
        </ul>
      )}

      <svg
        width={vp.widthPx}
        height={vp.heightPx}
        viewBox={`0 0 ${vp.widthPx} ${vp.heightPx}`}
        role="group"
        aria-label={`Scaled 2D plan of ${view.room_name}`}
        className="max-w-full rounded-sm border border-slate-800 bg-slate-950"
      >
        <SceneChrome vp={vp} bounds={bounds} showGrid={showGrid} />
        {renderUnderlay?.({ vp, bounds })}

        {view.objects.map((o) => {
          const style = OBJECT_STYLE[o.object_type] ?? OBJECT_STYLE.imported_shape;
          const pts = polygonPoints(o);
          if (o.geometry_type === "text") {
            const [px, py] = mmToPx(vp, o.x_mm, o.y_mm);
            return (
              <text key={o.id} x={px} y={py} fontSize={9} fill="#94a3b8">
                {o.label}
              </text>
            );
          }
          if (!pts) return null;
          const px = pts.map(([x, y]) => mmToPx(vp, x, y).join(",")).join(" ");
          const open = o.geometry_type === "path";
          const Shape = open ? "polyline" : "polygon";
          return (
            <g key={o.id} data-object-id={o.id} data-object-type={o.object_type}>
              <Shape points={px} fill={open ? "none" : style.fill} stroke={style.stroke} strokeWidth={style.width} strokeDasharray={style.dash} />
              <title>{`${o.object_type}${o.label ? ` · ${o.label}` : ""} (${o.source})`}</title>
            </g>
          );
        })}

        {view.racks.map((rack) => {
          const fp = rackFootprint(rack);
          if (rack.x_mm == null || rack.y_mm == null || !fp) return null;
          const [px, py] = mmToPx(vp, rack.x_mm, rack.y_mm);
          const w = fp.width * vp.scale;
          const d = fp.depth * vp.scale;
          const item = states.get(rack.id);
          const color = item ? STATE_COLORS[item.state] : "#1d4ed8";
          return (
            <g
              key={rack.id}
              data-asset-id={rack.id}
              data-asset-kind="rack"
              data-testid={`rack-${rack.id}`}
              data-width-px={w.toFixed(2)}
              data-depth-px={d.toFixed(2)}
              transform={`rotate(${rack.rotation_deg ?? 0} ${px + w / 2} ${py + d / 2})`}
            >
              <Link to={`/racks/${rack.id}`} aria-label={`Rack ${rack.name}, ${fp.width} by ${fp.depth} millimetres${item ? `, ${describeOverlay(overlay as OverlayKind, item)}` : ""}`}>
                <rect x={px} y={py} width={w} height={d} fill={color} fillOpacity={item ? 0.55 : 0.8} stroke={item ? color : "#93c5fd"} strokeWidth={item ? 2 : 1} />
                {/* front edge (toward +Y at 0 degrees) so orientation is readable */}
                <line x1={px} x2={px + w} y1={py + d} y2={py + d} stroke="#f8fafc" strokeWidth={2} />
                {w > 26 && d > 12 && (
                  <text x={px + w / 2} y={py + d / 2 + 3} fontSize={Math.min(10, w / 5)} fill="#f8fafc" textAnchor="middle" pointerEvents="none">
                    {rack.name}
                  </text>
                )}
                {item && (
                  <g pointerEvents="none">
                    <circle cx={px + w - 2} cy={py + 2} r={6} fill="#020617" stroke={color} strokeWidth={1.5} />
                    <text x={px + w - 2} y={py + 5} fontSize={8} fill={color} textAnchor="middle" fontWeight={700}>
                      {STATE_SYMBOL[item.state]}
                    </text>
                  </g>
                )}
                <title>{`${rack.name} (${rack.asset_tag}) · ${formatLength(fp.width)} x ${formatLength(fp.depth)} · ${rack.rotation_deg ?? 0}°${item ? ` · ${describeOverlay(overlay as OverlayKind, item)}` : ""}`}</title>
              </Link>
            </g>
          );
        })}

        {view.equipment.map((eq) => {
          if (eq.position_state !== "placed" || eq.x_mm == null || eq.y_mm == null) return null;
          const [px, py] = mmToPx(vp, eq.x_mm, eq.y_mm);
          const item = states.get(eq.id);
          const color = item ? STATE_COLORS[item.state] : "#0d9488";
          if (eq.width_mm && eq.depth_mm) {
            const w = eq.width_mm * vp.scale;
            const d = eq.depth_mm * vp.scale;
            return (
              <g key={eq.id} data-asset-id={eq.id} data-asset-kind="equipment" transform={`rotate(${eq.rotation_deg ?? 0} ${px + w / 2} ${py + d / 2})`}>
                <Link to={`/equipment/${eq.id}`} aria-label={`Equipment ${eq.hostname ?? eq.asset_tag}`}>
                  <rect x={px} y={py} width={w} height={d} fill={color} fillOpacity={0.55} stroke={color} strokeWidth={1.5} />
                  <title>{`${eq.hostname ?? eq.asset_tag} · ${formatLength(eq.width_mm)} x ${formatLength(eq.depth_mm)}${item ? ` · ${describeOverlay(overlay as OverlayKind, item)}` : ""}`}</title>
                </Link>
              </g>
            );
          }
          // position known, footprint not: a fixed-size pin that is visibly not a to-scale shape
          return (
            <g key={eq.id} data-asset-id={eq.id} data-asset-kind="equipment" data-footprint="unknown">
              <Link to={`/equipment/${eq.id}`} aria-label={`Equipment ${eq.hostname ?? eq.asset_tag}, footprint unknown`}>
                <path d={`M ${px} ${py - 8} L ${px + 8} ${py} L ${px} ${py + 8} L ${px - 8} ${py} Z`} fill="none" stroke="#fbbf24" strokeWidth={1.5} strokeDasharray="2 2" />
                <text x={px} y={py + 3} fontSize={9} fill="#fbbf24" textAnchor="middle">
                  ?
                </text>
                <title>{`${eq.hostname ?? eq.asset_tag}: position known, catalog dimensions unknown (not drawn to scale)`}</title>
              </Link>
            </g>
          );
        })}
        {renderLayers?.({ vp, bounds })}
      </svg>

      {extraLegend}
      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 bg-blue-700" /> Rack at catalog width × depth (white edge = front)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 bg-teal-600" /> Floor-standing equipment at catalog footprint
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 border border-dashed border-slate-500" /> Drawn shape from an accepted import
        </span>
        {overlay !== "none" &&
          (Object.keys(STATE_COLORS) as (keyof typeof STATE_COLORS)[]).map((state) => (
            <span key={state} className="flex items-center gap-1.5" data-testid={`legend-${state}`}>
              <span className="inline-block h-3 w-3" style={{ background: STATE_COLORS[state] }} /> {STATE_SYMBOL[state]} {STATE_LABEL[state]}
            </span>
          ))}
      </div>

      {(unpositionedRacks.length > 0 || unpositionedEquipment.length > 0) && (
        <div className="mt-3 rounded-sm border border-yellow-900 bg-yellow-950/30 p-3 text-xs text-yellow-300" role="status" data-testid="unpositioned-list">
          <p className="mb-1 font-medium">Not drawn: position or dimensions are not recorded</p>
          <ul className="list-disc pl-5">
            {unpositionedRacks.map((r) => (
              <li key={r.id}>
                <Link className="underline" to={`/racks/${r.id}`}>
                  Rack {r.name}
                </Link>{" "}
                — {r.x_mm == null || r.y_mm == null ? "no floor position recorded" : "catalog dimensions missing"}
              </li>
            ))}
            {unpositionedEquipment.map((e) => (
              <li key={e.id}>
                <Link className="underline" to={`/equipment/${e.id}`}>
                  {e.hostname ?? e.asset_tag}
                </Link>{" "}
                — no linked position
              </li>
            ))}
          </ul>
        </div>
      )}

      <details className="mt-3 text-xs text-slate-400">
        <summary className="cursor-pointer">Text alternative: placed racks and floor equipment</summary>
        <table className="mt-2 w-full text-left" data-testid="spatial-table">
          <thead>
            <tr className="text-slate-500">
              <th scope="col">Asset</th>
              <th scope="col">X, Y (mm)</th>
              <th scope="col">Size W × D (mm)</th>
              <th scope="col">Rotation</th>
              <th scope="col">{overlay === "none" ? "Overlay" : `${overlay} state`}</th>
            </tr>
          </thead>
          <tbody>
            {view.racks.map((r) => {
              const fp = rackFootprint(r);
              const item = states.get(r.id);
              return (
                <tr key={r.id}>
                  <th scope="row">{r.name}</th>
                  <td>{r.x_mm != null && r.y_mm != null ? `${r.x_mm}, ${r.y_mm}` : "not recorded"}</td>
                  <td>{fp ? `${fp.width} × ${fp.depth}` : "not recorded"}</td>
                  <td>{r.rotation_deg ?? 0}°</td>
                  <td>{overlay === "none" ? "—" : item ? `${STATE_LABEL[item.state]}: ${item.reason}` : "no data"}</td>
                </tr>
              );
            })}
            {view.equipment.map((e) => (
              <tr key={e.id}>
                <th scope="row">{e.hostname ?? e.asset_tag}</th>
                <td>{e.x_mm != null && e.y_mm != null ? `${e.x_mm}, ${e.y_mm}` : "not recorded"}</td>
                <td>{e.width_mm && e.depth_mm ? `${e.width_mm} × ${e.depth_mm}` : "not recorded"}</td>
                <td>{e.rotation_deg ?? 0}°</td>
                <td>{overlay === "none" ? "—" : states.get(e.id) ? `${STATE_LABEL[states.get(e.id)!.state]}: ${states.get(e.id)!.reason}` : "no data"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
