import { useQuery } from "@tanstack/react-query";
import { Fragment, useMemo, useState } from "react";

import { ImpactAnalysisModal, ImpactTarget } from "@/features/impact/ImpactAnalysisModal";
import { FaceplateOverlay, SelectedMarker, STATUS_COLORS } from "@/features/racks/FaceplateOverlay";
import { getLatestPortStatusForRack } from "@/features/telemetry/api";
import { ElevationSlot, ImpactSimulationResult, LatestPortStatus, RackElevation } from "@/types";

const U_HEIGHT_PX = 22;
const COL_WIDTH_PX = 160;
const LABEL_COL_WIDTH_PX = 36;
// Phase 10C: how often the rack-wide telemetry cache is repolled for the faceplate
// overlays — a plain refetch on this interval (react-query only re-renders the markers
// whose data actually changed) rather than a WebSocket/SSE channel, matching every other
// live-ish view in this codebase (TelemetryTrend, alarm history) and needing no new
// transport/infra for a control-plane-cadence status feed.
const TELEMETRY_POLL_MS = 5_000;

const SIDE_COLOR: Record<string, string> = {
  front: "#2563eb",
  rear: "#7c3aed",
  both: "#0d9488",
};

const IMPACT_HIGHLIGHT_COLOR: Record<"direct" | "indirect", string> = {
  direct: "#ef4444",
  indirect: "#eab308",
};

interface Box {
  x: number;
  width: number;
  side: "front" | "rear";
}

function impactHighlightsFrom(result: ImpactSimulationResult | null): Record<string, "direct" | "indirect"> {
  if (!result) return {};
  const highlights: Record<string, "direct" | "indirect"> = {};
  for (const item of result.indirectly_impacted) highlights[item.equipment_id] = "indirect";
  for (const item of result.directly_impacted) highlights[item.equipment_id] = "direct"; // direct wins if both
  return highlights;
}

/** A pure projection of RackElevation data — never an independently editable model.
 * Renders U1 at the bottom (the conventional physical rack numbering direction).
 *
 * Phase 10B: when a slot's equipment was instantiated from a published catalog revision
 * (slot.catalog_model_revision_id is set), a <foreignObject> layers the real front/rear
 * faceplate image + connectivity-colored port/PSU markers (FaceplateOverlay) on top of
 * the plain colored box every slot already draws — that box stays as the fallback
 * background, visible through the overlay wherever no graphic exists for that side, so
 * equipment created via the pre-Phase-10B legacy path renders exactly as it always has.
 *
 * Phase 10C: one bulk, polled `GET /telemetry/port-status/latest?rack_id=` query feeds
 * live link/power/thermal status into every marker in the rack (rather than each
 * FaceplateOverlay instance polling its own equipment), and a "Simulate Failure" action
 * on any marker opens ImpactAnalysisModal, which highlights the returned blast radius's
 * slots directly on this elevation (red = directly impacted, amber = indirectly). */
export function RackElevationView({ elevation }: { elevation: RackElevation }) {
  const [selection, setSelection] = useState<{ slot: ElevationSlot; selected: SelectedMarker } | null>(null);
  const [impactTarget, setImpactTarget] = useState<ImpactTarget | null>(null);
  const [impactResult, setImpactResult] = useState<ImpactSimulationResult | null>(null);

  const telemetryQuery = useQuery({
    queryKey: ["telemetry", "port-status", "rack", elevation.rack_id],
    queryFn: () => getLatestPortStatusForRack(elevation.rack_id),
    enabled: elevation.slots.length > 0,
    refetchInterval: TELEMETRY_POLL_MS,
  });

  const { portStatusByPortId, portStatusByInletId } = useMemo(() => {
    const byPort: Record<string, LatestPortStatus> = {};
    const byInlet: Record<string, LatestPortStatus> = {};
    for (const item of telemetryQuery.data ?? []) {
      if (item.equipment_port_id) byPort[item.equipment_port_id] = item;
      if (item.equipment_power_inlet_id) byInlet[item.equipment_power_inlet_id] = item;
    }
    return { portStatusByPortId: byPort, portStatusByInletId: byInlet };
  }, [telemetryQuery.data]);

  const impactHighlights = useMemo(() => impactHighlightsFrom(impactResult), [impactResult]);

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
        <span className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS_COLORS.connected }} /> Connected
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS_COLORS.unassigned }} /> Unassigned
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS_COLORS.faulted }} /> Faulted
          </span>
        </span>
      </div>
      <svg aria-label={`Rack elevation, ${elevation.height_u} units; equipment links and faceplate controls follow`} width={svgWidth} height={totalHeight + 4} className="rounded border border-slate-800 bg-slate-950">
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
          const highlight = impactHighlights[slot.equipment_id];
          const boxes: Box[] =
            slot.side === "both"
              ? [
                  { x: LABEL_COL_WIDTH_PX, width: COL_WIDTH_PX, side: "front" },
                  { x: LABEL_COL_WIDTH_PX + COL_WIDTH_PX + 4, width: COL_WIDTH_PX, side: "rear" },
                ]
              : [
                  {
                    x: LABEL_COL_WIDTH_PX + (slot.side === "rear" ? COL_WIDTH_PX + 4 : 0),
                    width: COL_WIDTH_PX,
                    side: slot.side === "rear" ? "rear" : "front",
                  },
                ];
          return boxes.map((box, i) => (
            <g key={`${slot.equipment_id}-${i}`}>
              <a href={`/equipment/${slot.equipment_id}`} aria-label={`Open ${slot.hostname ?? slot.asset_tag}`}>
                <rect x={box.x + 1} y={y + 1} width={box.width - 2} height={height - 2} fill={color} opacity={0.85} rx={2} />
                {!slot.catalog_model_revision_id && (
                  <text x={box.x + 6} y={y + height / 2 + 4} fontSize={10} fill="#f1f5f9">
                    {(slot.hostname ?? slot.asset_tag).slice(0, 22)}
                  </text>
                )}
              </a>
              {slot.catalog_model_revision_id && (
                <foreignObject x={box.x + 1} y={y + 1} width={box.width - 2} height={height - 2}>
                  <FaceplateOverlay
                    catalogModelRevisionId={slot.catalog_model_revision_id}
                    equipmentId={slot.equipment_id}
                    side={box.side}
                    onSelect={(selected) => setSelection({ slot, selected })}
                    portStatusByPortId={portStatusByPortId}
                    portStatusByInletId={portStatusByInletId}
                  />
                </foreignObject>
              )}
              {highlight && (
                <rect
                  data-testid="impact-highlight"
                  data-impact-severity={highlight}
                  x={box.x}
                  y={y}
                  width={box.width}
                  height={height}
                  fill="none"
                  stroke={IMPACT_HIGHLIGHT_COLOR[highlight]}
                  strokeWidth={2.5}
                  strokeDasharray={highlight === "indirect" ? "4 3" : undefined}
                  rx={2}
                  pointerEvents="none"
                />
              )}
            </g>
          ));
        })}
      </svg>
      {elevation.slots.length === 0 && <p className="mt-2 text-sm text-slate-500">No equipment mounted in this rack.</p>}
      {selection && (
        <MarkerDetailPanel
          slot={selection.slot}
          selected={selection.selected}
          onClose={() => setSelection(null)}
          onSimulateFailure={setImpactTarget}
        />
      )}
      <ImpactAnalysisModal target={impactTarget} onClose={() => setImpactTarget(null)} onResult={setImpactResult} />
    </div>
  );
}

function telemetryDetail(selected: SelectedMarker): { label: string; value: string }[] {
  const { telemetry } = selected;
  if (!telemetry?.status_level || !telemetry.payload) return [];
  if (telemetry.target_type === "network_port" && "link_state" in telemetry.payload) {
    const p = telemetry.payload;
    return [
      { label: "Link state", value: telemetry.status_level },
      { label: "Utilization", value: `${p.bandwidth_util_pct.toFixed(1)}%` },
      { label: "Error rate", value: `${p.error_rate_pct.toFixed(2)}%` },
    ];
  }
  if (telemetry.target_type === "power_inlet" && "active_power_watts" in telemetry.payload) {
    const p = telemetry.payload;
    return [
      { label: "Power status", value: telemetry.status_level },
      { label: "Load", value: `${p.active_power_watts.toFixed(0)} W | ${p.current_amps.toFixed(1)} A` },
      { label: "Voltage", value: `${p.voltage.toFixed(0)} V` },
    ];
  }
  return [];
}

function MarkerDetailPanel({
  slot,
  selected,
  onClose,
  onSimulateFailure,
}: {
  slot: ElevationSlot;
  selected: SelectedMarker;
  onClose: () => void;
  onSimulateFailure: (target: ImpactTarget) => void;
}) {
  const { marker, port, powerInlet, status } = selected;
  const equipmentLabel = slot.hostname ?? slot.asset_tag;
  const telemetryRows = telemetryDetail(selected);
  const simulateTarget: ImpactTarget | null = port
    ? { type: "network_port", id: port.id, label: `${equipmentLabel} — ${port.display_name}` }
    : powerInlet
      ? { type: "power_node", id: powerInlet.power_node_id, label: `${equipmentLabel} — ${powerInlet.label}` }
      : null;

  return (
    <div data-testid="marker-detail-panel" className="mt-3 rounded border border-slate-700 bg-slate-900 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <p className="font-semibold text-slate-200">
          {equipmentLabel} — {marker.label ?? (port?.display_name ?? powerInlet?.label ?? "marker")}
        </p>
        <div className="flex gap-2">
          {simulateTarget && (
            <button
              data-testid="simulate-failure-button"
              onClick={() => onSimulateFailure(simulateTarget)}
              className="rounded bg-red-900/60 px-2 py-0.5 text-xs text-red-200 hover:bg-red-900"
            >
              Simulate failure
            </button>
          )}
          <button onClick={onClose} className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-700">
            Close
          </button>
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <dt className="text-slate-500">Type</dt>
        <dd>{marker.marker_type === "network_port" ? "Network port" : marker.marker_type === "power_supply" ? "Power supply" : marker.marker_type}</dd>
        <dt className="text-slate-500">Status</dt>
        <dd data-testid="marker-detail-status" className="capitalize" style={{ color: STATUS_COLORS[status] }}>
          {status}
        </dd>
        {port && (
          <>
            <dt className="text-slate-500">Port</dt>
            <dd>{port.display_name} ({port.connector_type}, {port.media_type})</dd>
            <dt className="text-slate-500">Cable ID</dt>
            <dd>{port.connection?.cable_id ?? "—"}</dd>
            <dt className="text-slate-500">Linked to</dt>
            <dd className="truncate">
              {port.connection?.target_port_id
                ? `Port ${port.connection.target_port_id.slice(0, 8)}…`
                : port.connection?.target_power_node_id
                  ? `PDU outlet ${port.connection.target_power_node_id.slice(0, 8)}…`
                  : "Not connected"}
            </dd>
          </>
        )}
        {powerInlet && (
          <>
            <dt className="text-slate-500">Inlet</dt>
            <dd>{powerInlet.label} ({powerInlet.connector_type})</dd>
          </>
        )}
        {telemetryRows.map((row) => (
          <Fragment key={row.label}>
            <dt className="text-slate-500">{row.label}</dt>
            <dd data-testid="marker-telemetry-value">{row.value}</dd>
          </Fragment>
        ))}
      </dl>
      {!selected.telemetry && (
        <p className="mt-2 text-xs italic text-slate-500">No live telemetry binding for this marker.</p>
      )}
      <a href={`/equipment/${slot.equipment_id}`} className="mt-2 inline-block text-xs text-blue-400 hover:underline">
        Open equipment for full telemetry →
      </a>
    </div>
  );
}
