import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";
import { getDashboardExceptions, getDashboardSummary } from "@/features/power/api";

const SEVERITY_COLORS: Record<string, string> = {
  critical: "bg-red-900 text-red-100",
  warning: "bg-yellow-800 text-yellow-100",
  info: "bg-slate-700 text-slate-200",
};

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900 p-4">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-slate-100">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

export function DashboardPage() {
  const { user } = useAuth();
  const summaryQuery = useQuery({ queryKey: ["dashboard", "summary"], queryFn: () => getDashboardSummary() });
  const exceptionsQuery = useQuery({ queryKey: ["dashboard", "exceptions"], queryFn: getDashboardExceptions });

  const s = summaryQuery.data;

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Welcome, {user?.full_name}</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Operational overview across infrastructure, power topology, and capacity. Every number below is computed by
        the API from live data — nothing here is aggregated in the browser.
      </p>

      {summaryQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}

      {s && (
        <>
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Site Summary</h2>
          <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
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

          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Rack Capacity</h2>
          <div className="mb-6 grid grid-cols-3 gap-4">
            <StatCard label="Total Racks" value={s.rack_summary.total_racks} />
            <StatCard label="Occupied" value={s.rack_summary.occupied_racks} />
            <StatCard label="Available" value={s.rack_summary.available_racks} />
          </div>

          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Power Capacity</h2>
          <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
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
        </>
      )}

      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Infrastructure Exceptions</h2>
      <div className="rounded border border-slate-800 bg-slate-900 p-4">
        {exceptionsQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {exceptionsQuery.data?.length === 0 && <p className="text-sm text-slate-500">No capacity or topology exceptions detected.</p>}
        <div className="space-y-2">
          {exceptionsQuery.data?.map((exc, i) => (
            <div key={i} className="flex items-center justify-between rounded bg-slate-800/50 px-3 py-2 text-sm">
              <div>
                <span className={`mr-2 rounded px-1.5 py-0.5 text-[10px] ${SEVERITY_COLORS[exc.severity] ?? "bg-slate-700"}`}>
                  {exc.code}
                </span>
                <span className="text-slate-300">{exc.message}</span>
              </div>
              <Link to="/power" className="text-xs text-blue-400 hover:underline">
                Inspect →
              </Link>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
