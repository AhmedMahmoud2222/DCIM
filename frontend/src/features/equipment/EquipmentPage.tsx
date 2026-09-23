import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  createEquipment,
  listEquipment,
  listEquipmentModelRevisions,
  listEquipmentModels,
} from "@/features/equipment/api";
import { listRacks } from "@/features/racks/api";

const LIFECYCLE_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function EquipmentPage() {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);

  const equipmentQuery = useQuery({ queryKey: ["equipment"], queryFn: listEquipment });
  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks });
  const equipmentModelsQuery = useQuery({ queryKey: ["equipment-models"], queryFn: listEquipmentModels });

  const [assetTag, setAssetTag] = useState("");
  const [hostname, setHostname] = useState("");
  const [modelId, setModelId] = useState("");
  const [revisionId, setRevisionId] = useState("");

  const equipmentModelRevisionsQuery = useQuery({
    queryKey: ["equipment-model-revisions", modelId],
    queryFn: () => listEquipmentModelRevisions(modelId),
    enabled: modelId !== "",
  });

  // Picking a different model invalidates whatever revision was selected for the
  // previous one — a revision id only makes sense scoped to its own model.
  useEffect(() => {
    setRevisionId("");
  }, [modelId]);

  const createMutation = useMutation({
    mutationFn: () =>
      createEquipment(
        { asset_tag: assetTag.trim(), model_revision_id: revisionId, hostname: hostname.trim() || undefined },
        crypto.randomUUID(),
      ),
    onSuccess: () => {
      setAssetTag("");
      setHostname("");
      setModelId("");
      setShowForm(false);
      queryClient.invalidateQueries({ queryKey: ["equipment"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (assetTag.trim() && revisionId) createMutation.mutate();
  }

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Equipment</h1>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
        >
          {showForm ? "Cancel" : "New Equipment"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 grid max-w-2xl grid-cols-2 gap-3 rounded border border-slate-800 bg-slate-900 p-4">
          <input
            value={assetTag}
            onChange={(e) => setAssetTag(e.target.value)}
            placeholder="Asset tag (e.g. SRV-0142)"
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <input
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Hostname (optional)"
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <div className="col-span-2 mt-2 text-xs uppercase tracking-wide text-slate-500">
            Equipment model — select an existing catalog model and revision (new catalog models are created by an
            administrator through the catalog)
          </div>
          <label className="col-span-2 flex flex-col gap-1 text-xs text-slate-400">
            Model
            <select
              aria-label="Equipment model"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Choose a model</option>
              {equipmentModelsQuery.data?.items.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.manufacturer} {model.model_name}
                </option>
              ))}
            </select>
          </label>
          <label className="col-span-2 flex flex-col gap-1 text-xs text-slate-400">
            Revision
            <select
              aria-label="Equipment model revision"
              value={revisionId}
              onChange={(e) => setRevisionId(e.target.value)}
              disabled={!modelId}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none disabled:opacity-50"
            >
              <option value="">{modelId ? "Choose a revision" : "Choose a model first"}</option>
              {equipmentModelRevisionsQuery.data?.items.map((revision) => (
                <option key={revision.id} value={revision.id}>
                  {revision.height_u != null ? `${revision.height_u}U` : "unspecified size"}
                  {revision.width_mm != null && revision.depth_mm != null
                    ? ` · ${revision.width_mm}×${revision.depth_mm}mm`
                    : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="col-span-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create Equipment"}
            </button>
            {createMutation.isError && (
              <p className="mt-2 text-sm text-red-400">{(createMutation.error as Error).message}</p>
            )}
          </div>
        </form>
      )}

      {equipmentQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {equipmentQuery.error && <p className="text-sm text-red-400">{(equipmentQuery.error as Error).message}</p>}

      {equipmentQuery.data && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Hostname</th>
              <th className="pb-2">Asset Tag</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Placement</th>
            </tr>
          </thead>
          <tbody>
            {equipmentQuery.data.items.map((eq) => (
              <tr key={eq.id} className="border-b border-slate-900 hover:bg-slate-900/50">
                <td className="py-2">
                  <Link to={`/equipment/${eq.id}`} className="font-medium text-blue-400 hover:underline">
                    {eq.hostname ?? eq.asset_tag}
                  </Link>
                </td>
                <td className="py-2 font-mono text-slate-300">{eq.asset_tag}</td>
                <td className="py-2">
                  <span className={`rounded px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[eq.lifecycle_status] ?? "bg-slate-700"}`}>
                    {eq.lifecycle_status}
                  </span>
                </td>
                <td className="py-2 text-slate-400">
                  {eq.placement ? (
                    eq.placement.placement_type === "rack_mounted" ? (
                      <>
                        {racksQuery.data?.items.find((r) => r.id === eq.placement!.rack_id)?.name ?? eq.placement.rack_id} — U
                        {eq.placement.u_start}-{eq.placement.u_end} ({eq.placement.side})
                      </>
                    ) : (
                      eq.placement.placement_type
                    )
                  ) : (
                    <span className="italic text-slate-600">unplaced</span>
                  )}
                </td>
              </tr>
            ))}
            {equipmentQuery.data.items.length === 0 && (
              <tr>
                <td colSpan={4} className="py-6 text-center text-slate-500">
                  No equipment yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
