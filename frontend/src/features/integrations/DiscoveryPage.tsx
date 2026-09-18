import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { acceptReconciliation, listDiscoveredDevices, listReconciliationDiffs, rejectReconciliation } from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";

const STATUS_COLORS: Record<string, string> = {
  new: "bg-amber-900 text-amber-200",
  reconciled: "bg-green-900 text-green-200",
  ignored: "bg-slate-700 text-slate-300",
};

export function DiscoveryPage() {
  const queryClient = useQueryClient();
  const [assetIdByDiff, setAssetIdByDiff] = useState<Record<string, string>>({});

  const devicesQuery = useQuery({ queryKey: ["discovery", "devices"], queryFn: listDiscoveredDevices });
  const diffsQuery = useQuery({
    queryKey: ["discovery", "reconciliation", "pending"],
    queryFn: () => listReconciliationDiffs("pending"),
  });

  const acceptMutation = useMutation({
    mutationFn: ({ diffId, assetId }: { diffId: string; assetId: string }) => acceptReconciliation(diffId, assetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discovery"] });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (diffId: string) => rejectReconciliation(diffId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discovery"] });
    },
  });

  const devicesById = new Map((devicesQuery.data ?? []).map((d) => [d.id, d]));

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Discovery &amp; Reconciliation</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Devices a collector observes but does not yet correspond to authoritative inventory. Discovery never creates or
        modifies inventory itself — a human decides whether a discovered device matches an existing asset (Phase 8 §7).
      </p>

      <div className="grid grid-cols-2 gap-6">
        <div>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">Pending Reconciliation</h2>
          <div className="space-y-2">
            {diffsQuery.data?.map((diff) => {
              const device = devicesById.get(diff.discovered_device_id);
              return (
                <div key={diff.id} className="rounded border border-slate-800 bg-slate-900 p-3 text-xs">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-mono text-slate-300">{device?.external_identifier ?? diff.discovered_device_id}</span>
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">{diff.diff_type}</span>
                  </div>
                  <form
                    onSubmit={(e: FormEvent) => {
                      e.preventDefault();
                      const assetId = assetIdByDiff[diff.id];
                      if (assetId) acceptMutation.mutate({ diffId: diff.id, assetId });
                    }}
                    className="flex gap-1"
                  >
                    <input
                      value={assetIdByDiff[diff.id] ?? ""}
                      onChange={(e) => setAssetIdByDiff((m) => ({ ...m, [diff.id]: e.target.value }))}
                      placeholder="Existing ManagedAsset ID to link"
                      className="flex-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-[10px] text-slate-100"
                    />
                    <button
                      type="submit"
                      disabled={acceptMutation.isPending}
                      className="rounded bg-blue-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      onClick={() => rejectMutation.mutate(diff.id)}
                      disabled={rejectMutation.isPending}
                      className="rounded bg-slate-700 px-2 py-1 text-[10px] font-medium text-slate-100 hover:bg-slate-600 disabled:opacity-50"
                    >
                      Reject
                    </button>
                  </form>
                  {acceptMutation.isError && (
                    <p className="mt-1 text-red-400">
                      {acceptMutation.error instanceof ApiError ? acceptMutation.error.detail : (acceptMutation.error as Error).message}
                    </p>
                  )}
                </div>
              );
            })}
            {diffsQuery.data?.length === 0 && <p className="text-xs italic text-slate-500">Nothing pending reconciliation.</p>}
          </div>
        </div>

        <div>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">All Discovered Devices</h2>
          <div className="overflow-x-auto rounded border border-slate-800 bg-slate-900">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="p-2">External ID</th>
                  <th className="p-2">Status</th>
                  <th className="p-2">Matched asset</th>
                </tr>
              </thead>
              <tbody>
                {devicesQuery.data?.map((d) => (
                  <tr key={d.id} className="border-b border-slate-800/50">
                    <td className="p-2 font-mono">{d.external_identifier}</td>
                    <td className="p-2">
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS_COLORS[d.status] ?? "bg-slate-700"}`}>{d.status}</span>
                    </td>
                    <td className="p-2">{d.matched_managed_asset_id ?? "—"}</td>
                  </tr>
                ))}
                {devicesQuery.data?.length === 0 && (
                  <tr>
                    <td colSpan={3} className="p-4 text-center italic text-slate-500">
                      No devices discovered yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
