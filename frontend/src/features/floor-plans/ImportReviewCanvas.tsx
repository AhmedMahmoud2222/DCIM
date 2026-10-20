import { type KeyboardEvent, type PointerEvent, useMemo, useRef, useState } from "react";

import type { Calibration, ImportCandidate, SourceGeometry } from "@/types";
import { SceneChrome } from "./SceneChrome";
import {
  type BoundsMm,
  type Point,
  boundsOf,
  fitViewport,
  type DisplayCal,
  isRect,
  provisionalCalibration,
  mmToPx,
  mmToSource,
  pxToMm,
  rectCorners,
  rectSourceToMm,
  snapToGrid,
  sourceToMm,
} from "./spatialMath";

export type PickMode = "none" | "two-point" | "origin";

const TYPE_COLORS: Record<string, string> = {
  rack: "#3b82f6",
  equipment: "#14b8a6",
  room_outline: "#38bdf8",
  wall: "#94a3b8",
  column: "#a78bfa",
  obstacle: "#a78bfa",
  aisle: "#2dd4bf",
  annotation: "#64748b",
};
const MATCH_DASH: Record<string, string | undefined> = { duplicate: "2 3", conflict: "6 3", ambiguous: "4 3" };

interface Props {
  source: SourceGeometry | null;
  candidates: ImportCandidate[];
  calibration: Calibration | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** New centre in *source* coordinates after a drag or keyboard nudge. */
  onMove: (candidate: ImportCandidate, centre: { cx: number; cy: number }) => void;
  pickMode: PickMode;
  picks: Point[];
  onPick: (sourcePoint: Point) => void;
  showGrid: boolean;
  snapMm: number;
  canvasWidthPx?: number;
}

export function ImportReviewCanvas({
  source,
  candidates,
  calibration,
  selectedId,
  onSelect,
  onMove,
  pickMode,
  picks,
  onPick,
  showGrid,
  snapMm,
  canvasWidthPx = 880,
}: Props) {
  const cal: DisplayCal = calibration ?? provisionalCalibration(source);
  const calibrated = calibration != null;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [drag, setDrag] = useState<{ id: string; startX: number; startY: number; dx: number; dy: number } | null>(null);

  const { vp, bounds } = useMemo(() => {
    const pts: Point[] = [];
    for (const e of source?.entities ?? []) {
      if (e.kind === "rect" && e.cx != null && e.cy != null && e.width && e.height) {
        const r = rectSourceToMm(cal, { cx: e.cx, cy: e.cy, width: e.width, height: e.height, rotation_deg: e.rotation_deg });
        pts.push(...rectCorners(r.x_mm, r.y_mm, r.width_mm, r.height_mm, r.rotation_deg));
      } else if (e.points) pts.push(...e.points.map((p) => sourceToMm(cal, p[0], p[1])));
      else if (e.cx != null && e.cy != null) pts.push(sourceToMm(cal, e.cx, e.cy));
    }
    for (const c of candidates) {
      const g = c.effective_geometry;
      if (isRect(g)) {
        const r = rectSourceToMm(cal, { cx: g.cx, cy: g.cy, width: g.width, height: g.height, rotation_deg: g.rotation_deg });
        pts.push(...rectCorners(r.x_mm, r.y_mm, r.width_mm, r.height_mm, r.rotation_deg));
      }
    }
    const b: BoundsMm = pts.length ? boundsOf(pts) : { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    return { vp: fitViewport(b, canvasWidthPx, 44, 640), bounds: b };
  }, [source, candidates, cal, canvasWidthPx]);

  const toDisplay = (clientX: number, clientY: number): Point => {
    const box = svgRef.current!.getBoundingClientRect();
    const ratio = vp.widthPx / box.width;
    return pxToMm(vp, (clientX - box.left) * ratio, (clientY - box.top) * ratio);
  };

  const snap = (v: number) => (calibrated && snapMm > 0 ? snapToGrid(v, snapMm) : v);

  const finishDrag = (event: PointerEvent<SVGGElement>, candidate: ImportCandidate) => {
    if (!drag || drag.id !== candidate.id) return;
    const g = candidate.effective_geometry;
    if (isRect(g) && (Math.abs(drag.dx) > 0.5 || Math.abs(drag.dy) > 0.5)) {
      const [mx, my] = sourceToMm(cal, g.cx, g.cy);
      const target: Point = [snap(mx + drag.dx / vp.scale), snap(my + drag.dy / vp.scale)];
      const [cx, cy] = mmToSource(cal, target[0], target[1]);
      onMove(candidate, { cx, cy });
    }
    (event.currentTarget as unknown as Element).releasePointerCapture?.(event.pointerId);
    setDrag(null);
  };

  const nudge = (event: KeyboardEvent<SVGGElement>, candidate: ImportCandidate) => {
    const g = candidate.effective_geometry;
    if (!isRect(g)) return;
    const step = (snapMm > 0 && calibrated ? snapMm : calibrated ? 10 : 1) * (event.shiftKey ? 10 : 1);
    const delta: Record<string, Point> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    const d = delta[event.key];
    if (!d) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelect(candidate.id);
      }
      return;
    }
    event.preventDefault();
    const [mx, my] = sourceToMm(cal, g.cx, g.cy);
    const [cx, cy] = mmToSource(cal, snap(mx + d[0]), snap(my + d[1]));
    onMove(candidate, { cx, cy });
  };

  const entityShapes = (source?.entities ?? []).slice(0, 4000).map((e, i) => {
    if (e.kind === "text") return null;
    if (e.kind === "rect" && e.cx != null && e.cy != null && e.width && e.height) {
      const r = rectSourceToMm(cal, { cx: e.cx, cy: e.cy, width: e.width, height: e.height, rotation_deg: e.rotation_deg });
      const pts = rectCorners(r.x_mm, r.y_mm, r.width_mm, r.height_mm, r.rotation_deg).map(([x, y]) => mmToPx(vp, x, y).join(",")).join(" ");
      return <polygon key={`e${i}`} points={pts} fill="none" stroke="#475569" strokeWidth={1} />;
    }
    if (e.kind === "circle" && e.cx != null && e.cy != null) {
      const [x, y] = mmToPx(vp, ...sourceToMm(cal, e.cx, e.cy));
      return <circle key={`e${i}`} cx={x} cy={y} r={Math.max(1, (e.radius ?? 0) * cal.mm_per_unit * vp.scale)} fill="none" stroke="#475569" />;
    }
    if (e.points && e.points.length >= 2) {
      const pts = e.points.map((p) => mmToPx(vp, ...sourceToMm(cal, p[0], p[1])).join(",")).join(" ");
      return e.kind === "polygon" ? (
        <polygon key={`e${i}`} points={pts} fill="none" stroke="#475569" strokeWidth={1} />
      ) : (
        <polyline key={`e${i}`} points={pts} fill="none" stroke="#475569" strokeWidth={1} />
      );
    }
    return null;
  });

  return (
    <div data-testid="import-review-canvas" data-calibrated={calibrated ? "true" : "false"}>
      <p className="mb-1 text-xs text-slate-500" role="status">
        {calibrated
          ? "Calibrated view: distances are real-world millimetres."
          : `Uncalibrated view in the drawing's own units (${source?.source_units ?? "unknown"}). Calibrate before accepting anything.`}
        {pickMode === "two-point" && " Click the two reference points on the drawing."}
        {pickMode === "origin" && " Click the room corner that should become the origin."}
      </p>
      <svg
        ref={svgRef}
        width={vp.widthPx}
        height={vp.heightPx}
        viewBox={`0 0 ${vp.widthPx} ${vp.heightPx}`}
        role="group"
        aria-label="Import review canvas"
        className={`max-w-full rounded-sm border border-slate-800 bg-slate-950 ${pickMode !== "none" ? "cursor-crosshair" : ""}`}
        onPointerDown={(event) => {
          if (pickMode === "none") return;
          const [dx, dy] = toDisplay(event.clientX, event.clientY);
          onPick(mmToSource(cal, dx, dy));
        }}
      >
        <SceneChrome vp={vp} bounds={bounds} showGrid={showGrid} unitLabel={calibrated ? "mm" : source?.source_units ?? "units"} />
        {entityShapes}

        {candidates.map((c) => {
          const g = c.effective_geometry;
          const type = c.effective_object_type ?? "";
          const color = TYPE_COLORS[type] ?? "#94a3b8";
          const selected = c.id === selectedId;
          const dash = MATCH_DASH[c.match_status];
          const label = c.effective_label ?? "";
          const common = { fill: color, fillOpacity: selected ? 0.55 : 0.28, stroke: selected ? "#f8fafc" : color, strokeWidth: selected ? 2.5 : 1.25, strokeDasharray: dash };
          if (isRect(g)) {
            const r = rectSourceToMm(cal, { cx: g.cx, cy: g.cy, width: g.width, height: g.height, rotation_deg: g.rotation_deg });
            const [px, py] = mmToPx(vp, r.x_mm, r.y_mm);
            const w = r.width_mm * vp.scale;
            const h = r.height_mm * vp.scale;
            const moving = drag?.id === c.id ? drag : null;
            return (
              <g
                key={c.id}
                role="button"
                tabIndex={0}
                aria-pressed={selected}
                aria-label={`Candidate ${label || g.shape_type}${type ? `, ${type}` : ""}, ${c.match_status}. Arrow keys move it, shift for larger steps.`}
                data-candidate-id={c.id}
                data-testid={`candidate-${c.id}`}
                transform={`translate(${moving?.dx ?? 0} ${moving?.dy ?? 0}) rotate(${r.rotation_deg} ${px + w / 2} ${py + h / 2})`}
                onKeyDown={(e) => nudge(e, c)}
                onFocus={() => onSelect(c.id)}
                onPointerDown={(e) => {
                  if (pickMode !== "none") return;
                  e.stopPropagation();
                  onSelect(c.id);
                  (e.currentTarget as unknown as Element).setPointerCapture?.(e.pointerId);
                  setDrag({ id: c.id, startX: e.clientX, startY: e.clientY, dx: 0, dy: 0 });
                }}
                onPointerMove={(e) => {
                  if (!drag || drag.id !== c.id) return;
                  const box = svgRef.current!.getBoundingClientRect();
                  const ratio = vp.widthPx / box.width;
                  setDrag({ ...drag, dx: (e.clientX - drag.startX) * ratio, dy: (e.clientY - drag.startY) * ratio });
                }}
                onPointerUp={(e) => finishDrag(e, c)}
                onPointerCancel={() => setDrag(null)}
                className="cursor-move outline-none focus-visible:[&>rect]:stroke-white"
              >
                <rect x={px} y={py} width={w} height={h} {...common} />
                {w > 20 && h > 10 && (
                  <text x={px + w / 2} y={py + h / 2 + 3} fontSize={Math.min(10, Math.max(6, w / 6))} textAnchor="middle" fill="#f8fafc" pointerEvents="none">
                    {label}
                  </text>
                )}
                <title>{`${label || g.shape_type} · ${type || "unclassified"} · match: ${c.match_status}`}</title>
              </g>
            );
          }
          if ((g.shape_type === "polygon" || g.shape_type === "polyline" || g.shape_type === "line") && g.points) {
            const pts = g.points.map((p) => mmToPx(vp, ...sourceToMm(cal, p[0], p[1])).join(",")).join(" ");
            const Shape = g.shape_type === "polygon" ? "polygon" : "polyline";
            return (
              <g key={c.id} role="button" tabIndex={0} aria-label={`Candidate ${g.shape_type} ${type}`} data-candidate-id={c.id} onClick={() => onSelect(c.id)} onFocus={() => onSelect(c.id)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(c.id)}>
                <Shape points={pts} {...common} fill={g.shape_type === "polygon" ? color : "none"} />
              </g>
            );
          }
          if (g.cx != null && g.cy != null) {
            const [x, y] = mmToPx(vp, ...sourceToMm(cal, g.cx, g.cy));
            return (
              <g key={c.id} role="button" tabIndex={0} aria-label={`Candidate ${g.shape_type} ${label}`} data-candidate-id={c.id} onClick={() => onSelect(c.id)} onFocus={() => onSelect(c.id)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(c.id)}>
                {g.shape_type === "circle" ? (
                  <circle cx={x} cy={y} r={Math.max(2, (g.radius ?? 0) * cal.mm_per_unit * vp.scale)} {...common} />
                ) : (
                  <text x={x} y={y} fontSize={9} fill={selected ? "#f8fafc" : "#94a3b8"}>
                    {label || "text"}
                  </text>
                )}
              </g>
            );
          }
          return null;
        })}

        {picks.map((p, i) => {
          const [x, y] = mmToPx(vp, ...sourceToMm(cal, p[0], p[1]));
          return (
            <g key={`pick${i}`} data-testid={`pick-${i}`} pointerEvents="none">
              <circle cx={x} cy={y} r={6} fill="none" stroke="#facc15" strokeWidth={2} />
              <line x1={x - 9} x2={x + 9} y1={y} y2={y} stroke="#facc15" />
              <line x1={x} x2={x} y1={y - 9} y2={y + 9} stroke="#facc15" />
              <text x={x + 9} y={y - 8} fontSize={10} fill="#facc15">
                P{i + 1}
              </text>
            </g>
          );
        })}
        {picks.length === 2 && (
          <line
            {...(() => {
              const [x1, y1] = mmToPx(vp, ...sourceToMm(cal, picks[0][0], picks[0][1]));
              const [x2, y2] = mmToPx(vp, ...sourceToMm(cal, picks[1][0], picks[1][1]));
              return { x1, y1, x2, y2 };
            })()}
            stroke="#facc15"
            strokeDasharray="4 3"
            pointerEvents="none"
          />
        )}
      </svg>
    </div>
  );
}
