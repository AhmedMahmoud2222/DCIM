import { TableError, TableLoading } from "@/components/data/DataTable";

interface QueryLike {
  isLoading: boolean;
  error: unknown;
  data: unknown;
  /** React Query's fetch status; "paused" is a first request waiting for the network. */
  fetchStatus?: string;
  refetch: () => unknown;
}

/** The loading and failure half of a list page: render it above the table and show the table only
 * when `query.data` is defined.
 *
 * - failure: an alert with the message and a retry; a 403 becomes "Access restricted" naming the
 *   permission when it is known. A failed request is never rendered as an empty list;
 * - first load, or a first request paused for the network: a polite loading status;
 * - otherwise nothing, and the page shows its rows or its own empty message.
 *
 * Whatever data an earlier success left behind stays on screen next to a failed refresh. */
export function QueryStatus({
  query,
  what,
  permission,
  columns = 4,
}: {
  query: QueryLike;
  /** Lower-case noun for the messages, e.g. "cables". */
  what: string;
  /** Permission a 403 is explained with, when the page knows it. */
  permission?: string;
  columns?: number;
}) {
  if (query.error) {
    return (
      <div className="mb-3">
        <TableError error={query.error} what={what} permission={permission} headingLevel={2} onRetry={() => void query.refetch()} />
      </div>
    );
  }
  if (query.isLoading || (query.data === undefined && query.fetchStatus === "paused")) {
    return <TableLoading label={what} columns={columns} />;
  }
  return null;
}
