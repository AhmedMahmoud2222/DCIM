import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { DataTable, EmptyRow, RowCount, TD, TH, TR } from "@/components/data/DataTable";
import { QueryStatus } from "@/components/data/QueryStatus";
import { PageHeader } from "@/components/layout/PageHeader";
import { useHasPermission } from "@/features/auth/useAuthorization";
import { BulkImportPanel } from "@/features/bulk-import/BulkImportPanel";
import { createRack, createRackModel, createRackModelRevision, listRacks, listRooms } from "@/features/racks/api";
import { LifecycleStatus } from "@/features/status/StatusBadges";

export function RacksPage() {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [showBulkImport, setShowBulkImport] = useState(false);
  const canImport = useHasPermission("rack:import");

  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks });
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });

  const [assetTag, setAssetTag] = useState("");
  const [name, setName] = useState("");
  const [roomId, setRoomId] = useState("");
  const [manufacturer, setManufacturer] = useState("");
  const [modelName, setModelName] = useState("");
  const [heightU, setHeightU] = useState("42");
  const [widthMm, setWidthMm] = useState("600");
  const [depthMm, setDepthMm] = useState("1000");

  const createMutation = useMutation({
    mutationFn: async () => {
      const model = await createRackModel(manufacturer.trim(), modelName.trim());
      const revision = await createRackModelRevision(model.id, {
        height_u: Number(heightU),
        width_mm: Number(widthMm),
        depth_mm: Number(depthMm),
      });
      return createRack(
        {
          asset_tag: assetTag.trim(),
          model_revision_id: revision.id,
          name: name.trim(),
          room_id: roomId || undefined,
        },
        crypto.randomUUID(),
      );
    },
    onSuccess: () => {
      setAssetTag("");
      setName("");
      setShowForm(false);
      queryClient.invalidateQueries({ queryKey: ["racks"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (assetTag.trim() && name.trim() && manufacturer.trim() && modelName.trim()) createMutation.mutate();
  }

  return (
    <div>
      <PageHeader
        title="Racks"
        actions={
          <>
            {canImport && (
              <button
                onClick={() => setShowBulkImport(true)}
                className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-100 hover:bg-slate-700"
              >
                Bulk Import
              </button>
            )}
            <button
              onClick={() => setShowForm((v) => !v)}
              className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
            >
              {showForm ? "Cancel" : "New Rack"}
            </button>
          </>
        }
      />

      {showBulkImport && (
        <BulkImportPanel
          resource="rack"
          resourceLabel="Rack"
          onClose={() => setShowBulkImport(false)}
          onCommitted={() => queryClient.invalidateQueries({ queryKey: ["racks"] })}
        />
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 grid max-w-3xl grid-cols-2 gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4">
          <div className="col-span-2 text-xs uppercase tracking-wide text-slate-500">Rack identity</div>
          <input
            value={assetTag}
            onChange={(e) => setAssetTag(e.target.value)}
            placeholder="Asset tag (e.g. RACK-A01)"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Display name"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <select
            value={roomId}
            onChange={(e) => setRoomId(e.target.value)}
            className="col-span-2 rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          >
            <option value="">No initial placement (place later)</option>
            {roomsQuery.data?.items.map((room) => (
              <option key={room.id} value={room.id}>
                {room.name} ({room.code})
              </option>
            ))}
          </select>

          <div className="col-span-2 mt-2 text-xs uppercase tracking-wide text-slate-500">
            Rack model (a new catalog model + revision is created together — see the Rack Model catalog via the API for
            reusing an existing one)
          </div>
          <input
            value={manufacturer}
            onChange={(e) => setManufacturer(e.target.value)}
            placeholder="Manufacturer"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="Model name"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Height (U)
            <input
              type="number"
              min={1}
              max={60}
              value={heightU}
              onChange={(e) => setHeightU(e.target.value)}
              className="w-20 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
            />
          </label>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Width (mm)
              <input
                type="number"
                value={widthMm}
                onChange={(e) => setWidthMm(e.target.value)}
                className="w-20 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
              />
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Depth (mm)
              <input
                type="number"
                value={depthMm}
                onChange={(e) => setDepthMm(e.target.value)}
                className="w-20 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
              />
            </label>
          </div>

          <div className="col-span-2 mt-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create Rack"}
            </button>
            {createMutation.isError && (
              <p className="mt-2 text-sm text-red-400">{(createMutation.error as Error).message}</p>
            )}
          </div>
        </form>
      )}

      <QueryStatus query={racksQuery} what="racks" permission="rack:read" columns={5} />

      {racksQuery.data && (
        <>
          <RowCount shown={racksQuery.data.items.length} total={racksQuery.data.total} noun="racks" />
          <DataTable label="Racks" minWidthClass="min-w-[44rem]">
            <thead>
              <tr className="border-b border-line-subtle bg-surface-panel">
                <th scope="col" className={TH}>Name</th>
                <th scope="col" className={TH}>Asset Tag</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={TH}>Room</th>
                <th scope="col" className={TH}>Position</th>
              </tr>
            </thead>
            <tbody>
              {racksQuery.data.items.map((rack) => (
                <tr key={rack.id} className={TR}>
                  <td className={TD}>
                    <Link to={`/racks/${rack.id}`} className="font-medium text-link hover:underline">
                      {rack.name}
                    </Link>
                  </td>
                  <td className={`${TD} font-mono text-fg-secondary`}>{rack.asset_tag}</td>
                  <td className={TD}>
                    <LifecycleStatus value={rack.lifecycle_status} />
                  </td>
                  <td className={`${TD} text-fg-secondary`}>
                    {rack.placement
                      ? roomsQuery.data?.items.find((r) => r.id === rack.placement!.room_id)?.name ?? rack.placement.room_id
                      : <span className="italic">unplaced</span>}
                  </td>
                  <td className={`${TD} whitespace-nowrap tabular-nums text-fg-secondary`}>
                    {rack.placement?.x_mm != null && rack.placement?.y_mm != null
                      ? `(${rack.placement.x_mm}, ${rack.placement.y_mm}) mm`
                      : "—"}
                  </td>
                </tr>
              ))}
              {racksQuery.data.items.length === 0 && <EmptyRow columns={5}>No racks yet. Create one to get started.</EmptyRow>}
            </tbody>
          </DataTable>
        </>
      )}
    </div>
  );
}
