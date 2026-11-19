import { ReactNode } from "react";

import { TableError } from "@/components/data/DataTable";
import { AccessRestricted } from "@/components/feedback/AccessRestricted";
import type { Access } from "@/features/dashboard/dashboardModel";
import { ApiError } from "@/lib/apiClient";

export interface QueryLike<T> {
  isLoading: boolean;
  error: unknown;
  data: T | undefined;
  /** React Query's fetch status; "paused" means the request is waiting for the network. */
  fetchStatus?: string;
  /** When the data on screen was last received (ms since epoch); 0 or absent when unknown. */
  dataUpdatedAt?: number;
  refetch: () => unknown;
}

export type SectionState = "loading" | "restricted" | "error" | "stale" | "empty" | "ready";

interface DashboardSectionProps<T> {
  id: string;
  title: string;
  /** What the user cannot see when access is denied or a load fails, e.g. "active alarms". */
  what: string;
  permission: string;
  access: Access;
  query: QueryLike<T>;
  /** Shown only when a successful response held no records. */
  emptyText: string;
  /** Overrides the default emptiness test (an empty array, or no data). */
  isEmpty?: (data: T) => boolean;
  /** Small summary next to the heading, shown only when the section has data. */
  aside?: (data: T) => ReactNode;
  /** Controls shown above the records or the empty message whenever a response is on screen (ready,
   * empty or stale), so a filter that produced nothing can still be changed. */
  controls?: (data: T) => ReactNode;
  /** Shown under the records, or under the empty message, but never while loading, restricted or in
   * error. Use it for information that stays true when the main list is empty. */
  footer?: ReactNode;
  className?: string;
  children: (data: T) => ReactNode;
}

const defaultIsEmpty = (data: unknown) => data == null || (Array.isArray(data) && data.length === 0);

/** One independently loaded part of the dashboard. It is always in exactly one of six states:
 *
 * - loading: the request is in flight or paused for the network, or the profile has not loaded so
 *   access is not yet known. A query with no data and no error is never empty: empty needs a
 *   successful response;
 * - restricted: the user is known not to hold the permission (no request is made), or the backend
 *   answered 403. Never rendered as zero records, a healthy state or a failure;
 * - error: any other failure with nothing loaded earlier; says so and offers a retry, never an empty list;
 * - stale: a refresh failed but an earlier response is still on screen; it stays visible and the
 *   section says it may be out of date and offers a retry;
 * - empty: a successful response with no records;
 * - ready: the records. */
export function DashboardSection<T>({
  id,
  title,
  what,
  permission,
  access,
  query,
  emptyText,
  isEmpty = defaultIsEmpty,
  aside,
  controls,
  footer,
  className = "",
  children,
}: DashboardSectionProps<T>) {
  let state: SectionState;
  if (access === "denied") state = "restricted";
  else if (access === "unknown" || query.isLoading) state = "loading";
  else if (query.error instanceof ApiError && query.error.status === 403) state = "restricted";
  else if (query.error) state = query.data === undefined ? "error" : "stale";
  else if (query.data === undefined) state = "loading";
  else if (isEmpty(query.data)) state = "empty";
  else state = "ready";

  const shown = state === "ready" || state === "empty" || state === "stale" ? (query.data as T) : undefined;
  const showRecords = shown !== undefined && !isEmpty(shown);
  const showEmpty = shown !== undefined && !showRecords;
  const loadedAt = query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toLocaleTimeString() : null;

  return (
    <section
      aria-labelledby={`${id}-title`}
      data-section={id}
      data-state={state}
      className={`min-w-0 rounded-sm border border-line-subtle bg-surface-panel p-4 ${className}`}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h2 id={`${id}-title`} className="text-base font-semibold text-fg-primary">
          {title}
        </h2>
        {showRecords && aside ? <div className="flex flex-wrap items-center gap-2 text-xs text-fg-secondary">{aside(shown)}</div> : null}
      </div>

      {state === "loading" && (
        <p role="status" aria-busy="true" className="text-sm text-fg-secondary">
          {query.fetchStatus === "paused" ? `Waiting for the network to load ${what}…` : `Loading ${what}…`}
        </p>
      )}
      {state === "restricted" && <AccessRestricted what={what} permission={permission} headingLevel={3} />}
      {state === "error" && (
        <TableError error={query.error} what={what} permission={permission} headingLevel={3} onRetry={() => void query.refetch()} />
      )}
      {state === "stale" && (
        <div
          data-testid={`${id}-stale`}
          role="status"
          className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-sm border border-tone-caution-fg bg-tone-caution-bg px-3 py-2 text-xs text-tone-caution-fg"
        >
          <span>
            Could not refresh {what}.{loadedAt ? ` Showing data loaded at ${loadedAt}` : " Showing earlier data"}; it may be out of date.
          </span>
          <button
            type="button"
            onClick={() => void query.refetch()}
            className="min-h-11 min-w-11 rounded-sm border border-line-control bg-surface-panel px-3 py-1 font-medium text-fg-primary hover:bg-surface-hover"
          >
            Try again
          </button>
        </div>
      )}
      {shown !== undefined && controls?.(shown)}
      {showEmpty && (
        <p data-testid={`${id}-empty`} className="text-sm text-fg-secondary">
          {emptyText}
        </p>
      )}
      {showRecords && children(shown)}
      {shown !== undefined && footer}
    </section>
  );
}
