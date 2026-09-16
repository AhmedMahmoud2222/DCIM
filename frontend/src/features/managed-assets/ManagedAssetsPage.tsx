import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/apiClient";
import { ManagedAsset, Page } from "@/types";

const STATUS_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function ManagedAssetsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["managed-assets"],
    queryFn: () => apiFetch<Page<ManagedAsset>>("/managed-assets"),
  });

  return (
    <div>
      <h1 className="mb-4 text-lg font-semibold">Managed Assets</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Bare identity/lifecycle anchors only — Phase 1 does not yet attach a Rack, Equipment, or PDU subtype to these
        records (that is Phase 2/3/7). Creation and lifecycle transitions are available through the API; this view
        demonstrates read access only.
      </p>

      {isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {error && <p className="text-sm text-red-400">{(error as Error).message}</p>}

      {data && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Asset Tag</th>
              <th className="pb-2">Type</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Serial</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((asset) => (
              <tr key={asset.id} className="border-b border-slate-900">
                <td className="py-2 font-mono">{asset.asset_tag}</td>
                <td className="py-2">{asset.asset_type}</td>
                <td className="py-2">
                  <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLORS[asset.lifecycle_status] ?? "bg-slate-700"}`}>
                    {asset.lifecycle_status}
                  </span>
                </td>
                <td className="py-2 text-slate-400">{asset.serial_number ?? "—"}</td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={4} className="py-6 text-center text-slate-500">
                  No managed assets yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
