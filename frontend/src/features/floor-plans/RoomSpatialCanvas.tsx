import { Link } from "react-router-dom";

import { RoomSpatialView } from "@/types";

const CANVAS_WIDTH_PX = 640;
const DEFAULT_ROOM_MM = 10_000; // fallback when the room has no recorded dimensions yet

/** Pure rendering of authoritative placement data (racks/equipment from RackPlacement/
 * EquipmentPlacement) plus the active FloorPlan's SpatialObjects — never an editable
 * canvas of its own; there is no client-side authoritative state here (ARCHITECTURE_
 * REVIEW.md §40/§12: the database is the only source of truth for position). */
export function RoomSpatialCanvas({ view }: { view: RoomSpatialView }) {
  const roomWidthMm = view.room_width_mm ?? DEFAULT_ROOM_MM;
  const roomHeightMm = view.room_height_mm ?? DEFAULT_ROOM_MM;
  const scale = CANVAS_WIDTH_PX / roomWidthMm;
  const canvasHeight = roomHeightMm * scale;

  const RACK_FOOTPRINT_MM = 600;

  return (
    <div>
      <svg
        width={CANVAS_WIDTH_PX}
        height={canvasHeight}
        className="rounded border border-slate-800 bg-slate-950"
      >
        <rect x={0} y={0} width={CANVAS_WIDTH_PX} height={canvasHeight} fill="none" stroke="#1e293b" />

        {view.objects
          .filter((o) => o.object_type !== "rack")
          .map((obj) => (
            <g key={obj.id}>
              <rect
                x={obj.x_mm * scale}
                y={obj.y_mm * scale}
                width={(obj.width_mm ?? 200) * scale}
                height={(obj.height_mm ?? 200) * scale}
                fill="none"
                stroke={obj.source === "imported" ? "#475569" : "#64748b"}
                strokeDasharray={obj.source === "imported" ? "4 2" : undefined}
              />
              {obj.label && (
                <text x={obj.x_mm * scale + 2} y={obj.y_mm * scale + 10} fontSize={8} fill="#64748b">
                  {obj.label}
                </text>
              )}
            </g>
          ))}

        {view.racks.map((rack) => {
          if (rack.x_mm == null || rack.y_mm == null) return null;
          const w = RACK_FOOTPRINT_MM * scale;
          const h = RACK_FOOTPRINT_MM * scale;
          return (
            <g key={rack.id} transform={`rotate(${rack.rotation_deg ?? 0} ${rack.x_mm * scale + w / 2} ${rack.y_mm * scale + h / 2})`}>
              <Link to={`/racks/${rack.id}`}>
                <rect x={rack.x_mm * scale} y={rack.y_mm * scale} width={w} height={h} fill="#1d4ed8" opacity={0.85} rx={2} />
                <text x={rack.x_mm * scale + 4} y={rack.y_mm * scale + h / 2 + 4} fontSize={9} fill="#f1f5f9">
                  {rack.name}
                </text>
              </Link>
            </g>
          );
        })}

        {view.equipment.map((eq, i) => (
          <g key={eq.id}>
            <circle cx={20 + i * 24} cy={canvasHeight - 16} r={7} fill="#0d9488" />
            <title>{eq.hostname ?? eq.asset_tag}</title>
          </g>
        ))}
      </svg>
      <div className="mt-2 flex gap-6 text-xs text-slate-500">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-sm bg-blue-700" /> Rack (click to open)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-full bg-teal-600" /> Floor/wall/ceiling-mounted equipment
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 border border-dashed border-slate-500" /> Imported (unpromoted) geometry
        </span>
      </div>
      {!view.room_width_mm && (
        <p className="mt-2 text-xs italic text-slate-600">
          This room has no recorded width/height yet — showing an arbitrary {DEFAULT_ROOM_MM / 1000}m×
          {DEFAULT_ROOM_MM / 1000}m canvas for scale.
        </p>
      )}
    </div>
  );
}
