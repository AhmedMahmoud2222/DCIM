import { Query, useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";

import { useAuth } from "@/features/auth/useAuth";
import { AlarmsPanel } from "@/features/dashboard/AlarmsPanel";
import { CollectorsPanel } from "@/features/dashboard/CollectorsPanel";
import { accessFor, DASHBOARD_PERMISSIONS } from "@/features/dashboard/dashboardModel";
import { ExceptionsPreview } from "@/features/dashboard/ExceptionsPreview";
import { GeographyPanel } from "@/features/dashboard/GeographyPanel";
import { InventoryPanel } from "@/features/dashboard/InventoryPanel";

/** The query keys the dashboard reads. Refresh and "last updated" cover exactly these. */
const isDashboardQuery = (query: Query) => {
  const [head, second] = query.queryKey as unknown[];
  return head === "dashboard" || head === "collectors" || (head === "alarms" && second === "overview");
};

function DashboardHeader() {
  const queryClient = useQueryClient();
  const fetching = useIsFetching({ predicate: isDashboardQuery });
  const cache = queryClient.getQueryCache();
  const subscribe = useCallback((onChange: () => void) => cache.subscribe(onChange), [cache]);
  const updatedAt = useSyncExternalStore(subscribe, () =>
    Math.max(0, ...cache.findAll({ predicate: isDashboardQuery }).map((query) => query.state.dataUpdatedAt)),
  );
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold text-fg-primary">Operations overview</h1>
        <p className="mt-1 text-xs text-fg-secondary" data-testid="dashboard-freshness">
          {updatedAt > 0 ? `Last data received ${new Date(updatedAt).toLocaleTimeString()}.` : "No data received yet."} Each section loads on its own and
          says when it is out of date.
        </p>
      </div>
      <div className="flex items-center gap-2">
        {fetching > 0 && (
          <span role="status" className="text-xs text-fg-secondary">
            Refreshing…
          </span>
        )}
        <button
          type="button"
          onClick={() => void queryClient.refetchQueries({ predicate: isDashboardQuery, type: "active" })}
          disabled={fetching > 0}
          className="min-h-9 rounded-sm border border-line-control bg-surface-panel px-3 py-1.5 text-sm font-medium text-fg-primary hover:bg-surface-hover disabled:opacity-50"
        >
          Refresh
        </button>
      </div>
    </header>
  );
}

/** Control Room dashboard: alarms and geographic coverage side by side on large screens, then the
 * exceptions preview, collector connectivity and inventory. Each section loads, fails and is authorized
 * on its own; a source the user may not read is never requested. */
export function DashboardPage() {
  const { user } = useAuth();
  return (
    <div className="space-y-4">
      <DashboardHeader />
      <div className="grid gap-4 lg:grid-cols-2">
        <AlarmsPanel access={accessFor(user, DASHBOARD_PERMISSIONS.alarms)} />
        <GeographyPanel access={accessFor(user, DASHBOARD_PERMISSIONS.geography)} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ExceptionsPreview access={accessFor(user, DASHBOARD_PERMISSIONS.exceptions)} />
        <CollectorsPanel access={accessFor(user, DASHBOARD_PERMISSIONS.collectors)} />
      </div>
      <InventoryPanel access={accessFor(user, DASHBOARD_PERMISSIONS.inventory)} />
    </div>
  );
}
