import type { CurrentUser } from "@/lib/authStore";
import type { Collector, DashboardExceptionItem } from "@/types";
import type { Alarm } from "@/features/telemetry/api";

/** What the browser already knows about the signed-in user's right to read a dashboard source.
 * `unknown` is only the moment before the profile has loaded; it must read as loading, never as
 * denied and never as empty. The backend still authorizes every request; this only avoids asking
 * for data the user is already known not to be allowed to read. */
export type Access = "unknown" | "granted" | "denied";

export function accessFor(user: CurrentUser | null, permission: string): Access {
  if (!user) return "unknown";
  return user.permission_codes.includes(permission) ? "granted" : "denied";
}

/** Permission each dashboard source needs; these mirror the backend routes (`/dashboard/*` needs
 * dashboard:read, `/alarms` alarm:read, `/collectors` collector:read). */
export const DASHBOARD_PERMISSIONS = {
  alarms: "alarm:read",
  exceptions: "dashboard:read",
  collectors: "collector:read",
  inventory: "dashboard:read",
} as const;

/** How many alarms one request asks for. A list that reaches this size may be cut short, so its count
 * is shown as "N or more" instead of as an exact total. */
export const ALARM_LIST_LIMIT = 100;

export function countLabel(count: number, limit: number): string {
  return count >= limit ? `${limit} or more` : String(count);
}

// --- Alarms -----------------------------------------------------------------------------------
/** Newest first. The alarm record carries no severity, so recency is the only ordering that does not
 * invent importance. */
export function sortAlarmsNewestFirst(alarms: ReadonlyArray<Alarm>): Alarm[] {
  return [...alarms].sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at) || 0);
}

// --- Exceptions -------------------------------------------------------------------------------
const SEVERITY_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const UNRECOGNISED_RANK = 3;

const rank = (severity: string | null | undefined) =>
  severity != null && Object.prototype.hasOwnProperty.call(SEVERITY_RANK, severity) ? SEVERITY_RANK[severity] : UNRECOGNISED_RANK;

/** Backend severity order: critical, warning, info, then any value this build does not know. Equal
 * severities keep the order the API returned them in. */
export function sortExceptionsBySeverity(items: ReadonlyArray<DashboardExceptionItem>): DashboardExceptionItem[] {
  return items.map((item, index) => ({ item, index })).sort((a, b) => rank(a.item.severity) - rank(b.item.severity) || a.index - b.index).map((entry) => entry.item);
}

export interface SeverityCounts {
  critical: number;
  warning: number;
  info: number;
  unrecognised: number;
}

export function severityCounts(items: ReadonlyArray<DashboardExceptionItem>): SeverityCounts {
  const counts: SeverityCounts = { critical: 0, warning: 0, info: 0, unrecognised: 0 };
  for (const item of items) {
    const r = rank(item.severity);
    if (r === 0) counts.critical += 1;
    else if (r === 1) counts.warning += 1;
    else if (r === 2) counts.info += 1;
    else counts.unrecognised += 1;
  }
  return counts;
}

// --- Collector connectivity -------------------------------------------------------------------
export interface ConnectivityCounts {
  offline: number;
  stale: number;
  healthy: number;
  unrecognised: number;
}

/** Counts of the backend's `health` classification, which is derived from heartbeat age. */
export function connectivityCounts(collectors: ReadonlyArray<Collector>): ConnectivityCounts {
  const counts: ConnectivityCounts = { offline: 0, stale: 0, healthy: 0, unrecognised: 0 };
  for (const collector of collectors) {
    if (collector.health === "offline") counts.offline += 1;
    else if (collector.health === "stale") counts.stale += 1;
    else if (collector.health === "healthy") counts.healthy += 1;
    else counts.unrecognised += 1;
  }
  return counts;
}

const CONCERN_RANK: Record<string, number> = { offline: 0, stale: 1 };

/** Collectors that need attention first: offline, then stale, then values this build does not know.
 * Healthy collectors are not listed individually. Ties are broken by name so the order is stable. */
export function collectorsNeedingAttention(collectors: ReadonlyArray<Collector>): Collector[] {
  const attention = collectors.filter((collector) => collector.health !== "healthy");
  const order = (collector: Collector) => (Object.prototype.hasOwnProperty.call(CONCERN_RANK, collector.health) ? CONCERN_RANK[collector.health] : 2);
  return [...attention].sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name));
}
