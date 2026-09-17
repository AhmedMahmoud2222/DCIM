import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { getEquipment, moveEquipment, retireEquipment } from "@/features/equipment/api";
import { createEquipmentFeed, getEquipmentPowerSummary } from "@/features/power/api";
import { listRacks, listRooms } from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";
import { PLACEMENT_TYPES, PlacementType, SIDES, Side } from "@/types";

const REDUNDANCY_LABELS: Record<string, { text: string; color: string }> = {
  dual_feed_healthy: { text: "Dual-feed (A+B), healthy", color: "bg-green-900 text-green-200" },
  single_feed: { text: "Single feed (no redundancy)", color: "bg-slate-700 text-slate-300" },
  degraded: { text: "Redundancy degraded", color: "bg-yellow-800 text-yellow-100" },
  no_power_modeled: { text: "No power modeled", color: "bg-slate-800 text-slate-500" },
};

const LIFECYCLE_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function EquipmentDetailPage() {
  const { equipmentId } = useParams<{ equipmentId: string }>();
  const queryClient = useQueryClient();
  const [showMoveForm, setShowMoveForm] = useState(false);
  const [placementType, setPlacementType] = useState<PlacementType>("floor_standing");
  const [roomId, setRoomId] = useState("");
  const [rackId, setRackId] = useState("");
  const [uStart, setUStart] = useState("");
  const [uEnd, setUEnd] = useState("");
  const [side, setSide] = useState<Side>("front");

  const equipmentQuery = useQuery({
    queryKey: ["equipment", equipmentId],
    queryFn: () => getEquipment(equipmentId!),
    enabled: !!equipmentId,
  });
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks });
  const powerSummaryQuery = useQuery({
    queryKey: ["power", "equipment-summary", equipmentId],
    queryFn: () => getEquipmentPowerSummary(equipmentId!),
    enabled: !!equipmentId,
  });

  const addFeedMutation = useMutation({
    mutationFn: (label: string) => createEquipmentFeed(equipmentId!, label),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["power", "equipment-summary", equipmentId] }),
  });

  const moveMutation = useMutation({
    mutationFn: () =>
      moveEquipment(equipmentId!, {
        placement_type: placementType,
        room_id: roomId,
        rack_id: placementType === "rack_mounted" ? rackId : undefined,
        u_start: placementType === "rack_mounted" ? Number(uStart) : undefined,
        u_end: placementType === "rack_mounted" ? Number(uEnd) : undefined,
        side: placementType === "rack_mounted" ? side : undefined,
      }),
    onSuccess: () => {
      setShowMoveForm(false);
      queryClient.invalidateQueries({ queryKey: ["equipment"] });
    },
  });

  const retireMutation = useMutation({
    mutationFn: () => retireEquipment(equipmentId!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["equipment"] }),
  });

  function handleMoveSubmit(e: FormEvent) {
    e.preventDefault();
    if (!roomId) return;
    if (placementType === "rack_mounted" && (!rackId || !uStart || !uEnd)) return;
    moveMutation.mutate();
  }

  if (equipmentQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (equipmentQuery.error) return <p className="text-sm text-red-400">{(equipmentQuery.error as Error).message}</p>;
  const equipment = equipmentQuery.data;
  if (!equipment) return null;

  const currentRoom = roomsQuery.data?.items.find((r) => r.id === equipment.placement?.room_id);
  const currentRack = equipment.placement?.rack_id ? racksQuery.data?.items.find((r) => r.id === equipment.placement!.rack_id) : null;

  return (
    <div>
      <Link to="/equipment" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Equipment
      </Link>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-lg font-semibold">{equipment.hostname ?? equipment.asset_tag}</h1>
          <p className="font-mono text-sm text-slate-400">{equipment.asset_tag}</p>
        </div>
        <span className={`rounded px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[equipment.lifecycle_status] ?? "bg-slate-700"}`}>
          {equipment.lifecycle_status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-6">
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Placement</h2>
          {equipment.placement ? (
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-slate-500">Type</dt>
                <dd>{equipment.placement.placement_type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-500">Room</dt>
                <dd>{currentRoom?.name ?? equipment.placement.room_id}</dd>
              </div>
              {equipment.placement.placement_type === "rack_mounted" && (
                <>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Rack</dt>
                    <dd>{currentRack?.name ?? equipment.placement.rack_id}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">U-range</dt>
                    <dd>
                      U{equipment.placement.u_start}–{equipment.placement.u_end} ({equipment.placement.side})
                    </dd>
                  </div>
                </>
              )}
            </dl>
          ) : (
            <p className="text-sm italic text-slate-500">Not currently placed.</p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setShowMoveForm((v) => !v)}
              className="rounded bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
            >
              {equipment.placement ? "Move" : "Place"}
            </button>
            {equipment.placement && (
              <button
                onClick={() => retireMutation.mutate()}
                disabled={retireMutation.isPending}
                className="rounded bg-red-900/50 px-3 py-1.5 text-sm text-red-200 hover:bg-red-900 disabled:opacity-50"
              >
                Retire placement
              </button>
            )}
          </div>

          {showMoveForm && (
            <form onSubmit={handleMoveSubmit} className="mt-4 space-y-2 border-t border-slate-800 pt-4">
              <select
                value={placementType}
                onChange={(e) => setPlacementType(e.target.value as PlacementType)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                {PLACEMENT_TYPES.map((pt) => (
                  <option key={pt} value={pt}>
                    {pt}
                  </option>
                ))}
              </select>
              <select
                value={roomId}
                onChange={(e) => setRoomId(e.target.value)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select room…</option>
                {roomsQuery.data?.items.map((room) => (
                  <option key={room.id} value={room.id}>
                    {room.name} ({room.code})
                  </option>
                ))}
              </select>

              {placementType === "rack_mounted" && (
                <>
                  <select
                    value={rackId}
                    onChange={(e) => setRackId(e.target.value)}
                    className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                  >
                    <option value="">Select rack…</option>
                    {racksQuery.data?.items.map((rack) => (
                      <option key={rack.id} value={rack.id}>
                        {rack.name}
                      </option>
                    ))}
                  </select>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      min={1}
                      value={uStart}
                      onChange={(e) => setUStart(e.target.value)}
                      placeholder="U start"
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <input
                      type="number"
                      min={2}
                      value={uEnd}
                      onChange={(e) => setUEnd(e.target.value)}
                      placeholder="U end (exclusive)"
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <select
                      value={side}
                      onChange={(e) => setSide(e.target.value as Side)}
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    >
                      {SIDES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              )}

              <button
                type="submit"
                disabled={moveMutation.isPending}
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Confirm
              </button>
              {moveMutation.isError && (
                <p className="text-sm text-red-400">
                  {moveMutation.error instanceof ApiError && moveMutation.error.status === 409
                    ? "That U-range conflicts with existing equipment, or someone else moved this item — reload and try again."
                    : (moveMutation.error as Error).message}
                </p>
              )}
            </form>
          )}
        </div>

        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Details</h2>
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-slate-500">Owner</dt>
              <dd>{equipment.owner ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Service</dt>
              <dd>{equipment.service ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Environment</dt>
              <dd>{equipment.environment ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Version</dt>
              <dd>{equipment.version}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Created</dt>
              <dd>{new Date(equipment.created_at).toLocaleString()}</dd>
            </div>
          </dl>
        </div>
      </div>

      <div className="rounded border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Power</h2>
          <div className="flex gap-2">
            <button
              onClick={() => addFeedMutation.mutate("Feed A")}
              disabled={addFeedMutation.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed A
            </button>
            <button
              onClick={() => addFeedMutation.mutate("Feed B")}
              disabled={addFeedMutation.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed B
            </button>
            <Link to="/power" className="rounded bg-slate-800 px-2 py-1 text-xs text-blue-400 hover:bg-slate-700">
              Open topology →
            </Link>
          </div>
        </div>
        {powerSummaryQuery.data && (
          <>
            {(() => {
              const r = REDUNDANCY_LABELS[powerSummaryQuery.data.redundancy_classification];
              return <span className={`mb-3 inline-block rounded px-2 py-0.5 text-xs ${r.color}`}>{r.text}</span>;
            })()}
            <div className="mt-2 space-y-1">
              {powerSummaryQuery.data.feed_nodes.map((feed) => (
                <div key={feed.power_node_id} className="flex items-center justify-between rounded bg-slate-800/50 px-3 py-1.5 text-xs">
                  <span className="font-mono text-slate-400">{feed.power_node_id.slice(0, 8)}…</span>
                  <span>{feed.feed_label ?? "unlabeled"}</span>
                  <span className={feed.has_upstream_path ? "text-green-400" : "text-red-400"}>
                    {feed.has_upstream_path ? "path OK" : "no upstream path"}
                  </span>
                  <span>{feed.effective_capacity_kw === null ? "capacity unknown" : `${feed.effective_capacity_kw.toFixed(1)} kW`}</span>
                </div>
              ))}
              {powerSummaryQuery.data.feed_nodes.length === 0 && (
                <p className="text-xs italic text-slate-500">No power feeds modeled for this equipment yet.</p>
              )}
            </div>
            {powerSummaryQuery.data.effective_demand_kw !== null && (
              <p className="mt-2 text-xs text-slate-400">
                Effective demand: {powerSummaryQuery.data.effective_demand_kw.toFixed(1)} kW
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
