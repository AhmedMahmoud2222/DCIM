import { ReactNode } from "react";

import { TableError } from "@/components/data/DataTable";
import { AccessRestricted } from "@/components/feedback/AccessRestricted";
import type { Access } from "@/features/dashboard/dashboardModel";
import { ApiError } from "@/lib/apiClient";

interface QueryLike<T> {
  isLoading: boolean;
  error: unknown;
  data: T | undefined;
  /** React Query's fetch status; "paused" means the request is waiting for the network. */
  fetchStatus?: string;
  refetch: () => unknown;
}

export type SectionState = "loading" | "restricted" | "error" | "empty" | "ready";

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
  /** Shown under the records, or under the empty message, but never while loading, restricted or in
   * error. Use it for information that stays true when the main list is empty. */
  footer?: ReactNode;
  className?: string;
  children: (data: T) => ReactNode;
}

const defaultIsEmpty = (data: unknown) => data == null || (Array.isArray(data) && data.length === 0);

/** One independently loaded part of the dashboard. It is always in exactly one of five states, and
 * only `ready` shows records:
 *
 * - loading: the request is in flight or paused for the network, or the profile has not loaded so
 *   access is not yet known. A query with no data and no error is never empty: empty needs a
 *   successful response;
 * - restricted: the user is known not to hold the permission (no request is made), or the backend
 *   answered 403. Never rendered as zero records, a healthy state or a failure;
 * - error: any other failure; says so and offers a retry, and never as an empty list;
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
  footer,
  className = "",
  children,
}: DashboardSectionProps<T>) {
  let state: SectionState;
  if (access === "denied") state = "restricted";
  else if (access === "unknown" || query.isLoading) state = "loading";
  else if (query.error) state = query.error instanceof ApiError && query.error.status === 403 ? "restricted" : "error";
  else if (query.data === undefined) state = "loading";
  else if (isEmpty(query.data)) state = "empty";
  else state = "ready";

  const data = state === "ready" ? (query.data as T) : undefined;

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
        {data !== undefined && aside ? <div className="flex flex-wrap items-center gap-2 text-xs text-fg-secondary">{aside(data)}</div> : null}
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
      {state === "empty" && (
        <p data-testid={`${id}-empty`} className="text-sm text-fg-secondary">
          {emptyText}
        </p>
      )}
      {data !== undefined && children(data)}
      {(state === "ready" || state === "empty") && footer}
    </section>
  );
}
