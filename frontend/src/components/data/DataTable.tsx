import { ReactNode } from "react";

import { AccessRestricted } from "@/components/feedback/AccessRestricted";
import { ApiError } from "@/lib/apiClient";

/** Shared table styling (UI/UX checkpoint 4 of 7; Equipment is the pilot). Class strings are exported
 * whole so Tailwind sees them and so a page that is not migrated yet can adopt one cell at a time.
 *
 * - Header cells are small caps in the secondary text color; body cells use the page text color.
 * - Numeric columns are right-aligned with tabular figures (`TD_NUMBER`), text columns left-aligned.
 * - On a narrow screen the table scrolls sideways inside its focusable region, so no column is ever
 *   hidden. Columns are deliberately not pinned: a pinned first column took over half of a phone screen
 *   and covered the neighbouring column's text.
 * - Rows highlight on hover and while they contain keyboard focus. */
export const TH = "px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-fg-secondary";
export const TH_NUMBER = "px-3 py-2 text-right text-[11px] font-semibold uppercase tracking-wide text-fg-secondary";
export const TD = "px-3 py-2 align-middle";
export const TD_NUMBER = "px-3 py-2 text-right align-middle tabular-nums";
export const TR = "border-b border-line-subtle last:border-b-0 hover:bg-white/5 focus-within:bg-white/5";

interface DataTableProps {
  /** Accessible name of the table and of its scrolling region, e.g. "Equipment". */
  label: string;
  /** Narrowest width before the region scrolls sideways instead of squeezing columns. */
  minWidthClass?: string;
  /** Smaller text for tables with many columns. */
  dense?: boolean;
  /** Extra classes for the scroll region, for placing the table in a grid. */
  className?: string;
  children: ReactNode;
}

/** A table inside a keyboard-focusable scroll region. The region is focusable so a keyboard user can
 * scroll it; it takes the shared focus ring from index.css. */
export function DataTable({ label, minWidthClass = "min-w-[40rem]", dense = false, className = "", children }: DataTableProps) {
  return (
    <div
      role="region"
      aria-label={`${label} table`}
      tabIndex={0}
      className={`min-w-0 overflow-x-auto rounded-sm border border-line-subtle bg-surface-page ${className}`}
    >
      <table aria-label={label} className={`w-full border-collapse ${dense ? "text-xs" : "text-sm"} text-fg-primary ${minWidthClass}`}>
        {children}
      </table>
    </div>
  );
}

/** "Showing 12 of 340" when the list is cut short, otherwise the plain count. A list the server
 * truncated must say so; silently showing the first page as if it were everything misleads. */
export function RowCount({ shown, total, noun }: { shown: number; total: number | undefined; noun: string }) {
  const truncated = total !== undefined && total > shown;
  return (
    <p className="mb-2 text-xs text-fg-secondary" data-testid="row-count">
      {truncated ? `Showing ${shown} of ${total} ${noun}. The remaining ${total - shown} are not listed here.` : `${shown} ${noun}`}
    </p>
  );
}

/** Placeholder rows while the first load is in flight; keeps the table's height so the page does not jump. */
export function TableLoading({ label, columns, rows = 5 }: { label: string; columns: number; rows?: number }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="rounded-sm border border-line-subtle bg-surface-page p-3">
      <p className="mb-3 text-sm text-fg-secondary">Loading {label}…</p>
      <div aria-hidden="true" className="space-y-2">
        {Array.from({ length: rows }, (_, row) => (
          <div key={row} className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {Array.from({ length: columns }, (_, column) => (
              <div key={column} className="h-4 animate-pulse rounded-sm bg-line-subtle" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** A failed load. An authorization failure (403) is "Access restricted", never an empty table or an
 * error that looks like an outage; anything else is announced as an alert with a retry. */
export function TableError({
  error,
  what,
  permission,
  onRetry,
  headingLevel = 2,
}: {
  error: unknown;
  what: string;
  permission?: string;
  onRetry?: () => void;
  /** Level of the Access restricted heading; 2 under a page h1, 3 inside a section that has its own h2. */
  headingLevel?: 2 | 3;
}) {
  if (error instanceof ApiError && error.status === 403) {
    return <AccessRestricted what={what} permission={permission} headingLevel={headingLevel} />;
  }
  const message = error instanceof Error ? error.message : "The request failed.";
  return (
    <div role="alert" className="rounded-sm border border-tone-danger-fg bg-tone-danger-bg p-3 text-sm text-tone-danger-fg">
      <p className="font-medium">Could not load {what}.</p>
      <p className="mt-1">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded-sm border border-tone-danger-fg px-2 py-1 text-xs font-medium hover:bg-white/10"
        >
          Try again
        </button>
      )}
    </div>
  );
}

/** Row shown when a successful load returned nothing. */
export function EmptyRow({ columns, children }: { columns: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={columns} className="px-3 py-8 text-center text-fg-secondary">
        {children}
      </td>
    </tr>
  );
}
