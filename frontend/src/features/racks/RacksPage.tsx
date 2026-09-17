import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { createRack, createRackModel, createRackModelRevision, listRacks, listRooms } from "@/features/racks/api";

const LIFECYCLE_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function RacksPage() {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);

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
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Racks</h1>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
        >
          {showForm ? "Cancel" : "New Rack"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 grid max-w-3xl grid-cols-2 gap-3 rounded border border-slate-800 bg-slate-900 p-4">
          <div className="col-span-2 text-xs uppercase tracking-wide text-slate-500">Rack identity</div>
          <input
            value={assetTag}
            onChange={(e) => setAssetTag(e.target.value)}
            placeholder="Asset tag (e.g. RACK-A01)"
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Display name"
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <select
            value={roomId}
            onChange={(e) => setRoomId(e.target.value)}
            className="col-span-2 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
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
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="Model name"
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Height (U)
            <input
              type="number"
              min={1}
              max={60}
              value={heightU}
              onChange={(e) => setHeightU(e.target.value)}
              className="w-20 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
            />
          </label>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Width (mm)
              <input
                type="number"
                value={widthMm}
                onChange={(e) => setWidthMm(e.target.value)}
                className="w-20 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
              />
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Depth (mm)
              <input
                type="number"
                value={depthMm}
                onChange={(e) => setDepthMm(e.target.value)}
                className="w-20 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
              />
            </label>
          </div>

          <div className="col-span-2 mt-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create Rack"}
            </button>
            {createMutation.isError && (
              <p className="mt-2 text-sm text-red-400">{(createMutation.error as Error).message}</p>
            )}
          </div>
        </form>
      )}

      {racksQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {racksQuery.error && <p className="text-sm text-red-400">{(racksQuery.error as Error).message}</p>}

      {racksQuery.data && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Name</th>
              <th className="pb-2">Asset Tag</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Room</th>
              <th className="pb-2">Position</th>
            </tr>
          </thead>
          <tbody>
            {racksQuery.data.items.map((rack) => (
              <tr key={rack.id} className="border-b border-slate-900 hover:bg-slate-900/50">
                <td className="py-2">
                  <Link to={`/racks/${rack.id}`} className="font-medium text-blue-400 hover:underline">
                    {rack.name}
                  </Link>
                </td>
                <td className="py-2 font-mono text-slate-300">{rack.asset_tag}</td>
                <td className="py-2">
                  <span className={`rounded px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[rack.lifecycle_status] ?? "bg-slate-700"}`}>
                    {rack.lifecycle_status}
                  </span>
                </td>
                <td className="py-2 text-slate-400">
                  {rack.placement
                    ? roomsQuery.data?.items.find((r) => r.id === rack.placement!.room_id)?.name ?? rack.placement.room_id
                    : <span className="italic text-slate-600">unplaced</span>}
                </td>
                <td className="py-2 text-slate-500">
                  {rack.placement?.x_mm != null && rack.placement?.y_mm != null
                    ? `(${rack.placement.x_mm}, ${rack.placement.y_mm}) mm`
                    : "—"}
                </td>
              </tr>
            ))}
            {racksQuery.data.items.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-center text-slate-500">
                  No racks yet. Create one to get started.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
