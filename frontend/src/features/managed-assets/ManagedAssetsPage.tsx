import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { EmptyState, PageHeader, StatusBadge } from "@/components/ui/ProductUi";
import { apiFetch } from "@/lib/apiClient";
import { ManagedAsset, Page } from "@/types";

function lifecycleTone(status: string) {
  if (status === "active") return "healthy" as const;
  if (status === "maintenance") return "warning" as const;
  if (status === "removed" || status === "decommissioned") return "critical" as const;
  return "neutral" as const;
}

export function ManagedAssetsPage() {
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  const [lifecycle, setLifecycle] = useState("");
  const { data, isLoading, error } = useQuery({ queryKey: ["managed-assets"], queryFn: () => apiFetch<Page<ManagedAsset>>("/managed-assets?limit=200") });
  const types = useMemo(() => [...new Set(data?.items.map((asset) => asset.asset_type) ?? [])].sort(), [data]);
  const assets = useMemo(() => (data?.items ?? []).filter((asset) => (!type || asset.asset_type === type) && (!lifecycle || asset.lifecycle_status === lifecycle) && `${asset.asset_tag} ${asset.serial_number ?? ""} ${asset.asset_type}`.toLowerCase().includes(search.toLowerCase())), [data, lifecycle, search, type]);

  return <div className="page"><PageHeader eyebrow="Asset inventory" title="Managed assets" description="The identity and lifecycle register for physical and modeled DCIM assets." />
    <section className="surface p-4"><div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_180px_180px]"><input aria-label="Search assets" className="field" placeholder="Search asset tag, serial, or type" value={search} onChange={(event) => setSearch(event.target.value)} /><select aria-label="Filter asset type" className="field" value={type} onChange={(event) => setType(event.target.value)}><option value="">All asset types</option>{types.map((assetType) => <option key={assetType} value={assetType}>{assetType.replace(/_/g, " ")}</option>)}</select><select aria-label="Filter lifecycle" className="field" value={lifecycle} onChange={(event) => setLifecycle(event.target.value)}><option value="">All lifecycle states</option>{["planned", "installed", "active", "maintenance", "decommissioned", "removed"].map((status) => <option key={status} value={status}>{status}</option>)}</select></div></section>
    {isLoading && <div className="surface-muted p-5 text-sm text-slate-400">Loading asset inventory…</div>}{error && <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">{(error as Error).message}</div>}
    {data && <section className="surface overflow-x-auto"><table className="data-table"><thead><tr><th>Asset tag</th><th>Type</th><th>Lifecycle</th><th>Serial</th><th>Registered</th></tr></thead><tbody>{assets.map((asset) => <tr key={asset.id}><td className="font-medium text-slate-200">{asset.asset_tag}</td><td className="capitalize text-slate-400">{asset.asset_type.replace(/_/g, " ")}</td><td><StatusBadge label={asset.lifecycle_status} tone={lifecycleTone(asset.lifecycle_status)} /></td><td className="font-mono text-xs text-slate-400">{asset.serial_number ?? "—"}</td><td className="text-slate-500">{new Date(asset.created_at).toLocaleDateString()}</td></tr>)}</tbody></table>{assets.length === 0 && <div className="p-4"><EmptyState title="No matching assets" detail="Adjust search or lifecycle filters to see registered assets." /></div>}</section>}
  </div>;
}
