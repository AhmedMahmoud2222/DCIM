import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Access, collectorsNeedingAttention, connectivityCounts, DASHBOARD_PERMISSIONS } from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { listCollectors } from "@/features/integrations/api";
import { formatAge } from "@/features/status/freshness";
import { CollectorConnectivity } from "@/features/status/StatusBadges";

/** How many collectors needing attention are listed; the rest are on the Collectors page. */
const LIST_LIMIT = 5;

function Count({ value }: { value: number }) {
  return <span className="font-semibold tabular-nums text-fg-primary">{value}</span>;
}

/** The backend's heartbeat-based classification. It is connectivity, never measurement freshness. */
export function CollectorsPanel({ access, className = "" }: { access: Access; className?: string }) {
  const query = useQuery({ queryKey: ["collectors"], queryFn: listCollectors, enabled: access === "granted" });
  return (
    <DashboardSection
      id="collectors"
      title="Collector connectivity"
      what="collector connectivity"
      permission={DASHBOARD_PERMISSIONS.collectors}
      access={access}
      query={query}
      className={className}
      emptyText="No collectors are registered."
      aside={(collectors) => (
        <>
          <Count value={collectors.length} /> registered
        </>
      )}
    >
      {(collectors) => {
        const counts = connectivityCounts(collectors);
        const attention = collectorsNeedingAttention(collectors);
        return (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
              <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="offline" /> <Count value={counts.offline} /></span>
              <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="stale" /> <Count value={counts.stale} /></span>
              <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="healthy" /> <Count value={counts.healthy} /></span>
              {counts.unrecognised > 0 && (
                <span className="inline-flex items-center gap-1.5 text-fg-secondary">Other classification <Count value={counts.unrecognised} /></span>
              )}
            </div>
            {attention.length === 0 ? (
              <p className="text-sm text-fg-secondary">Every registered collector reports a Healthy heartbeat.</p>
            ) : (
              <ul className="space-y-2">
                {attention.slice(0, LIST_LIMIT).map((collector) => (
                  <li key={collector.id} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-sm bg-surface-inset px-3 py-2 text-sm">
                    <div className="min-w-0">
                      <span className="mr-2"><CollectorConnectivity health={collector.health} /></span>
                      <span className="text-fg-primary">{collector.name}</span>
                      <span className="ml-1 text-fg-secondary">({collector.collector_type})</span>
                    </div>
                    <span className="text-xs text-fg-secondary">
                      {collector.seconds_since_heartbeat == null
                        ? "no heartbeat recorded"
                        : `last heartbeat ${formatAge(Math.round(collector.seconds_since_heartbeat))} ago`}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {attention.length > LIST_LIMIT && (
              <p className="mt-2 text-xs text-fg-secondary">
                Showing {LIST_LIMIT} of {attention.length} collectors that need attention.
              </p>
            )}
            <p className="mt-3 text-xs text-fg-secondary">
              Connectivity comes from collector heartbeats. It does not say how fresh any measurement is.{" "}
              <Link className="text-link underline underline-offset-2" to="/collectors">
                Open Collectors →
              </Link>{" "}
              <Link className="text-link underline underline-offset-2" to="/integrations">
                Open Integrations →
              </Link>
            </p>
          </>
        );
      }}
    </DashboardSection>
  );
}
