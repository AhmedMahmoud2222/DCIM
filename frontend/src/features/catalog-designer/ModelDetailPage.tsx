import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useIsCatalogAdministrator } from "@/features/auth/useAuthorization";
import { cloneRevision, createDraftRevision, getCatalogModel, updateCatalogModelMetadata } from "@/features/catalog-designer/api";

const LIFECYCLE_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-200",
  published: "bg-green-700 text-green-100",
  retired: "bg-red-900 text-red-100",
};

export function ModelDetailPage() {
  const { modelId } = useParams<{ modelId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isCatalogAdministrator = useIsCatalogAdministrator();
  const [editingMetadata, setEditingMetadata] = useState(false);
  const [description, setDescription] = useState("");
  const [tagsInput, setTagsInput] = useState("");

  const modelQuery = useQuery({
    queryKey: ["catalog", "models", modelId],
    queryFn: () => getCatalogModel(modelId!),
    enabled: !!modelId,
  });

  const updateMetadataMutation = useMutation({
    mutationFn: () =>
      updateCatalogModelMetadata(modelId!, {
        description: description.trim() || null,
        tags: tagsInput
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      setEditingMetadata(false);
      queryClient.invalidateQueries({ queryKey: ["catalog", "models", modelId] });
    },
  });

  const createDraftMutation = useMutation({
    mutationFn: () => createDraftRevision(modelId!),
    onSuccess: (revision) => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "models", modelId] });
      navigate(`/admin/catalog/revisions/${revision.id}`);
    },
  });

  const cloneMutation = useMutation({
    mutationFn: (fromRevisionId: string) => cloneRevision(modelId!, fromRevisionId),
    onSuccess: (revision) => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "models", modelId] });
      navigate(`/admin/catalog/revisions/${revision.id}`);
    },
  });

  function startEditingMetadata() {
    if (!modelQuery.data) return;
    setDescription(modelQuery.data.description ?? "");
    setTagsInput(modelQuery.data.tags.join(", "));
    setEditingMetadata(true);
  }

  function handleMetadataSubmit(e: FormEvent) {
    e.preventDefault();
    updateMetadataMutation.mutate();
  }

  if (modelQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (modelQuery.error) return <p className="text-sm text-red-400">{(modelQuery.error as Error).message}</p>;
  const model = modelQuery.data;
  if (!model) return null;

  return (
    <div>
      <Link to="/admin/catalog" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Catalog
      </Link>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-lg font-semibold">{model.model_name}</h1>
          <p className="text-sm text-slate-400">
            {model.category}
            {model.subtype ? ` / ${model.subtype}` : ""}
            {model.model_number ? ` · ${model.model_number}` : ""}
          </p>
        </div>
        <span className="rounded-sm bg-slate-700 px-2 py-0.5 text-xs text-slate-300">{model.status}</span>
      </div>

      <div className="mb-6 rounded-sm border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Metadata</h2>
          {isCatalogAdministrator && !editingMetadata && (
            <button onClick={startEditingMetadata} className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700">
              Edit
            </button>
          )}
        </div>
        {isCatalogAdministrator && editingMetadata ? (
          <form onSubmit={handleMetadataSubmit} className="space-y-2">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Description"
              rows={3}
              className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
            />
            <input
              value={tagsInput}
              onChange={(e) => setTagsInput(e.target.value)}
              placeholder="Tags (comma-separated)"
              className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
            />
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={updateMetadataMutation.isPending}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => setEditingMetadata(false)}
                className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
              >
                Cancel
              </button>
            </div>
            {updateMetadataMutation.isError && <p className="text-sm text-red-400">{(updateMetadataMutation.error as Error).message}</p>}
          </form>
        ) : (
          <div className="text-sm">
            <p className="mb-2 text-slate-300">{model.description || <span className="italic text-slate-500">No description.</span>}</p>
            <div className="flex flex-wrap gap-1">
              {model.tags.map((tag) => (
                <span key={tag} className="rounded-sm bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
                  {tag}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Revisions</h2>
          {isCatalogAdministrator && (
            <button
              onClick={() => createDraftMutation.mutate()}
              disabled={createDraftMutation.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createDraftMutation.isPending ? "Creating…" : "New Draft"}
            </button>
          )}
        </div>
        {createDraftMutation.isError && <p className="mb-3 text-sm text-red-400">{(createDraftMutation.error as Error).message}</p>}
        {cloneMutation.isError && <p className="mb-3 text-sm text-red-400">{(cloneMutation.error as Error).message}</p>}

        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Revision</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Published</th>
              <th className="pb-2">Retired</th>
              <th className="pb-2"></th>
            </tr>
          </thead>
          <tbody>
            {model.revisions.map((revision) => (
              <tr key={revision.id} className="border-b border-slate-900 hover:bg-slate-900/50">
                <td className="py-2">
                  <Link to={`/admin/catalog/revisions/${revision.id}`} className="font-medium text-blue-400 hover:underline">
                    Rev {revision.revision_number}
                  </Link>
                </td>
                <td className="py-2">
                  <span className={`rounded-sm px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[revision.lifecycle_status] ?? "bg-slate-700"}`}>
                    {revision.lifecycle_status}
                  </span>
                </td>
                <td className="py-2 text-slate-400">{revision.published_at ? new Date(revision.published_at).toLocaleDateString() : "—"}</td>
                <td className="py-2 text-slate-400">{revision.retired_at ? new Date(revision.retired_at).toLocaleDateString() : "—"}</td>
                <td className="py-2 text-right">
                  {isCatalogAdministrator && revision.lifecycle_status !== "draft" && (
                    <button
                      onClick={() => cloneMutation.mutate(revision.id)}
                      disabled={cloneMutation.isPending}
                      className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
                    >
                      Clone
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {model.revisions.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-center text-slate-500">
                  No revisions yet. Create a draft to get started.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
