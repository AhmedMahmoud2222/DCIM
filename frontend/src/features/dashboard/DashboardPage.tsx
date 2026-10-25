import { useQuery } from "@tanstack/react-query";
import { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";
import {
  accessFor,
  ALARM_LIST_LIMIT,
  collectorsNeedingAttention,
  connectivityCounts,
  countLabel,
  DASHBOARD_PERMISSIONS,
  severityCounts,
  sortAlarmsNewestFirst,
  sortExceptionsBySeverity,
} from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { listCollectors } from "@/features/integrations/api";
import { getDashboardExceptions, getDashboardSummary } from "@/features/power/api";
import { formatAge } from "@/features/status/freshness";
import { AlarmWorkflowStatus, CollectorConnectivity, ExceptionSeverity } from "@/features/status/StatusBadges";
import { getOpenAlarms } from "@/features/telemetry/api";

/** How many alarms and exceptions are listed in full; the rest are reached through their own pages. */
const LIST_LIMIT = 8;

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="h-full rounded-sm border border-line-subtle bg-surface-page p-3">
      <p className="text-xs uppercase tracking-wide text-fg-secondary">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-fg-primary">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-fg-secondary">{sub}</p>}
    </div>
  );
}

function Count({ children }: { children: ReactNode }) {
  return <span className="font-semibold tabular-nums text-fg-primary">{children}</span>;
}

const rowClass = "flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-sm bg-white/5 px-3 py-2 text-sm";

export function DashboardPage() {
  const { user } = useAuth();

  const alarmsAccess = accessFor(user, DASHBOARD_PERMISSIONS.alarms);
  const exceptionsAccess = accessFor(user, DASHBOARD_PERMISSIONS.exceptions);
  const collectorsAccess = accessFor(user, DASHBOARD_PERMISSIONS.collectors);
  const inventoryAccess = accessFor(user, DASHBOARD_PERMISSIONS.inventory);

  // A source the user is known not to be allowed to read is never requested.
  const activeAlarmsQuery = useQuery({
    queryKey: ["alarms", "active"],
    queryFn: () => getOpenAlarms("ACTIVE", ALARM_LIST_LIMIT),
    enabled: alarmsAccess === "granted",
  });
  const acknowledgedAlarmsQuery = useQuery({
    queryKey: ["alarms", "acknowledged"],
    queryFn: () => getOpenAlarms("ACKNOWLEDGED", ALARM_LIST_LIMIT),
    enabled: alarmsAccess === "granted",
  });
  const exceptionsQuery = useQuery({
    queryKey: ["dashboard", "exceptions"],
    queryFn: getDashboardExceptions,
    enabled: exceptionsAccess === "granted",
  });
  const collectorsQuery = useQuery({
    queryKey: ["collectors"],
    queryFn: listCollectors,
    enabled: collectorsAccess === "granted",
  });
  const summaryQuery = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: () => getDashboardSummary(),
    enabled: inventoryAccess === "granted",
  });

  const ack = acknowledgedAlarmsQuery;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold text-fg-primary">Operations overview</h1>
        <p className="mt-1 max-w-3xl text-sm text-fg-secondary">
          Welcome, {user?.full_name}. What needs attention comes first. Alarm, exception and collector counts are the number of records
          the API returned; a section that could not be loaded or is not permitted says so instead of showing zero.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* 1. Urgency: alarms that are open now. */}
        <DashboardSection
          id="alarms"
          title="Active alarms"
          what="active alarms"
          permission={DASHBOARD_PERMISSIONS.alarms}
          access={alarmsAccess}
          query={activeAlarmsQuery}
          emptyText="No active alarms."
          className="lg:col-span-2"
          aside={(alarms) => (
            <>
              <AlarmWorkflowStatus status="ACTIVE" size="sm" />
              <Count>{countLabel(alarms.length, ALARM_LIST_LIMIT)}</Count>
            </>
          )}
        >
          {(alarms) => {
            const sorted = sortAlarmsNewestFirst(alarms);
            return (
              <>
                <ul className="space-y-2">
                  {sorted.slice(0, LIST_LIMIT).map((alarm) => (
                    <li key={alarm.id} className={rowClass}>
                      <div className="min-w-0">
                        <span className="mr-2">
                          <AlarmWorkflowStatus status={alarm.status} size="sm" />
                        </span>
                        <span>
                          {alarm.subject_key} · {alarm.presentation_value}
                          {alarm.presentation_unit === "%" ? "%" : ` ${alarm.presentation_unit ?? ""}`}
                        </span>
                        <p className="mt-1 text-xs text-fg-secondary">Occurred {new Date(alarm.opened_at).toLocaleString()}</p>
                      </div>
                      {alarm.managed_asset_id && (
                        <Link className="text-xs text-link hover:underline" to={`/equipment/${alarm.managed_asset_id}`}>
                          Equipment →
                        </Link>
                      )}
                    </li>
                  ))}
                </ul>
                <p className="mt-3 text-xs text-fg-secondary">
                  {sorted.length > LIST_LIMIT ? `Showing the newest ${LIST_LIMIT} of ${countLabel(sorted.length, ALARM_LIST_LIMIT)}. ` : ""}
                  {ack.isLoading && "Acknowledged alarms: loading… "}
                  {!ack.isLoading && ack.error ? "The acknowledged count could not be loaded. " : null}
                  {!ack.isLoading && !ack.error && ack.data ? (
                    <>
                      <Count>{countLabel(ack.data.length, ALARM_LIST_LIMIT)}</Count> acknowledged, awaiting clearance.{" "}
                    </>
                  ) : null}
                  <Link className="text-link hover:underline" to="/events">
                    Open Events →
                  </Link>
                </p>
              </>
            );
          }}
        </DashboardSection>

        {/* 2. Operational exceptions, ordered by the severity the backend supplied. */}
        <DashboardSection
          id="exceptions"
          title="Infrastructure exceptions"
          what="infrastructure exceptions"
          permission={DASHBOARD_PERMISSIONS.exceptions}
          access={exceptionsAccess}
          query={exceptionsQuery}
          emptyText="No capacity or topology exceptions detected."
          aside={(items) => {
            const counts = severityCounts(items);
            return (
              <>
                {counts.critical > 0 && (
                  <span className="inline-flex items-center gap-1">
                    <ExceptionSeverity severity="critical" size="sm" /> <Count>{counts.critical}</Count>
                  </span>
                )}
                {counts.warning > 0 && (
                  <span className="inline-flex items-center gap-1">
                    <ExceptionSeverity severity="warning" size="sm" /> <Count>{counts.warning}</Count>
                  </span>
                )}
                {counts.info > 0 && (
                  <span className="inline-flex items-center gap-1">
                    <ExceptionSeverity severity="info" size="sm" /> <Count>{counts.info}</Count>
                  </span>
                )}
                {counts.unrecognised > 0 && (
                  <span className="inline-flex items-center gap-1">
                    <ExceptionSeverity severity={null} size="sm" /> <Count>{counts.unrecognised}</Count>
                  </span>
                )}
              </>
            );
          }}
        >
          {(items) => {
            const sorted = sortExceptionsBySeverity(items);
            return (
              <>
                <ul className="space-y-2">
                  {sorted.slice(0, LIST_LIMIT).map((exc, i) => (
                    <li key={`${exc.code}-${exc.object_id}-${i}`} className={rowClass}>
                      <div className="min-w-0">
                        <span className="mr-2">
                          <ExceptionSeverity severity={exc.severity} size="sm" />
                        </span>
                        <span className="mr-2 rounded-sm bg-tone-neutral-bg px-1.5 py-0.5 font-mono text-[10px] text-tone-neutral-fg">{exc.code}</span>
                        <span className="text-fg-secondary">{exc.message}</span>
                      </div>
                      <Link to="/power" className="text-xs text-link hover:underline">
                        Inspect →
                      </Link>
                    </li>
                  ))}
                </ul>
                {sorted.length > LIST_LIMIT && (
                  <p className="mt-3 text-xs text-fg-secondary">
                    Showing the first {LIST_LIMIT} of {sorted.length} by severity.
                  </p>
                )}
              </>
            );
          }}
        </DashboardSection>
      </div>

      {/* 3. Collector connectivity: the backend's heartbeat-based classification. */}
      <DashboardSection
        id="collectors"
        title="Collector connectivity"
        what="collector connectivity"
        permission={DASHBOARD_PERMISSIONS.collectors}
        access={collectorsAccess}
        query={collectorsQuery}
        emptyText="No collectors are registered."
        aside={(collectors) => (
          <>
            <Count>{collectors.length}</Count> registered
          </>
        )}
      >
        {(collectors) => {
          const counts = connectivityCounts(collectors);
          const attention = collectorsNeedingAttention(collectors);
          return (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
                <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="offline" /> <Count>{counts.offline}</Count></span>
                <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="stale" /> <Count>{counts.stale}</Count></span>
                <span className="inline-flex items-center gap-1.5"><CollectorConnectivity health="healthy" /> <Count>{counts.healthy}</Count></span>
                {counts.unrecognised > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-fg-secondary">Other classification <Count>{counts.unrecognised}</Count></span>
                )}
              </div>
              {attention.length === 0 ? (
                <p className="text-sm text-fg-secondary">Every registered collector reports a Healthy heartbeat.</p>
              ) : (
                <ul className="space-y-2">
                  {attention.slice(0, LIST_LIMIT).map((collector) => (
                    <li key={collector.id} className={rowClass}>
                      <div className="min-w-0">
                        <span className="mr-2"><CollectorConnectivity health={collector.health} /></span>
                        <span>{collector.name}</span>
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
              <p className="mt-3 text-xs text-fg-secondary">
                Connectivity comes from collector heartbeats. It does not say how fresh any measurement is.{" "}
                <Link className="text-link hover:underline" to="/collectors">
                  Open Collectors →
                </Link>{" "}
                <Link className="text-link hover:underline" to="/integrations">
                  Open Integrations →
                </Link>
              </p>
            </>
          );
        }}
      </DashboardSection>

      {/* 4. Inventory and capacity, from the dashboard summary the API computes. */}
      <DashboardSection
        id="inventory"
        title="Infrastructure inventory"
        what="the infrastructure inventory summary"
        permission={DASHBOARD_PERMISSIONS.inventory}
        access={inventoryAccess}
        query={summaryQuery}
        emptyText="The API returned no inventory summary."
      >
        {(s) => (
          <div className="space-y-5">
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Sites and racks</h3>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
                <StatCard label="Sites" value={s.site_summary.sites} />
                <StatCard label="Buildings" value={s.site_summary.buildings} />
                <StatCard label="Floors" value={s.site_summary.floors} />
                <StatCard label="Rooms" value={s.site_summary.rooms} />
                <Link to="/racks">
                  <StatCard label="Racks" value={s.site_summary.racks} />
                </Link>
                <Link to="/equipment">
                  <StatCard label="Equipment" value={s.site_summary.equipment} />
                </Link>
                <Link to="/power">
                  <StatCard label="Power Nodes" value={s.site_summary.power_nodes} />
                </Link>
              </div>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Rack capacity</h3>
              <div className="grid grid-cols-3 gap-3">
                <StatCard label="Total Racks" value={s.rack_summary.total_racks} />
                <StatCard label="Occupied" value={s.rack_summary.occupied_racks} />
                <StatCard label="Available" value={s.rack_summary.available_racks} />
              </div>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Power capacity</h3>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatCard
                  label="Configured Capacity"
                  value={s.capacity_summary.total_configured_kw === null ? "unknown" : `${s.capacity_summary.total_configured_kw.toFixed(0)} kW`}
                  sub={`${s.capacity_summary.nodes_with_known_capacity} known / ${s.capacity_summary.nodes_with_unknown_capacity} unknown`}
                />
                <Link to="/power">
                  <StatCard label="Overloaded Nodes" value={s.power_summary.overloaded_nodes} />
                </Link>
                <Link to="/power">
                  <StatCard label="Near-Capacity Nodes" value={s.power_summary.near_capacity_nodes} />
                </Link>
                <Link to="/power">
                  <StatCard label="Redundancy Degraded" value={s.power_summary.redundancy_degraded_equipment} />
                </Link>
              </div>
            </div>
          </div>
        )}
      </DashboardSection>
    </div>
  );
}
