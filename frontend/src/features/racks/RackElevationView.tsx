import { RackElevation } from "@/types";

const U_HEIGHT_PX = 22;
const COL_WIDTH_PX = 160;
const LABEL_COL_WIDTH_PX = 36;

const SIDE_COLOR: Record<string, string> = {
  front: "#2563eb",
  rear: "#7c3aed",
  both: "#0d9488",
};

/** A pure projection of RackElevation data — never an independently editable model.
 * Renders U1 at the bottom (the conventional physical rack numbering direction). */
export function RackElevationView({ elevation }: { elevation: RackElevation }) {
  const totalHeight = elevation.height_u * U_HEIGHT_PX;
  const svgWidth = LABEL_COL_WIDTH_PX + COL_WIDTH_PX * 2 + 8;

  const uToY = (u: number) => totalHeight - (u - 1) * U_HEIGHT_PX - U_HEIGHT_PX;

  return (
    <div>
      <div className="mb-2 flex gap-6 text-xs text-slate-400">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-sm" style={{ background: SIDE_COLOR.front }} /> Front
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-sm" style={{ background: SIDE_COLOR.rear }} /> Rear
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-sm" style={{ background: SIDE_COLOR.both }} /> Both
        </span>
      </div>
      <svg width={svgWidth} height={totalHeight + 4} className="rounded border border-slate-800 bg-slate-950">
        {Array.from({ length: elevation.height_u }, (_, i) => i + 1).map((u) => (
          <g key={u}>
            <text x={2} y={uToY(u) + U_HEIGHT_PX / 2 + 4} fontSize={9} fill="#64748b">
              {u}
            </text>
            <line
              x1={LABEL_COL_WIDTH_PX}
              y1={uToY(u)}
              x2={svgWidth}
              y2={uToY(u)}
              stroke="#1e293b"
              strokeWidth={1}
            />
          </g>
        ))}
        <rect x={LABEL_COL_WIDTH_PX} y={0} width={COL_WIDTH_PX} height={totalHeight} fill="none" stroke="#334155" />
        <rect
          x={LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 4}
          y={0}
          width={COL_WIDTH_PX}
          height={totalHeight}
          fill="none"
          stroke="#334155"
        />
        {elevation.slots.map((slot) => {
          const y = uToY(slot.u_end - 1);
          const height = (slot.u_end - slot.u_start) * U_HEIGHT_PX;
          const color = SIDE_COLOR[slot.side] ?? "#475569";
          const boxes =
            slot.side === "both"
              ? [
                  { x: LABEL_COL_WIDTH_PX, width: COL_WIDTH_PX },
                  { x: LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 4, width: COL_WIDTH_PX },
                ]
              : [{ x: LABEL_COL_WIDTH_PX + (slot.side === "rear" ? COL_WIDTH_PX + 4 : 0), width: COL_WIDTH_PX }];
          return boxes.map((box, i) => (
            <a key={`${slot.equipment_id}-${i}`} href={`/equipment/${slot.equipment_id}`} aria-label={`Open ${slot.hostname ?? slot.asset_tag}`}>
              <rect x={box.x + 1} y={y + 1} width={box.width - 2} height={height - 2} fill={color} opacity={0.85} rx={2} />
              <text x={box.x + 6} y={y + height / 2 + 4} fontSize={10} fill="#f1f5f9">
                {(slot.hostname ?? slot.asset_tag).slice(0, 22)}
              </text>
            </a>
          ));
        })}
      </svg>
      {elevation.slots.length === 0 && <p className="mt-2 text-sm text-slate-500">No equipment mounted in this rack.</p>}
    </div>
  );
}
