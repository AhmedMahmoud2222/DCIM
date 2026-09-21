import { Link } from "react-router-dom";

import { ElevationSlot, RackElevation } from "@/types";

const U_HEIGHT_PX = 17;
const COL_WIDTH_PX = 320;
const LABEL_COL_WIDTH_PX = 44;

const SIDE_COLOR: Record<string, string> = {
  front: "#2563eb",
  rear: "#7c3aed",
  both: "#0d9488",
};

/** A read-only projection of the authoritative rack placement model. U1 remains at
 * the bottom; selection is visual only and never creates a client-side rack state. */
export function RackElevationView({
  elevation,
  selectedEquipmentId,
  onInspect,
}: {
  elevation: RackElevation;
  selectedEquipmentId?: string | null;
  onInspect?: (slot: ElevationSlot) => void;
}) {
  const totalHeight = elevation.height_u * U_HEIGHT_PX;
  const svgWidth = LABEL_COL_WIDTH_PX + COL_WIDTH_PX * 2 + 16;
  const uToY = (u: number) => totalHeight - (u - 1) * U_HEIGHT_PX - U_HEIGHT_PX;

  return (
    <div className="rack-elevation-canvas">
      <div className="rack-elevation-legend" aria-label="Rack elevation legend">
        <span>U1 at base</span>
        <span><i style={{ background: SIDE_COLOR.front }} />Front</span>
        <span><i style={{ background: SIDE_COLOR.rear }} />Rear</span>
        <span><i style={{ background: SIDE_COLOR.both }} />Both faces</span>
      </div>
      <div className="rack-elevation-scroll">
        <svg width={svgWidth} height={totalHeight + 48} className="rack-elevation-svg" role="img" aria-label={`${elevation.rack_name} rack elevation`}>
          <text x={LABEL_COL_WIDTH_PX + COL_WIDTH_PX / 2} y={28} textAnchor="middle" className="rack-elevation-face-label">FRONT</text>
          <text x={LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 12 + COL_WIDTH_PX / 2} y={28} textAnchor="middle" className="rack-elevation-face-label">REAR</text>
          <g transform="translate(0 44)">
            {Array.from({ length: elevation.height_u }, (_, index) => index + 1).map((u) => (
              <g key={u}>
                <text x={4} y={uToY(u) + U_HEIGHT_PX / 2 + 4} className="rack-elevation-u-label">{u}</text>
                <line x1={LABEL_COL_WIDTH_PX} y1={uToY(u)} x2={svgWidth} y2={uToY(u)} className="rack-elevation-grid" />
              </g>
            ))}
            <rect x={LABEL_COL_WIDTH_PX} y={0} width={COL_WIDTH_PX} height={totalHeight} className="rack-elevation-frame" />
            <rect x={LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 12} y={0} width={COL_WIDTH_PX} height={totalHeight} className="rack-elevation-frame" />
            {elevation.slots.flatMap((slot) => {
              const y = uToY(slot.u_end - 1);
              const height = (slot.u_end - slot.u_start) * U_HEIGHT_PX;
              const color = SIDE_COLOR[slot.side] ?? "#475569";
              const faces = slot.side === "both"
                ? [LABEL_COL_WIDTH_PX, LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 12]
                : [LABEL_COL_WIDTH_PX + (slot.side === "rear" ? COL_WIDTH_PX + 12 : 0)];
              const selected = selectedEquipmentId === slot.equipment_id;
              return faces.map((x, index) => (
                <Link
                  key={`${slot.equipment_id}-${index}`}
                  to={`/equipment/${slot.equipment_id}`}
                  onMouseEnter={() => onInspect?.(slot)}
                  onFocus={() => onInspect?.(slot)}
                  aria-label={`Open ${slot.hostname ?? slot.asset_tag}, U${slot.u_start} to U${slot.u_end}`}
                >
                  <rect x={x + 2} y={y + 2} width={COL_WIDTH_PX - 4} height={Math.max(height - 4, 4)} fill={color} opacity={selected ? 1 : 0.84} rx={4} className="rack-elevation-slot" stroke={selected ? "#f8fafc" : "transparent"} strokeWidth={selected ? 2 : 0} />
                  {height >= 16 && <text x={x + 12} y={y + height / 2 + 4} className="rack-elevation-slot-label">{slot.hostname ?? slot.asset_tag}</text>}
                </Link>
              ));
            })}
          </g>
        </svg>
      </div>
      {elevation.slots.length === 0 && <p className="mt-3 text-sm text-slate-500">No equipment is mounted in this rack.</p>}
    </div>
  );
}
