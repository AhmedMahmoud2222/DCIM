import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { EmptyState, MetricCard, PageHeader, SectionTitle, StatusBadge } from "@/components/ui/ProductUi";
import { useAuth } from "@/features/auth/useAuth";
import { getDashboardExceptions, getDashboardSummary } from "@/features/power/api";
import { getOpenAlarms } from "@/features/telemetry/api";

function exceptionTone(severity: string) {
  if (severity === "critical") return "critical" as const;
  if (severity === "warning") return "warning" as const;
  return "info" as const;
}

export function DashboardPage() {
  const { user } = useAuth();
  const summaryQuery = useQuery({ queryKey: ["dashboard", "summary"], queryFn: () => getDashboardSummary() });
  const exceptionsQuery = useQuery({ queryKey: ["dashboard", "exceptions"], queryFn: getDashboardExceptions });
  const activeAlarmsQuery = useQuery({ queryKey: ["alarms", "active"], queryFn: () => getOpenAlarms("ACTIVE") });
  const acknowledgedAlarmsQuery = useQuery({ queryKey: ["alarms", "acknowledged"], queryFn: () => getOpenAlarms("ACKNOWLEDGED") });
  const summary = summaryQuery.data;
  const activeCount = activeAlarmsQuery.data?.length;
  const exceptionCount = exceptionsQuery.data?.length;

  return <div className="page">
    <PageHeader eyebrow="Operations overview" title={`Good ${new Date().getHours() < 12 ? "morning" : "afternoon"}, ${user?.full_name ?? "operator"}`} description="A concise view of infrastructure conditions, active operational work, and capacity risk. Values are reported by the DCIM APIs; unavailable signals stay explicitly unknown." actions={<Link to="/infrastructure" className="action-secondary">Explore infrastructure</Link>} />

    {(summaryQuery.isLoading || activeAlarmsQuery.isLoading || exceptionsQuery.isLoading) && <div className="surface-muted p-5 text-sm text-slate-400">Loading operational status…</div>}
    {summaryQuery.isError && <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">The dashboard summary is unavailable. Individual operational screens remain available from the sidebar.</div>}

    {summary && <>
      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Link to="/events" className="block"><MetricCard label="Active conditions" value={activeCount ?? "—"} detail="Requires operator attention · open Events" tone={(activeCount ?? 0) > 0 ? "critical" : "healthy"} /></Link>
        <MetricCard label="Acknowledged" value={acknowledgedAlarmsQuery.data?.length ?? "—"} detail="Awaiting clearance" tone={(acknowledgedAlarmsQuery.data?.length ?? 0) > 0 ? "warning" : "neutral"} />
        <MetricCard label="Capacity exceptions" value={exceptionCount ?? "—"} detail="Power and topology checks" tone={(exceptionCount ?? 0) > 0 ? "warning" : "healthy"} />
        <MetricCard label="Rack availability" value={`${summary.rack_summary.available_racks}/${summary.rack_summary.total_racks}`} detail={`${summary.rack_summary.occupied_racks} occupied racks`} tone="info" />
      </section>

      <section className="grid gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,.75fr)]">
        <div className="surface p-5">
          <SectionTitle title="Operational exceptions" detail="Conditions reported by the current dashboard data" action={<Link to="/power" className="text-xs font-medium text-indigo-300 hover:text-indigo-200">Open power workspace</Link>} />
          <div className="mt-4 space-y-2">
            {exceptionsQuery.data?.map((exception, index) => <Link key={`${exception.object_id}-${index}`} to="/power" className="group flex items-start gap-3 rounded-lg border border-slate-800 bg-slate-950/30 p-3 transition hover:border-slate-700 hover:bg-slate-800/40"><StatusBadge label={exception.severity} tone={exceptionTone(exception.severity)} /><div className="min-w-0 flex-1"><p className="text-sm text-slate-200">{exception.message}</p><p className="mt-1 text-xs text-slate-500">{exception.code.replace(/_/g, " ")}</p></div><span className="text-slate-600 transition group-hover:text-indigo-300">›</span></Link>)}
            {exceptionsQuery.data?.length === 0 && <EmptyState title="No infrastructure exceptions" detail="The current capacity and topology checks do not report an exception." />}
          </div>
        </div>
        <div className="surface p-5">
          <SectionTitle title="Open alarms" detail="Current conditions, not historical events" action={<Link to="/events" className="text-xs font-medium text-indigo-300 hover:text-indigo-200">Open Events</Link>} />
          <div className="mt-4 space-y-2">
            {activeAlarmsQuery.data?.slice(0, 6).map((alarm) => <Link key={alarm.id} to="/events" className="block rounded-lg border border-slate-800 bg-slate-950/30 p-3 transition hover:border-indigo-400/30 hover:bg-slate-800/40"><div className="flex items-center justify-between gap-2"><StatusBadge label="Active" tone="critical" /><span className="truncate text-xs text-slate-500">{alarm.subject_key}</span></div><p className="mt-2 text-sm text-slate-200">{alarm.last_value ?? "No current value"}</p><p className="mt-1 text-xs text-slate-500">Occurred {new Date(alarm.opened_at).toLocaleString()}</p></Link>)}
            {activeAlarmsQuery.data?.length === 0 && <EmptyState title="No active alarms" detail="No current alarm condition has been reported." />}
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.2fr_.8fr]">
        <div className="surface p-5"><SectionTitle title="Power capacity" detail="Topology-derived allocation; unknown stays unknown" action={<Link to="/power" className="text-xs font-medium text-indigo-300 hover:text-indigo-200">View topology</Link>} /><div className="mt-4 grid gap-3 sm:grid-cols-3"><MetricCard label="Configured" value={summary.capacity_summary.total_configured_kw === null ? "Unknown" : `${summary.capacity_summary.total_configured_kw.toFixed(1)} kW`} detail={`${summary.capacity_summary.nodes_with_known_capacity} nodes with known capacity`} /><MetricCard label="Near capacity" value={summary.power_summary.near_capacity_nodes} detail="Warning threshold" tone={summary.power_summary.near_capacity_nodes ? "warning" : "healthy"} /><MetricCard label="Overloaded" value={summary.power_summary.overloaded_nodes} detail="Critical threshold" tone={summary.power_summary.overloaded_nodes ? "critical" : "healthy"} /></div></div>
        <div className="surface p-5"><SectionTitle title="Inventory at a glance" detail="Authoritative physical inventory" /><div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 text-sm"><Link to="/infrastructure" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Sites</span><strong>{summary.site_summary.sites}</strong></Link><Link to="/infrastructure" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Rooms</span><strong>{summary.site_summary.rooms}</strong></Link><Link to="/racks" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Racks</span><strong>{summary.site_summary.racks}</strong></Link><Link to="/equipment" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Equipment</span><strong>{summary.site_summary.equipment}</strong></Link><Link to="/power" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Power nodes</span><strong>{summary.site_summary.power_nodes}</strong></Link><Link to="/collectors" className="flex justify-between text-slate-300 hover:text-indigo-300"><span>Collectors</span><strong>Inspect</strong></Link></div></div>
      </section>
    </>}
  </div>;
}
