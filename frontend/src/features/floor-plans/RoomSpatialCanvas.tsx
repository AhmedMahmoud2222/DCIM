import { PointerEvent, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { RoomSpatialView } from "@/types";

const DEFAULT_ROOM_MM = 10_000;
const RACK_FOOTPRINT_MM = 600;

/** A read-only view of authoritative room placement and floor-plan data. Controls only
 * the viewport; no layout state is created in the client. */
export function RoomSpatialCanvas({ view }: { view: RoomSpatialView }) {
  const width = view.room_width_mm ?? DEFAULT_ROOM_MM;
  const height = view.room_height_mm ?? DEFAULT_ROOM_MM;
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null);
  const viewBox = useMemo(() => {
    const visibleWidth = width / zoom;
    const visibleHeight = height / zoom;
    const x = Math.max(0, Math.min(width - visibleWidth, offset.x));
    const y = Math.max(0, Math.min(height - visibleHeight, offset.y));
    return `${x} ${y} ${visibleWidth} ${visibleHeight}`;
  }, [height, offset, width, zoom]);
  const reset = () => { setZoom(1); setOffset({ x: 0, y: 0 }); };
  const startDrag = (event: PointerEvent<SVGSVGElement>) => { setDrag({ x: event.clientX, y: event.clientY }); event.currentTarget.setPointerCapture(event.pointerId); };
  const moveDrag = (event: PointerEvent<SVGSVGElement>) => {
    if (!drag) return;
    const scale = width / Math.max(event.currentTarget.clientWidth, 1) / zoom;
    setOffset((previous) => ({ x: previous.x - (event.clientX - drag.x) * scale, y: previous.y - (event.clientY - drag.y) * scale }));
    setDrag({ x: event.clientX, y: event.clientY });
  };

  return <div className="surface overflow-hidden">
    <div className="flex flex-col gap-3 border-b border-slate-800 bg-slate-950/30 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div><p className="text-sm font-medium text-slate-200">Operational layout</p><p className="text-xs text-slate-500">Rack positions are authoritative. Drag the canvas only to inspect the view.</p></div>
      <div className="flex gap-2"><button className="action-secondary !px-2 !py-1 text-xs" onClick={() => setZoom((value) => Math.min(3, +(value + 0.25).toFixed(2)))}>Zoom in</button><button className="action-secondary !px-2 !py-1 text-xs" onClick={() => setZoom((value) => Math.max(0.5, +(value - 0.25).toFixed(2)))}>Zoom out</button><button className="action-secondary !px-2 !py-1 text-xs" onClick={reset}>Fit view</button></div>
    </div>
    <div className="relative bg-[#070c15] p-3">
      <svg viewBox={viewBox} className="h-[min(66vh,680px)] w-full touch-none rounded-lg border border-slate-800" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)} aria-label={`Floor plan for ${view.room_name}`}>
        <defs><pattern id="floor-grid" width="500" height="500" patternUnits="userSpaceOnUse"><path d="M 500 0 L 0 0 0 500" fill="none" stroke="#1e293b" strokeWidth="16" /></pattern></defs>
        <rect x={0} y={0} width={width} height={height} fill="#0b1220" />
        <rect x={0} y={0} width={width} height={height} fill="url(#floor-grid)" />
        <rect x={0} y={0} width={width} height={height} fill="none" stroke="#475569" strokeWidth="25" />
        {view.objects.filter((object) => object.object_type !== "rack").map((object) => <g key={object.id}><rect x={object.x_mm} y={object.y_mm} width={object.width_mm ?? 200} height={object.height_mm ?? 200} fill="none" stroke={object.source === "imported" ? "#64748b" : "#94a3b8"} strokeWidth="18" strokeDasharray={object.source === "imported" ? "60 40" : undefined} /><text x={object.x_mm + 30} y={object.y_mm + 80} fontSize="90" fill="#94a3b8">{object.label ?? ""}</text></g>)}
        {view.racks.map((rack) => {
          if (rack.x_mm == null || rack.y_mm == null) return null;
          return <g key={rack.id} transform={`rotate(${rack.rotation_deg ?? 0} ${rack.x_mm + RACK_FOOTPRINT_MM / 2} ${rack.y_mm + RACK_FOOTPRINT_MM / 2})`}><Link to={`/racks/${rack.id}`} aria-label={`Open rack ${rack.name}`}><rect x={rack.x_mm} y={rack.y_mm} width={RACK_FOOTPRINT_MM} height={RACK_FOOTPRINT_MM} rx="32" fill="#4f46e5" stroke="#a5b4fc" strokeWidth="18" /><text x={rack.x_mm + RACK_FOOTPRINT_MM / 2} y={rack.y_mm + RACK_FOOTPRINT_MM / 2 + 35} textAnchor="middle" fontSize="105" fontWeight="600" fill="#eef2ff">{rack.name}</text></Link></g>;
        })}
      </svg>
      <div className="pointer-events-none absolute bottom-6 left-6 rounded-md border border-slate-700 bg-slate-950/90 px-3 py-2 text-xs text-slate-300"><span className="mr-3 inline-flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-indigo-500" /> Rack</span><span className="inline-flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm border border-slate-400" /> Imported geometry</span></div>
    </div>
    {!view.room_width_mm && <p className="border-t border-slate-800 px-4 py-3 text-xs text-amber-200">Room dimensions are not recorded. The canvas uses a 10 m × 10 m viewing scale only; it does not infer physical dimensions.</p>}
  </div>;
}
