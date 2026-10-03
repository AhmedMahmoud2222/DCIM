import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { getManufacturer, listCatalogModels } from "@/features/catalog-designer/api";

export function ManufacturerDetailPage() {
  const { manufacturerId } = useParams<{ manufacturerId: string }>();

  const manufacturerQuery = useQuery({
    queryKey: ["catalog", "manufacturers", manufacturerId],
    queryFn: () => getManufacturer(manufacturerId!),
    enabled: !!manufacturerId,
  });
  const modelsQuery = useQuery({
    queryKey: ["catalog", "models", { manufacturer_id: manufacturerId }],
    queryFn: () => listCatalogModels({ manufacturer_id: manufacturerId }),
    enabled: !!manufacturerId,
  });

  if (manufacturerQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (manufacturerQuery.error) return <p className="text-sm text-red-400">{(manufacturerQuery.error as Error).message}</p>;
  const manufacturer = manufacturerQuery.data;
  if (!manufacturer) return null;

  return (
    <div>
      <Link to="/admin/catalog" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Catalog
      </Link>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-lg font-semibold">{manufacturer.name}</h1>
        <span className="rounded-sm bg-slate-700 px-2 py-0.5 text-xs text-slate-300">{manufacturer.status}</span>
      </div>

      <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Models</h2>
        {modelsQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {modelsQuery.error && <p className="text-sm text-red-400">{(modelsQuery.error as Error).message}</p>}
        {modelsQuery.data && (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-slate-400">
                <th className="pb-2">Model</th>
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
                  <td className="py-2 text-slate-400">{model.category}</td>
                  <td className="py-2 text-slate-300">{model.status}</td>
                </tr>
              ))}
              {modelsQuery.data.items.length === 0 && (
                <tr>
                  <td colSpan={3} className="py-6 text-center text-slate-500">
                    No models from this manufacturer yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
