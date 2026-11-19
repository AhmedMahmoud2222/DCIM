import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Access, DASHBOARD_PERMISSIONS } from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { getDashboardSummary } from "@/features/power/api";

function Stat({ label, value, sub, to }: { label: string; value: string | number; sub?: string; to?: string }) {
  const body = (
    <>
      <span className="block text-[11px] uppercase tracking-wide text-fg-secondary">{label}</span>
      <span className="mt-0.5 block text-lg font-semibold tabular-nums text-fg-primary">{value}</span>
      {sub && <span className="block text-[11px] text-fg-secondary">{sub}</span>}
    </>
  );
  const box = "block h-full rounded-sm border border-line-subtle bg-surface-page px-2.5 py-2";
  return to ? (
    <Link to={to} className={`${box} hover:bg-surface-hover`}>
      {body}
    </Link>
  ) : (
    <div className={box}>{body}</div>
  );
}

/** Inventory, rack and power capacity, all from the dashboard summary the API computes. */
export function InventoryPanel({ access, className = "" }: { access: Access; className?: string }) {
  const query = useQuery({ queryKey: ["dashboard", "summary"], queryFn: () => getDashboardSummary(), enabled: access === "granted" });
  return (
    <DashboardSection
      id="inventory"
      title="Inventory and capacity"
      what="the infrastructure inventory summary"
      permission={DASHBOARD_PERMISSIONS.inventory}
      access={access}
      query={query}
      className={className}
      emptyText="The API returned no inventory summary."
    >
      {(s) => (
        <div className="grid gap-4 lg:grid-cols-3">
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Inventory</h3>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-2">
              <Stat label="Sites" value={s.site_summary.sites} />
              <Stat label="Rooms" value={s.site_summary.rooms} sub={`${s.site_summary.buildings} buildings, ${s.site_summary.floors} floors`} />
              <Stat label="Equipment" value={s.site_summary.equipment} to="/equipment" />
              <Stat label="Power Nodes" value={s.site_summary.power_nodes} to="/power" />
            </div>
          </div>
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Rack capacity</h3>
            <div className="grid grid-cols-3 gap-2 lg:grid-cols-2">
              <Stat label="Racks" value={s.rack_summary.total_racks} to="/racks" />
              <Stat label="Occupied" value={s.rack_summary.occupied_racks} />
              <Stat label="Available" value={s.rack_summary.available_racks} />
            </div>
          </div>
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-secondary">Power capacity</h3>
            <div className="grid grid-cols-2 gap-2">
              <Stat
                label="Configured"
                value={s.capacity_summary.total_configured_kw === null ? "unknown" : `${s.capacity_summary.total_configured_kw.toFixed(0)} kW`}
                sub={`${s.capacity_summary.nodes_with_known_capacity} known / ${s.capacity_summary.nodes_with_unknown_capacity} unknown`}
              />
              <Stat label="Overloaded Nodes" value={s.power_summary.overloaded_nodes} to="/power" />
              <Stat label="Near-Capacity Nodes" value={s.power_summary.near_capacity_nodes} to="/power" />
              <Stat label="Redundancy Degraded" value={s.power_summary.redundancy_degraded_equipment} to="/power" />
            </div>
          </div>
        </div>
      )}
    </DashboardSection>
  );
}
