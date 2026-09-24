import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { fetchGraphicImage, getRevision } from "@/features/catalog-designer/api";
import { getEquipmentPorts } from "@/features/equipment/api";
import { getEquipmentPowerSummary } from "@/features/power/api";
import { CatalogGraphicMarker, EquipmentPort, EquipmentPowerInlet } from "@/types";

type MarkerStatus = "connected" | "planned" | "faulted" | "unassigned";

const STATUS_COLORS: Record<MarkerStatus, string> = {
  connected: "#22c55e",
  planned: "#eab308",
  faulted: "#ef4444",
  unassigned: "#64748b",
};

export interface SelectedMarker {
  marker: CatalogGraphicMarker;
  port: EquipmentPort | null;
  powerInlet: EquipmentPowerInlet | null;
  status: MarkerStatus;
}

/** Blob-URL bridge, same technique as GraphicsEditorPage.tsx's own (module-private)
 * useGraphicImageUrl — duplicated here rather than imported since that hook isn't
 * exported and this component must stay read-only/side-effect-free with no create/
 * update/delete mutations wired in, unlike the editor's canvas. */
function useGraphicImageUrl(revisionId: string, side: "front" | "rear", graphicId: string | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!graphicId) {
      setUrl(null);
      return;
    }
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    fetchGraphicImage(revisionId, side).then((blob) => {
      if (cancelled) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphicId]);
  return url;
}

/** Read-only faceplate + marker overlay for the rack elevation view (Phase 10B). Renders
 * the catalog's front/rear graphic (Phase 10A) scaled to the slot, with a marker per
 * network port / power supply, colored by live connectivity — unlike
 * GraphicsEditorPage.tsx's FrontRearImageCanvas, this never drags, creates, or deletes a
 * marker; onSelect is the only interaction. Renders nothing (letting the caller fall back
 * to the plain colored box) when the equipment's catalog revision has no graphic for the
 * requested side. */
export function FaceplateOverlay({
  catalogModelRevisionId,
  equipmentId,
  side,
  onSelect,
}: {
  catalogModelRevisionId: string;
  equipmentId: string;
  side: "front" | "rear";
  onSelect?: (selection: SelectedMarker) => void;
}) {
  const revisionQuery = useQuery({
    queryKey: ["catalog", "revisions", catalogModelRevisionId],
    queryFn: () => getRevision(catalogModelRevisionId),
  });
  const portsQuery = useQuery({
    queryKey: ["equipment", equipmentId, "ports"],
    queryFn: () => getEquipmentPorts(equipmentId),
  });
  const powerSummaryQuery = useQuery({
    queryKey: ["power", "equipment-summary", equipmentId],
    queryFn: () => getEquipmentPowerSummary(equipmentId),
  });

  const graphic = revisionQuery.data?.graphics.find((g) => g.side === side);
  const imageUrl = useGraphicImageUrl(catalogModelRevisionId, side, graphic?.id);

  if (!graphic || !imageUrl) return null;

  function statusFor(marker: CatalogGraphicMarker): { status: MarkerStatus; port: EquipmentPort | null; inlet: EquipmentPowerInlet | null } {
    if (marker.marker_type === "network_port") {
      const port = portsQuery.data?.ports.find((p) => p.network_port_template_id === marker.network_port_template_id) ?? null;
      if (!port?.connection) return { status: "unassigned", port, inlet: null };
      const raw = port.connection.status;
      const status: MarkerStatus = raw === "active" ? "connected" : raw === "planned" ? "planned" : "faulted";
      return { status, port, inlet: null };
    }
    if (marker.marker_type === "power_supply") {
      // A PowerSupplyTemplate with quantity > 1 instantiates several EquipmentPowerInlet
      // rows from one marker — this overlay shows the first inlet's feed status as a
      // representative summary rather than fanning one marker out into several dots,
      // matching how the marker itself is placed once on the catalog graphic (§4.5).
      const inlet =
        portsQuery.data?.power_inlets.find((i) => i.power_supply_template_id === marker.power_supply_template_id) ?? null;
      const feed = inlet ? powerSummaryQuery.data?.feed_nodes.find((f) => f.power_node_id === inlet.power_node_id) : undefined;
      const status: MarkerStatus = feed?.has_upstream_path ? "connected" : "unassigned";
      return { status, port: null, inlet };
    }
    return { status: "unassigned", port: null, inlet: null };
  }

  return (
    <div
      data-testid="faceplate-overlay"
      className="relative w-full overflow-hidden rounded border border-slate-800 bg-slate-950"
      style={{ aspectRatio: `${graphic.width_px} / ${graphic.height_px}` }}
    >
      <img src={imageUrl} alt={`${side} view`} className="pointer-events-none h-full w-full select-none object-contain" draggable={false} />
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="absolute inset-0 h-full w-full">
        {graphic.markers.map((marker) => {
          const { status, port, inlet } = statusFor(marker);
          return (
            <g
              key={marker.id}
              data-testid="faceplate-marker"
              data-marker-status={status}
              tabIndex={onSelect ? 0 : -1}
              role={onSelect ? "button" : undefined}
              aria-label={`${marker.marker_type === "network_port" ? "Port" : "Power"} ${marker.label ?? ""}: ${status}`}
              className={onSelect ? "cursor-pointer outline-none" : undefined}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onSelect?.({ marker, port, powerInlet: inlet, status });
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  e.stopPropagation();
                  onSelect?.({ marker, port, powerInlet: inlet, status });
                }
              }}
            >
              <circle cx={marker.marker_x} cy={marker.marker_y} r={0.016} fill={STATUS_COLORS[status]} stroke="#0f172a" strokeWidth={0.003} />
              <circle
                cx={marker.marker_x}
                cy={marker.marker_y}
                r={0.024}
                fill="transparent"
                stroke="#3b82f6"
                strokeWidth={0.004}
                className="opacity-0 focus-visible:opacity-100"
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export type { MarkerStatus };
export { STATUS_COLORS };
