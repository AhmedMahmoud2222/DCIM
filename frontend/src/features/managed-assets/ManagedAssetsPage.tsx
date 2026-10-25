import { useQuery } from "@tanstack/react-query";

import { DataTable, EmptyRow, RowCount, TD, TH, TR } from "@/components/data/DataTable";
import { QueryStatus } from "@/components/data/QueryStatus";
import { PageHeader } from "@/components/layout/PageHeader";
import { LifecycleStatus } from "@/features/status/StatusBadges";
import { apiFetch } from "@/lib/apiClient";
import { ManagedAsset, Page } from "@/types";

export function ManagedAssetsPage() {
  const query = useQuery({
    queryKey: ["managed-assets"],
    queryFn: () => apiFetch<Page<ManagedAsset>>("/managed-assets"),
  });
  const data = query.data;

  return (
    <div>
      <PageHeader
        title="Managed Assets"
        description="Bare identity/lifecycle anchors only — Phase 1 does not yet attach a Rack, Equipment, or PDU subtype to these records (that is Phase 2/3/7). Creation and lifecycle transitions are available through the API; this view demonstrates read access only."
      />

      <QueryStatus query={query} what="managed assets" permission="managed_asset:read" />

      {data && (
        <>
          <RowCount shown={data.items.length} total={data.total} noun="managed assets" />
          <DataTable label="Managed assets" minWidthClass="min-w-[32rem]">
            <thead>
              <tr className="border-b border-line-subtle bg-surface-panel">
                <th scope="col" className={TH}>Asset Tag</th>
                <th scope="col" className={TH}>Type</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={TH}>Serial</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((asset) => (
                <tr key={asset.id} className={TR}>
                  <td className={`${TD} font-mono`}>{asset.asset_tag}</td>
                  <td className={TD}>{asset.asset_type}</td>
                  <td className={TD}>
                    <LifecycleStatus value={asset.lifecycle_status} />
                  </td>
                  <td className={`${TD} text-fg-secondary`}>{asset.serial_number ?? "—"}</td>
                </tr>
              ))}
              {data.items.length === 0 && <EmptyRow columns={4}>No managed assets yet.</EmptyRow>}
            </tbody>
          </DataTable>
        </>
      )}
    </div>
  );
}
