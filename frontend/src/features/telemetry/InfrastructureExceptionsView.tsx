import { useQuery } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { QueryStatus } from "@/components/data/QueryStatus";
import { AccessRestricted } from "@/components/feedback/AccessRestricted";
import { useAuth } from "@/features/auth/useAuth";
import { accessFor, DASHBOARD_PERMISSIONS, exceptionHref, severityCounts, sortExceptionsBySeverity } from "@/features/dashboard/dashboardModel";
import { getDashboardExceptions } from "@/features/power/api";
import { ExceptionSeverity } from "@/features/status/StatusBadges";

type Filter = "all" | "critical" | "warning" | "info" | "unrecognised";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "critical", label: "Critical" },
  { id: "warning", label: "Warning" },
  { id: "info", label: "Info" },
  { id: "unrecognised", label: "Unknown severity" },
];
const KNOWN = ["critical", "warning", "info"];

function objectLabel(objectType: string) {
  if (objectType === "power_node") return "Power node";
  if (objectType === "equipment") return "Equipment";
  return objectType;
}

/** Events → Infrastructure: every derived capacity and topology exception, unbounded, most severe first.
 * These are not alarms: they have no workflow status, cannot be acknowledged and are never exported with
 * the alarm CSV. */
export function InfrastructureExceptionsView() {
  const { user } = useAuth();
  const access = accessFor(user, DASHBOARD_PERMISSIONS.exceptions);
  const [filter, setFilter] = useState<Filter>("all");
  const groupName = useId();
  const query = useQuery({ queryKey: ["dashboard", "exceptions"], queryFn: getDashboardExceptions, enabled: access === "granted" });
  const sorted = useMemo(() => sortExceptionsBySeverity(query.data ?? []), [query.data]);
  const counts = useMemo(() => severityCounts(query.data ?? []), [query.data]);
  const visible = sorted.filter((item) =>
    filter === "all" ? true : filter === "unrecognised" ? !KNOWN.includes(item.severity) : item.severity === filter,
  );

  if (access === "denied") {
    return (
      <div className="mt-4">
        <AccessRestricted what="infrastructure exceptions" permission={DASHBOARD_PERMISSIONS.exceptions} headingLevel={2} />
      </div>
    );
  }

  return (
    <section aria-labelledby="infrastructure-title" className="mt-4 rounded-sm border border-line-subtle bg-surface-panel p-4" data-testid="infrastructure-view">
      <h2 id="infrastructure-title" className="text-base font-semibold text-fg-primary">
        Infrastructure exceptions
      </h2>
      <p className="mt-1 text-xs text-fg-secondary">
        Capacity and topology findings derived from the model. They are not alarms: they have no acknowledgement and are not part of the alarm export.
      </p>
      <div className="mt-3">
        {access === "unknown" ? (
          <p role="status" aria-busy="true" className="text-sm text-fg-secondary">
            Loading infrastructure exceptions…
          </p>
        ) : (
          <QueryStatus query={query} what="infrastructure exceptions" permission={DASHBOARD_PERMISSIONS.exceptions} columns={5} />
        )}
      </div>
      {query.data !== undefined && query.data.length === 0 && (
        <p data-testid="infrastructure-empty" className="text-sm text-fg-secondary">
          No capacity or topology exceptions detected.
        </p>
      )}
      {query.data !== undefined && query.data.length > 0 && (
        <>
          <fieldset className="mb-3 flex flex-wrap gap-1">
            <legend className="sr-only">Severity</legend>
            {FILTERS.filter((f) => f.id !== "unrecognised" || counts.unrecognised > 0).map((f) => {
              const count = f.id === "all" ? query.data!.length : counts[f.id];
              return (
                <label
                  key={f.id}
                  className={`inline-flex min-h-8 cursor-pointer items-center gap-1 rounded-sm border px-2.5 py-1 text-xs has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-focus ${
                    filter === f.id ? "border-action-primary bg-action-primary text-action-primary-fg" : "border-line-control text-fg-secondary hover:bg-surface-hover"
                  }`}
                >
                  <input type="radio" name={groupName} value={f.id} checked={filter === f.id} onChange={() => setFilter(f.id)} className="sr-only" />
                  {f.label} <span className="tabular-nums">({count})</span>
                </label>
              );
            })}
          </fieldset>
          <div role="region" aria-label="Infrastructure exceptions table" tabIndex={0} className="relative overflow-x-auto">
            <table aria-label="Infrastructure exceptions" className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-fg-secondary">
                <tr>
                  <th scope="col" className="px-3 py-2">Severity</th>
                  <th scope="col" className="px-3 py-2">Code</th>
                  <th scope="col" className="px-3 py-2">Object</th>
                  <th scope="col" className="px-3 py-2">Message</th>
                  <th scope="col" className="px-3 py-2">
                    <span className="sr-only">Drill-down</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item, i) => (
                  <tr key={`${item.code}-${item.object_id}-${i}`} className="border-t border-line-subtle">
                    <td className="px-3 py-2">
                      <ExceptionSeverity severity={item.severity} size="sm" />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-fg-primary">{item.code}</td>
                    <td className="px-3 py-2 text-fg-secondary">
                      {objectLabel(item.object_type)}
                      <span className="block break-all font-mono text-[11px]">{item.object_id}</span>
                    </td>
                    <td className="min-w-[200px] px-3 py-2 text-fg-primary">{item.message}</td>
                    <td className="whitespace-nowrap px-3 py-2">
                      <Link className="text-xs text-link underline underline-offset-2" to={exceptionHref(item)} aria-label={`Inspect ${item.code} on ${objectLabel(item.object_type)} ${item.object_id}`}>
                        Inspect →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {visible.length === 0 && <p className="mt-2 text-sm text-fg-secondary">No exception with this severity.</p>}
        </>
      )}
    </section>
  );
}
