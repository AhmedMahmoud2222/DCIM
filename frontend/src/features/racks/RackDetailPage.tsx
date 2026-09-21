import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { getEquipmentPowerSummary } from "@/features/power/api";
import { getRack, getRackElevation, listRooms, moveRack, retireRack } from "@/features/racks/api";
import { RackElevationView } from "@/features/racks/RackElevationView";
import { ApiError } from "@/lib/apiClient";
import { PageHeader, SectionTitle, StatusBadge } from "@/components/ui/ProductUi";
import { ElevationSlot } from "@/types";

export function RackDetailPage() {
  const { rackId } = useParams<{ rackId: string }>();
  const queryClient = useQueryClient();
  const [showMoveForm, setShowMoveForm] = useState(false);
  const [moveRoomId, setMoveRoomId] = useState("");
  const [moveX, setMoveX] = useState("");
  const [moveY, setMoveY] = useState("");
  const [inspectedSlot, setInspectedSlot] = useState<ElevationSlot | null>(null);

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

  if (rackQuery.isLoading) return <div className="surface-muted p-5 text-sm text-slate-400">Loading rack and elevation…</div>;
  if (rackQuery.error) return <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">{(rackQuery.error as Error).message}</div>;
  const rack = rackQuery.data;
  if (!rack) return null;

  const currentRoom = roomsQuery.data?.items.find((r) => r.id === rack.placement?.room_id);
  const mountedUnits = new Set(elevationQuery.data?.slots.flatMap((slot) => Array.from({ length: slot.u_end - slot.u_start }, (_, index) => slot.u_start + index)) ?? []).size;
  const selectedSlot = inspectedSlot ?? elevationQuery.data?.slots[0] ?? null;

  return (
    <div className="page flex flex-col gap-5 !space-y-0">
      <Link to="/racks" className="text-sm text-slate-400 hover:text-indigo-300">← All racks</Link>
      <PageHeader eyebrow={rack.asset_tag} title={rack.name} description="Physical placement, capacity, elevation, and modeled power context." actions={<StatusBadge label={rack.lifecycle_status} tone={rack.lifecycle_status === "active" ? "healthy" : "neutral"} />} />

      <div className="order-2 grid gap-6 lg:grid-cols-2">
        <div className="surface p-5">
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

        <div className="surface p-5">
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

      <div className="surface order-1 p-4">
        <SectionTitle title="Rack elevation" detail={`${elevationQuery.data?.height_u ?? "—"}U cabinet · select equipment for its operational view`} />
        {elevationQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {elevationQuery.data && <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
          <RackElevationView elevation={elevationQuery.data} selectedEquipmentId={selectedSlot?.equipment_id} onInspect={setInspectedSlot} />
          <aside className="surface-muted flex flex-col p-5">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-indigo-300">Cabinet context</p>
            <h3 className="mt-2 text-xl font-semibold text-slate-100">{rack.name}</h3>
            <dl className="mt-5 space-y-3 text-sm">
              <div className="flex items-center justify-between gap-4"><dt className="text-slate-500">Capacity</dt><dd className="font-medium text-slate-200">{elevationQuery.data.height_u}U</dd></div>
              <div className="flex items-center justify-between gap-4"><dt className="text-slate-500">Occupied</dt><dd className="font-medium text-slate-200">{mountedUnits}U</dd></div>
              <div className="flex items-center justify-between gap-4"><dt className="text-slate-500">Free</dt><dd className="font-medium text-emerald-300">{Math.max(elevationQuery.data.height_u - mountedUnits, 0)}U</dd></div>
              <div className="flex items-center justify-between gap-4"><dt className="text-slate-500">Utilization</dt><dd className="font-medium text-slate-200">{elevationQuery.data.height_u ? `${Math.round((mountedUnits / elevationQuery.data.height_u) * 100)}%` : "—"}</dd></div>
              <div className="flex items-center justify-between gap-4"><dt className="text-slate-500">Room</dt><dd className="truncate text-right text-slate-200">{currentRoom?.name ?? "Unplaced"}</dd></div>
            </dl>
            {selectedSlot ? <div className="mt-6 rounded-lg border border-indigo-400/25 bg-indigo-500/10 p-4">
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-indigo-200">Inspected equipment</p>
              <p className="mt-2 font-semibold text-slate-100">{selectedSlot.hostname ?? selectedSlot.asset_tag}</p>
              <p className="mt-1 text-sm text-slate-400">{selectedSlot.asset_tag} · U{selectedSlot.u_start}–U{selectedSlot.u_end} · {selectedSlot.side}</p>
              <Link to={`/equipment/${selectedSlot.equipment_id}`} className="mt-4 inline-flex text-sm font-medium text-indigo-300 hover:text-indigo-200">Open equipment view →</Link>
            </div> : <p className="mt-6 text-sm text-slate-500">No mounted equipment is available to inspect.</p>}
          </aside>
        </div>}
      </div>

      <div className="surface order-3 p-5">
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
