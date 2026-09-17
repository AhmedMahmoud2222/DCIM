import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { getEquipmentPowerSummary } from "@/features/power/api";
import { getRack, getRackElevation, listRooms, moveRack, retireRack } from "@/features/racks/api";
import { RackElevationView } from "@/features/racks/RackElevationView";
import { ApiError } from "@/lib/apiClient";

const LIFECYCLE_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function RackDetailPage() {
  const { rackId } = useParams<{ rackId: string }>();
  const queryClient = useQueryClient();
  const [showMoveForm, setShowMoveForm] = useState(false);
  const [moveRoomId, setMoveRoomId] = useState("");
  const [moveX, setMoveX] = useState("");
  const [moveY, setMoveY] = useState("");

  const rackQuery = useQuery({ queryKey: ["racks", rackId], queryFn: () => getRack(rackId!), enabled: !!rackId });
  const elevationQuery = useQuery({
    queryKey: ["racks", rackId, "elevation"],
    queryFn: () => getRackElevation(rackId!),
    enabled: !!rackId,
  });
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });

  const equipmentIds = elevationQuery.data?.slots.map((s) => s.equipment_id) ?? [];
  const powerSummaryQueries = useQueries({
    queries: equipmentIds.map((id) => ({
      queryKey: ["power", "equipment-summary", id],
      queryFn: () => getEquipmentPowerSummary(id),
    })),
  });

  const moveMutation = useMutation({
    mutationFn: () =>
      moveRack(rackId!, {
        room_id: moveRoomId,
        x_mm: moveX ? Number(moveX) : undefined,
        y_mm: moveY ? Number(moveY) : undefined,
      }),
    onSuccess: () => {
      setShowMoveForm(false);
      queryClient.invalidateQueries({ queryKey: ["racks"] });
    },
  });

  const retireMutation = useMutation({
    mutationFn: () => retireRack(rackId!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["racks"] }),
  });

  function handleMoveSubmit(e: FormEvent) {
    e.preventDefault();
    if (moveRoomId) moveMutation.mutate();
  }

  if (rackQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (rackQuery.error) return <p className="text-sm text-red-400">{(rackQuery.error as Error).message}</p>;
  const rack = rackQuery.data;
  if (!rack) return null;

  const currentRoom = roomsQuery.data?.items.find((r) => r.id === rack.placement?.room_id);

  return (
    <div>
      <Link to="/racks" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Racks
      </Link>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-lg font-semibold">{rack.name}</h1>
          <p className="font-mono text-sm text-slate-400">{rack.asset_tag}</p>
        </div>
        <span className={`rounded px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[rack.lifecycle_status] ?? "bg-slate-700"}`}>
          {rack.lifecycle_status}
        </span>
      </div>

      <div className="mb-6 grid grid-cols-2 gap-6">
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Placement</h2>
          {rack.placement ? (
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-slate-500">Room</dt>
                <dd>{currentRoom?.name ?? rack.placement.room_id}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-500">Position</dt>
                <dd>
                  {rack.placement.x_mm != null && rack.placement.y_mm != null
                    ? `(${rack.placement.x_mm}, ${rack.placement.y_mm}) mm`
                    : "not drawn"}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-500">Rotation</dt>
                <dd>{rack.placement.rotation_deg ?? 0}°</dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm italic text-slate-500">Not currently placed in any room.</p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setShowMoveForm((v) => !v)}
              className="rounded bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
            >
              {rack.placement ? "Move" : "Place"}
            </button>
            {rack.placement && (
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
                value={moveRoomId}
                onChange={(e) => setMoveRoomId(e.target.value)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select room…</option>
                {roomsQuery.data?.items.map((room) => (
                  <option key={room.id} value={room.id}>
                    {room.name} ({room.code})
                  </option>
                ))}
              </select>
              <div className="flex gap-2">
                <input
                  type="number"
                  value={moveX}
                  onChange={(e) => setMoveX(e.target.value)}
                  placeholder="x_mm"
                  className="w-1/2 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                />
                <input
                  type="number"
                  value={moveY}
                  onChange={(e) => setMoveY(e.target.value)}
                  placeholder="y_mm"
                  className="w-1/2 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                />
              </div>
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
                    ? "Someone else moved this rack in the meantime — reload and try again."
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
              <dd>{rack.owner ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Version</dt>
              <dd>{rack.version}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Created</dt>
              <dd>{new Date(rack.created_at).toLocaleString()}</dd>
            </div>
          </dl>
          {rack.notes && <p className="mt-3 text-sm text-slate-400">{rack.notes}</p>}
        </div>
      </div>

      <div className="mb-6 rounded border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Elevation</h2>
        {elevationQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {elevationQuery.data && <RackElevationView elevation={elevationQuery.data} />}
      </div>

      <div className="rounded border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Rack Power Summary</h2>
          <Link to="/power" className="rounded bg-slate-800 px-2 py-1 text-xs text-blue-400 hover:bg-slate-700">
            Open topology →
          </Link>
        </div>
        {equipmentIds.length === 0 && <p className="text-xs italic text-slate-500">No equipment mounted in this rack.</p>}
        <div className="space-y-1">
          {elevationQuery.data?.slots.map((slot, i) => {
            const summary = powerSummaryQueries[i]?.data;
            return (
              <div key={slot.equipment_id} className="flex items-center justify-between rounded bg-slate-800/50 px-3 py-1.5 text-xs">
                <span>{slot.asset_tag}</span>
                {summary ? (
                  <>
                    <span>{summary.redundancy_classification.replace(/_/g, " ")}</span>
                    <span>
                      {summary.effective_demand_kw === null ? "demand unknown" : `${summary.effective_demand_kw.toFixed(1)} kW`}
                    </span>
                  </>
                ) : (
                  <span className="text-slate-500">loading…</span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
