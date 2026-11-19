import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Access, DASHBOARD_PERMISSIONS, exceptionHref, exceptionPreview } from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { getDashboardExceptions } from "@/features/power/api";
import { ExceptionSeverity } from "@/features/status/StatusBadges";

export const INFRASTRUCTURE_EVENTS_HREF = "/events?view=infrastructure";

/** A compact preview of the infrastructure exceptions: counts from the whole list, the three most severe
 * rows and an explicit count of the rest. The full list lives in Events → Infrastructure. These are
 * derived capacity and topology findings, not alarms. */
export function ExceptionsPreview({ access, className = "" }: { access: Access; className?: string }) {
  const query = useQuery({ queryKey: ["dashboard", "exceptions"], queryFn: getDashboardExceptions, enabled: access === "granted" });
  return (
    <DashboardSection
      id="exceptions"
      title="Events & exceptions"
      what="infrastructure exceptions"
      permission={DASHBOARD_PERMISSIONS.exceptions}
      access={access}
      query={query}
      className={className}
      emptyText="No capacity or topology exceptions detected."
      aside={(items) => {
        const { counts } = exceptionPreview(items);
        return (
          <>
            {(["critical", "warning", "info"] as const).map((level) =>
              counts[level] > 0 ? (
                <span key={level} className="inline-flex items-center gap-1">
                  <ExceptionSeverity severity={level} size="sm" /> <span className="font-semibold tabular-nums text-fg-primary">{counts[level]}</span>
                </span>
              ) : null,
            )}
            {counts.unrecognised > 0 && (
              <span className="inline-flex items-center gap-1">
                <ExceptionSeverity severity={null} size="sm" /> <span className="font-semibold tabular-nums text-fg-primary">{counts.unrecognised}</span>
              </span>
            )}
          </>
        );
      }}
      footer={
        <p className="mt-3 text-xs text-fg-secondary">
          <Link className="text-link underline underline-offset-2" to={INFRASTRUCTURE_EVENTS_HREF}>
            Open all in Events → Infrastructure
          </Link>
        </p>
      }
    >
      {(items) => {
        const preview = exceptionPreview(items);
        return (
          <>
            <ul className="space-y-2">
              {preview.shown.map((exc, i) => (
                <li key={`${exc.code}-${exc.object_id}-${i}`} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-sm bg-surface-inset px-3 py-2 text-sm">
                  <div className="min-w-0 [overflow-wrap:anywhere]">
                    <span className="mr-2">
                      <ExceptionSeverity severity={exc.severity} size="sm" />
                    </span>
                    <span className="mr-2 rounded-sm bg-tone-neutral-bg px-1.5 py-0.5 font-mono text-[10px] text-tone-neutral-fg">{exc.code}</span>
                    <span className="text-fg-secondary">{exc.message}</span>
                  </div>
                  <Link className="text-xs text-link underline underline-offset-2" to={exceptionHref(exc)} aria-label={`Inspect ${exc.code}`}>
                    Inspect →
                  </Link>
                </li>
              ))}
            </ul>
            {preview.hidden > 0 && (
              <p data-testid="exceptions-more" className="mt-2 text-xs text-fg-secondary">
                +{preview.hidden} more
                {preview.hiddenCritical > 0 ? `, including ${preview.hiddenCritical} Critical not shown here` : ""}.
              </p>
            )}
          </>
        );
      }}
    </DashboardSection>
  );
}
