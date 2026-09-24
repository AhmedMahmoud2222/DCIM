import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { createCatalogModel, createManufacturer, listCatalogModels, listManufacturers } from "@/features/catalog-designer/api";
import { CATALOG_CATEGORIES } from "@/types";

const STATUS_COLORS: Record<string, string> = {
  active: "bg-green-700 text-green-100",
  deprecated: "bg-yellow-700 text-yellow-100",
  archived: "bg-slate-700 text-slate-300",
};

export function CatalogHomePage() {
  const queryClient = useQueryClient();
  const [manufacturerQuery, setManufacturerQuery] = useState("");
  const [modelQuery, setModelQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [manufacturerFilter, setManufacturerFilter] = useState("");

  const [showManufacturerForm, setShowManufacturerForm] = useState(false);
  const [newManufacturerName, setNewManufacturerName] = useState("");

  const [showModelForm, setShowModelForm] = useState(false);
  const [newModelManufacturerId, setNewModelManufacturerId] = useState("");
  const [newModelCategory, setNewModelCategory] = useState<string>("rack");
  const [newModelName, setNewModelName] = useState("");
  const [newModelNumber, setNewModelNumber] = useState("");

  const manufacturersQuery = useQuery({
    queryKey: ["catalog", "manufacturers", manufacturerQuery],
    queryFn: () => listManufacturers(manufacturerQuery || undefined),
  });
  const modelsQuery = useQuery({
    queryKey: ["catalog", "models", { q: modelQuery, category: categoryFilter, manufacturer_id: manufacturerFilter }],
    queryFn: () =>
      listCatalogModels({
        q: modelQuery || undefined,
        category: categoryFilter || undefined,
        manufacturer_id: manufacturerFilter || undefined,
      }),
  });

  const createManufacturerMutation = useMutation({
    mutationFn: () => createManufacturer(newManufacturerName.trim()),
    onSuccess: () => {
      setNewManufacturerName("");
      setShowManufacturerForm(false);
      queryClient.invalidateQueries({ queryKey: ["catalog", "manufacturers"] });
    },
  });

  const createModelMutation = useMutation({
    mutationFn: () =>
      createCatalogModel({
        manufacturer_id: newModelManufacturerId,
        category: newModelCategory,
        model_name: newModelName.trim(),
        model_number: newModelNumber.trim() || undefined,
      }),
    onSuccess: () => {
      setNewModelName("");
      setNewModelNumber("");
      setShowModelForm(false);
      queryClient.invalidateQueries({ queryKey: ["catalog", "models"] });
    },
  });

  function handleManufacturerSubmit(e: FormEvent) {
    e.preventDefault();
    if (newManufacturerName.trim()) createManufacturerMutation.mutate();
  }

  function handleModelSubmit(e: FormEvent) {
    e.preventDefault();
    if (newModelManufacturerId && newModelName.trim()) createModelMutation.mutate();
  }

  const manufacturerNameById = new Map((manufacturersQuery.data?.items ?? []).map((m) => [m.id, m.name]));

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Asset Catalog</h1>
      </div>

      <section className="mb-8 rounded border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Manufacturers</h2>
          <button
            onClick={() => setShowManufacturerForm((v) => !v)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
          >
            {showManufacturerForm ? "Cancel" : "New Manufacturer"}
          </button>
        </div>

        {showManufacturerForm && (
          <form onSubmit={handleManufacturerSubmit} className="mb-4 flex gap-2">
            <input
              value={newManufacturerName}
              onChange={(e) => setNewManufacturerName(e.target.value)}
              placeholder="Manufacturer name"
              className="flex-1 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            />
            <button
              type="submit"
              disabled={createManufacturerMutation.isPending}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createManufacturerMutation.isPending ? "Creating…" : "Create"}
            </button>
          </form>
        )}
        {createManufacturerMutation.isError && (
          <p className="mb-3 text-sm text-red-400">{(createManufacturerMutation.error as Error).message}</p>
        )}

        <input
          value={manufacturerQuery}
          onChange={(e) => setManufacturerQuery(e.target.value)}
          placeholder="Search manufacturers…"
          className="mb-3 w-full max-w-sm rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
        />

        {manufacturersQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {manufacturersQuery.error && <p className="text-sm text-red-400">{(manufacturersQuery.error as Error).message}</p>}
        {manufacturersQuery.data && (
          <ul className="flex flex-wrap gap-2">
            {manufacturersQuery.data.items.map((m) => (
              <li key={m.id}>
                <Link
                  to={`/admin/catalog/manufacturers/${m.id}`}
                  className="inline-block rounded bg-slate-800 px-3 py-1 text-sm text-slate-200 hover:bg-slate-700"
                >
                  {m.name}
                </Link>
              </li>
            ))}
            {manufacturersQuery.data.items.length === 0 && <li className="text-sm italic text-slate-500">No manufacturers yet.</li>}
          </ul>
        )}
      </section>

      <section className="rounded border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Models</h2>
          <button
            onClick={() => setShowModelForm((v) => !v)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
          >
            {showModelForm ? "Cancel" : "New Model"}
          </button>
        </div>

        {showModelForm && (
          <form onSubmit={handleModelSubmit} className="mb-4 grid max-w-2xl grid-cols-2 gap-3 rounded border border-slate-800 bg-slate-950 p-4">
            <select
              value={newModelManufacturerId}
              onChange={(e) => setNewModelManufacturerId(e.target.value)}
              className="col-span-2 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="">Select manufacturer…</option>
              {manufacturersQuery.data?.items.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
            <select
              value={newModelCategory}
              onChange={(e) => setNewModelCategory(e.target.value)}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              {CATALOG_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <input
              value={newModelNumber}
              onChange={(e) => setNewModelNumber(e.target.value)}
              placeholder="Model number (optional)"
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            />
            <input
              value={newModelName}
              onChange={(e) => setNewModelName(e.target.value)}
              placeholder="Model name"
              className="col-span-2 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
            />
            <div className="col-span-2">
              <button
                type="submit"
                disabled={createModelMutation.isPending}
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createModelMutation.isPending ? "Creating…" : "Create Model"}
              </button>
              {createModelMutation.isError && <p className="mt-2 text-sm text-red-400">{(createModelMutation.error as Error).message}</p>}
            </div>
          </form>
        )}

        <div className="mb-3 flex flex-wrap gap-2">
          <input
            value={modelQuery}
            onChange={(e) => setModelQuery(e.target.value)}
            placeholder="Search models…"
            className="flex-1 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          />
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          >
            <option value="">All categories</option>
            {CATALOG_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select
            value={manufacturerFilter}
            onChange={(e) => setManufacturerFilter(e.target.value)}
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
          >
            <option value="">All manufacturers</option>
            {manufacturersQuery.data?.items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </div>

        {modelsQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {modelsQuery.error && <p className="text-sm text-red-400">{(modelsQuery.error as Error).message}</p>}
        {modelsQuery.data && (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-slate-400">
                <th className="pb-2">Model</th>
                <th className="pb-2">Manufacturer</th>
                <th className="pb-2">Category</th>
                <th className="pb-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {modelsQuery.data.items.map((model) => (
                <tr key={model.id} className="border-b border-slate-900 hover:bg-slate-900/50">
                  <td className="py-2">
                    <Link to={`/admin/catalog/models/${model.id}`} className="font-medium text-blue-400 hover:underline">
                      {model.model_name}
                    </Link>
                    {model.model_number && <span className="ml-2 font-mono text-xs text-slate-500">{model.model_number}</span>}
                  </td>
                  <td className="py-2 text-slate-300">{manufacturerNameById.get(model.manufacturer_id) ?? model.manufacturer_id}</td>
                  <td className="py-2 text-slate-400">{model.category}</td>
                  <td className="py-2">
                    <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLORS[model.status] ?? "bg-slate-700"}`}>{model.status}</span>
                  </td>
                </tr>
              ))}
              {modelsQuery.data.items.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-6 text-center text-slate-500">
                    No models yet. Create one to get started.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
