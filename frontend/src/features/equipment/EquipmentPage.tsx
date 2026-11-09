import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { DataTable, EmptyRow, TableError, TableLoading, TD, TH, TR } from "@/components/data/DataTable";
import { useHasPermission } from "@/features/auth/useAuthorization";
import { BulkImportPanel } from "@/features/bulk-import/BulkImportPanel";
import {
  createEquipment,
  createEquipmentModel,
  createEquipmentModelRevision,
  listEquipment,
} from "@/features/equipment/api";
import { LifecycleTransitionAction } from "@/features/managed-assets/LifecycleTransitionAction";
import { listRacks } from "@/features/racks/api";
import { LifecycleStatus } from "@/features/status/StatusBadges";

const EQUIPMENT_PAGE_SIZE = 200;

export function EquipmentPage() {
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(EQUIPMENT_PAGE_SIZE);
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [showBulkImport, setShowBulkImport] = useState(false);
  const [lifecycleNotice, setLifecycleNotice] = useState<string | null>(null);
  const lifecycleNoticeRef = useRef<HTMLParagraphElement>(null);
  const canImport = useHasPermission("equipment:import");

  const equipmentQuery = useQuery({
    queryKey: ["equipment", offset],
    queryFn: () => listEquipment(pageSize, offset),
  });
  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks });

  const [assetTag, setAssetTag] = useState("");
  const [hostname, setHostname] = useState("");
  const [manufacturer, setManufacturer] = useState("");
  const [modelName, setModelName] = useState("");
  const currentPageSize = equipmentQuery.data?.limit && equipmentQuery.data.limit > 0 ? equipmentQuery.data.limit : pageSize;
  const pageOutOfRange = Boolean(equipmentQuery.data && offset > 0 && offset >= equipmentQuery.data.total);

  useEffect(() => {
    if (lifecycleNotice) lifecycleNoticeRef.current?.focus();
  }, [lifecycleNotice]);

  useEffect(() => {
    const page = equipmentQuery.data;
    if (page && page.limit > 0 && page.limit !== pageSize) {
      setPageSize(page.limit);
    }
    if (page && offset > 0 && offset >= page.total) {
      const size = page.limit > 0 ? page.limit : pageSize;
      setOffset(page.total === 0 ? 0 : Math.floor((page.total - 1) / size) * size);
    }
  }, [equipmentQuery.data, offset, pageSize]);

  const createMutation = useMutation({
    mutationFn: async () => {
      const model = await createEquipmentModel(manufacturer.trim(), modelName.trim());
      const revision = await createEquipmentModelRevision(model.id);
      return createEquipment(
        { asset_tag: assetTag.trim(), model_revision_id: revision.id, hostname: hostname.trim() || undefined },
        crypto.randomUUID(),
      );
    },
    onSuccess: () => {
      setAssetTag("");
      setHostname("");
      setShowForm(false);
      queryClient.invalidateQueries({ queryKey: ["equipment"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (assetTag.trim() && manufacturer.trim() && modelName.trim()) createMutation.mutate();
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">Equipment</h1>
        <div className="flex flex-wrap gap-2">
          <Link
            to="/equipment/instantiate"
            className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-100 hover:bg-slate-700"
          >
            Instantiate from catalog
          </Link>
          {canImport && (
            <button
              onClick={() => setShowBulkImport(true)}
              className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-100 hover:bg-slate-700"
            >
              Bulk Import
            </button>
          )}
          <button
            onClick={() => setShowForm((v) => !v)}
            className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
          >
            {showForm ? "Cancel" : "New Equipment"}
          </button>
        </div>
      </div>

      {lifecycleNotice && <p ref={lifecycleNoticeRef} tabIndex={-1} role="status" className="mb-3 text-sm text-green-300">{lifecycleNotice}</p>}

      {showBulkImport && (
        <BulkImportPanel
          resource="equipment"
          resourceLabel="Equipment"
          onClose={() => setShowBulkImport(false)}
          onCommitted={() => queryClient.invalidateQueries({ queryKey: ["equipment"] })}
        />
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 grid max-w-2xl grid-cols-2 gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4">
          <input
            value={assetTag}
            onChange={(e) => setAssetTag(e.target.value)}
            placeholder="Asset tag (e.g. SRV-0142)"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <input
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Hostname (optional)"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <input
            value={manufacturer}
            onChange={(e) => setManufacturer(e.target.value)}
            placeholder="Manufacturer"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="Model name"
            className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <div className="col-span-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create Equipment"}
            </button>
            {createMutation.isError && (
              <p className="mt-2 text-sm text-red-400">{(createMutation.error as Error).message}</p>
            )}
          </div>
        </form>
      )}

      {equipmentQuery.isLoading && <TableLoading label="equipment" columns={4} />}
      {equipmentQuery.error && (
        <TableError error={equipmentQuery.error} what="the equipment list" permission="equipment:read" onRetry={() => equipmentQuery.refetch()} />
      )}

      {equipmentQuery.data && pageOutOfRange && (
        <p role="status" className="text-sm text-fg-secondary">Refreshing the equipment list…</p>
      )}

      {equipmentQuery.data && !pageOutOfRange && (
        <>
          <p className="mb-2 text-xs text-fg-secondary" data-testid="row-count">
            {equipmentQuery.data.total === 0
              ? "0 equipment"
              : `Showing ${offset + 1}-${offset + equipmentQuery.data.items.length} of ${equipmentQuery.data.total} equipment`}
          </p>
          <DataTable label="Equipment">
            <thead>
              <tr className="border-b border-line-subtle bg-surface-panel">
                <th scope="col" className={TH}>Hostname</th>
                <th scope="col" className={TH}>Asset Tag</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={TH}>Placement</th>
              </tr>
            </thead>
            <tbody>
              {equipmentQuery.data.items.map((eq) => (
                <tr key={eq.id} className={TR}>
                  <td className={TD}>
                    <Link to={`/equipment/${eq.id}`} className="font-medium text-link hover:underline">
                      {eq.hostname ?? eq.asset_tag}
                    </Link>
                  </td>
                  <td className={`${TD} font-mono text-fg-secondary`}>{eq.asset_tag}</td>
                  <td className={TD}>
                    <div className="flex flex-wrap items-center gap-2">
                      <LifecycleStatus value={eq.lifecycle_status} />
                    <LifecycleTransitionAction assetId={eq.id} assetName={eq.hostname ?? eq.asset_tag} status={eq.lifecycle_status} assetLabel="equipment" onCompleted={setLifecycleNotice} />
                    </div>
                  </td>
                  <td className={`${TD} text-fg-secondary`}>
                    {eq.placement ? (
                      eq.placement.placement_type === "rack_mounted" ? (
                        <>
                          {racksQuery.data?.items.find((r) => r.id === eq.placement!.rack_id)?.name ?? eq.placement.rack_id} —{" "}
                          <span className="whitespace-nowrap tabular-nums text-fg-primary">
                            U{eq.placement.u_start}-{eq.placement.u_end}
                          </span>{" "}
                          ({eq.placement.side})
                        </>
                      ) : (
                        eq.placement.placement_type
                      )
                    ) : (
                      <span className="italic">unplaced</span>
                    )}
                  </td>
                </tr>
              ))}
              {equipmentQuery.data.items.length === 0 && <EmptyRow columns={4}>No equipment yet.</EmptyRow>}
            </tbody>
          </DataTable>
          {equipmentQuery.data.total > currentPageSize && (
            <nav aria-label="Equipment pages" className="mt-3 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setOffset((current) => Math.max(0, current - currentPageSize))}
                disabled={offset === 0 || equipmentQuery.isFetching}
                className="rounded-sm border border-line-subtle px-3 py-1.5 text-sm text-fg-primary hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                Previous page
              </button>
              <span className="text-xs text-fg-secondary" aria-live="polite">
                Page {Math.floor(offset / currentPageSize) + 1} of {Math.ceil(equipmentQuery.data.total / currentPageSize)}
              </span>
              <button
                type="button"
                onClick={() => setOffset((current) => current + currentPageSize)}
                disabled={offset + currentPageSize >= equipmentQuery.data.total || equipmentQuery.isFetching}
                className="rounded-sm border border-line-subtle px-3 py-1.5 text-sm text-fg-primary hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                Next page
              </button>
            </nav>
          )}
        </>
      )}
    </div>
  );
}
