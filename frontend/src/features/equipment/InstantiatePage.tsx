import { useMutation, useQuery } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useIsCatalogAdministrator } from "@/features/auth/useAuthorization";
import { getCatalogModel, listCatalogModels } from "@/features/catalog-designer/api";
import { instantiateEquipment } from "@/features/equipment/api";
import { listRacks, listRooms } from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";
import { PlacementType, Side } from "@/types";

/** Phase 10B: instantiate a physical Equipment (and its ports/power inlets) from a
 * published catalog model, optionally placing it directly into a rack slot in the same
 * request. Gated the same way the backend gates POST /equipment/instantiate —
 * `equipment:manage` — not by catalog administrator status: instantiating equipment is an
 * inventory action, not a catalog-authoring one (that distinction is why this page reads
 * catalog models for selection but never edits them). */
export function InstantiatePage() {
  const navigate = useNavigate();
  const isCatalogAdministrator = useIsCatalogAdministrator();

  const [modelId, setModelId] = useState("");
  const [revisionId, setRevisionId] = useState("");
  const [assetTag, setAssetTag] = useState("");
  const [hostname, setHostname] = useState("");
  const [wantsPlacement, setWantsPlacement] = useState(false);
  const [roomId, setRoomId] = useState("");
  const [rackId, setRackId] = useState("");
  const [uStart, setUStart] = useState("");
  const [uEnd, setUEnd] = useState("");
  const [side, setSide] = useState<Side>("front");

  const modelsQuery = useQuery({
    queryKey: ["catalog", "models", { category: "equipment" }],
    queryFn: () => listCatalogModels({ category: "equipment", status: "active" }),
  });
  const modelDetailQuery = useQuery({
    queryKey: ["catalog", "models", modelId],
    queryFn: () => getCatalogModel(modelId),
    enabled: !!modelId,
  });
  const publishedRevisions = modelDetailQuery.data?.revisions.filter((r) => r.lifecycle_status === "published") ?? [];

  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms, enabled: wantsPlacement });
  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks, enabled: wantsPlacement });

  const instantiateMutation = useMutation({
    mutationFn: () =>
      instantiateEquipment(
        {
          asset_tag: assetTag.trim(),
          catalog_model_revision_id: revisionId,
          hostname: hostname.trim() || undefined,
          ...(wantsPlacement
            ? {
                placement_type: "rack_mounted" as PlacementType,
                room_id: roomId,
                rack_id: rackId,
                u_start: Number(uStart),
                u_end: Number(uEnd),
                side,
              }
            : {}),
        },
        crypto.randomUUID(),
      ),
    onSuccess: (equipment) => navigate(`/equipment/${equipment.id}`),
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!assetTag.trim() || !revisionId) return;
    if (wantsPlacement && (!roomId || !rackId || !uStart || !uEnd)) return;
    instantiateMutation.mutate();
  }

  return (
    <div className="max-w-2xl">
      <h1 className="mb-1 text-lg font-semibold">Instantiate equipment from catalog</h1>
      <p className="mb-4 text-sm text-slate-400">
        Creates a physical Equipment record from a published catalog model, plus its network ports and power inlets.
        {isCatalogAdministrator && (
          <> Need a different model published first? <a href="/admin/catalog" className="text-blue-400 hover:underline">Open the catalog designer →</a></>
        )}
      </p>

      <form onSubmit={handleSubmit} className="space-y-3 rounded border border-slate-800 bg-slate-900 p-4">
        <select
          data-testid="instantiate-model-select"
          value={modelId}
          onChange={(e) => {
            setModelId(e.target.value);
            setRevisionId("");
          }}
          className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
        >
          <option value="">Select a catalog model…</option>
          {modelsQuery.data?.items.map((m) => (
            <option key={m.id} value={m.id}>
              {m.model_name} {m.model_number ? `(${m.model_number})` : ""}
            </option>
          ))}
        </select>

        {modelId && (
          <select
            data-testid="instantiate-revision-select"
            value={revisionId}
            onChange={(e) => setRevisionId(e.target.value)}
            className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
          >
            <option value="">Select a published revision…</option>
            {publishedRevisions.map((r) => (
              <option key={r.id} value={r.id}>
                Revision {r.revision_number} (published {r.published_at ? new Date(r.published_at).toLocaleDateString() : ""})
              </option>
            ))}
          </select>
        )}
        {modelId && !modelDetailQuery.isLoading && publishedRevisions.length === 0 && (
          <p className="text-xs italic text-yellow-400">This model has no published revision yet.</p>
        )}

        <input
          data-testid="instantiate-asset-tag"
          value={assetTag}
          onChange={(e) => setAssetTag(e.target.value)}
          placeholder="Asset tag (e.g. SRV-0142)"
          className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
        />
        <input
          data-testid="instantiate-hostname"
          value={hostname}
          onChange={(e) => setHostname(e.target.value)}
          placeholder="Hostname (optional)"
          className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
        />

        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            data-testid="instantiate-wants-placement"
            type="checkbox"
            checked={wantsPlacement}
            onChange={(e) => setWantsPlacement(e.target.checked)}
          />
          Place into a rack now
        </label>

        {wantsPlacement && (
          <div className="space-y-2 border-t border-slate-800 pt-3">
            <select
              data-testid="instantiate-room-select"
              value={roomId}
              onChange={(e) => setRoomId(e.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
            >
              <option value="">Select room…</option>
              {roomsQuery.data?.items.map((room) => (
                <option key={room.id} value={room.id}>{room.name} ({room.code})</option>
              ))}
            </select>
            <select
              data-testid="instantiate-rack-select"
              value={rackId}
              onChange={(e) => setRackId(e.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
            >
              <option value="">Select rack…</option>
              {racksQuery.data?.items.map((rack) => (
                <option key={rack.id} value={rack.id}>{rack.name}</option>
              ))}
            </select>
            <div className="flex gap-2">
              <input
                data-testid="instantiate-u-start"
                type="number" min={1} value={uStart} onChange={(e) => setUStart(e.target.value)} placeholder="U start"
                className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="instantiate-u-end"
                type="number" min={2} value={uEnd} onChange={(e) => setUEnd(e.target.value)} placeholder="U end (exclusive)"
                className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <select
                data-testid="instantiate-side-select"
                value={side} onChange={(e) => setSide(e.target.value as Side)}
                className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="front">front</option>
                <option value="rear">rear</option>
                <option value="both">both</option>
              </select>
            </div>
          </div>
        )}

        <button
          data-testid="instantiate-submit"
          type="submit"
          disabled={instantiateMutation.isPending}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {instantiateMutation.isPending ? "Instantiating…" : "Instantiate"}
        </button>
        {instantiateMutation.isError && (
          <p className="text-sm text-red-400">
            {instantiateMutation.error instanceof ApiError ? instantiateMutation.error.detail : (instantiateMutation.error as Error).message}
          </p>
        )}
      </form>
    </div>
  );
}
