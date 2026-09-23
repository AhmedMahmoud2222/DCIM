import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { createRack, listRackModelRevisions, listRackModels, listRacks, listRooms } from "@/features/racks/api";

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
  const rackModelsQuery = useQuery({ queryKey: ["rack-models"], queryFn: listRackModels });

  const [assetTag, setAssetTag] = useState("");
  const [name, setName] = useState("");
  const [roomId, setRoomId] = useState("");
  const [modelId, setModelId] = useState("");
  const [revisionId, setRevisionId] = useState("");

  const rackModelRevisionsQuery = useQuery({
    queryKey: ["rack-model-revisions", modelId],
    queryFn: () => listRackModelRevisions(modelId),
    enabled: modelId !== "",
  });

  // Picking a different model invalidates whatever revision was selected for the
  // previous one — a revision id only makes sense scoped to its own model.
  useEffect(() => {
    setRevisionId("");
  }, [modelId]);

  const createMutation = useMutation({
    mutationFn: () =>
      createRack(
        {
          asset_tag: assetTag.trim(),
          model_revision_id: revisionId,
          name: name.trim(),
          room_id: roomId || undefined,
        },
        crypto.randomUUID(),
      ),
    onSuccess: () => {
      setAssetTag("");
      setName("");
      setModelId("");
      setShowForm(false);
      queryClient.invalidateQueries({ queryKey: ["racks"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (assetTag.trim() && name.trim() && revisionId) createMutation.mutate();
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
            Rack model — select an existing catalog model and revision (new catalog models are created by an
            administrator through the catalog)
          </div>
          <label className="col-span-2 flex flex-col gap-1 text-xs text-slate-400">
            Model
            <select
              aria-label="Rack model"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Choose a model</option>
              {rackModelsQuery.data?.items.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.manufacturer} {model.model_name}
                </option>
              ))}
            </select>
          </label>
          <label className="col-span-2 flex flex-col gap-1 text-xs text-slate-400">
            Revision
            <select
              aria-label="Rack model revision"
              value={revisionId}
              onChange={(e) => setRevisionId(e.target.value)}
              disabled={!modelId}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none disabled:opacity-50"
            >
              <option value="">{modelId ? "Choose a revision" : "Choose a model first"}</option>
              {rackModelRevisionsQuery.data?.items.map((revision) => (
                <option key={revision.id} value={revision.id}>
                  {revision.height_u}U · {revision.width_mm}×{revision.depth_mm}mm
                </option>
              ))}
            </select>
          </label>

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
