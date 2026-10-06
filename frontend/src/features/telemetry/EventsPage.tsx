import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Alarm, acknowledgeAlarm, getOpenAlarms, listAlarmHistory } from "@/features/telemetry/api";

type ViewMode = "active" | "history";
type StatusFilter = "ALL" | Alarm["status"];

const STATUS_COLORS: Record<Alarm["status"], string> = {
  ACTIVE: "bg-red-900 text-red-100",
  ACKNOWLEDGED: "bg-yellow-800 text-yellow-100",
  CLEARED: "bg-green-800 text-green-100",
};

function csvCell(value: string | number | null | undefined) {
  const text = value == null ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function exportCsv(alarms: Alarm[], fileName: string) {
  const headings = ["opened_at", "acknowledged_at", "cleared_at", "status", "subject_key", "presentation_value", "presentation_unit", "last_value", "unit", "integration_id", "managed_asset_id", "rule_id"];
  const rows = alarms.map((alarm) =>
    [alarm.opened_at, alarm.acknowledged_at, alarm.cleared_at, alarm.status, alarm.subject_key, alarm.presentation_value, alarm.presentation_unit, alarm.last_value, alarm.unit, alarm.integration_id, alarm.managed_asset_id, alarm.rule_id]
      .map(csvCell)
      .join(","),
  );
  const blob = new Blob([[headings.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

export function EventsPage() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<ViewMode>("active");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("ALL");
  const [preset, setPreset] = useState("24");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [cursor, setCursor] = useState<string | undefined>();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const activeQuery = useQuery({ queryKey: ["events", "active"], queryFn: () => getOpenAlarms("ACTIVE") });
  const acknowledgedQuery = useQuery({ queryKey: ["events", "acknowledged"], queryFn: () => getOpenAlarms("ACKNOWLEDGED") });
  const historyWindow = useMemo(() => {
    if (preset === "custom") {
      const start = new Date(customStart);
      const end = new Date(customEnd);
      return { start, end, valid: Boolean(customStart && customEnd && !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) && start < end) };
    }
    const hours = Number(preset);
    return { start: new Date(Date.now() - hours * 3_600_000), end: new Date(), valid: true };
  }, [customEnd, customStart, preset]);
  const historyQuery = useQuery({
    queryKey: ["events", "history", status, preset, customStart, customEnd, cursor],
    queryFn: () => listAlarmHistory({ status: status === "ALL" ? undefined : status, start: historyWindow.start, end: historyWindow.end, cursor, limit: 50 }),
    enabled: view === "history" && historyWindow.valid,
  });
  const acknowledgeMutation = useMutation({
    mutationFn: acknowledgeAlarm,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["events"] });
      queryClient.invalidateQueries({ queryKey: ["alarms"] });
    },
  });

  const requestedAlarms = useMemo(() => {
    if (view === "history") return historyQuery.data?.items ?? [];
    return [...(activeQuery.data ?? []), ...(acknowledgedQuery.data ?? [])].sort(
      (left, right) => new Date(right.opened_at).getTime() - new Date(left.opened_at).getTime(),
    );
  }, [acknowledgedQuery.data, activeQuery.data, historyQuery.data?.items, view]);
  const visibleAlarms = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return requestedAlarms;
    return requestedAlarms.filter((alarm) =>
      [alarm.subject_key, alarm.integration_id, alarm.managed_asset_id, alarm.rule_id, alarm.status].some((value) => value?.toLowerCase().includes(needle)),
    );
  }, [requestedAlarms, search]);
  const selected = visibleAlarms.find((alarm) => alarm.id === selectedId) ?? visibleAlarms[0] ?? null;
  const loading = view === "active" ? activeQuery.isLoading || acknowledgedQuery.isLoading : historyQuery.isLoading;
  const error = view === "active" ? activeQuery.error || acknowledgedQuery.error : historyQuery.error;

  function changePreset(value: string) {
    setPreset(value);
    setCursor(undefined);
    setSelectedId(null);
    if (value !== "custom") {
      setCustomStart("");
      setCustomEnd("");
    }
  }

  return (
    <div>
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h1 className="mb-1 text-lg font-semibold">Events</h1>
          <p className="text-sm text-slate-500">
            Current alarm conditions and retained lifecycle history. Data is bounded by the existing alarm APIs; no event state
            is created in the browser.
          </p>
        </div>
        <button
          type="button"
          onClick={() => exportCsv(visibleAlarms, `dcim-events-${view}.csv`)}
          disabled={visibleAlarms.length === 0}
          className="whitespace-nowrap rounded-sm border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
        >
          Export current results CSV
        </button>
      </div>

      <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div role="tablist" aria-label="Events view" className="flex gap-2">
            <button
              type="button"
              role="tab"
              aria-selected={view === "active"}
              onClick={() => {
                setView("active");
                setSelectedId(null);
              }}
              className={`rounded-sm px-3 py-1.5 text-sm ${view === "active" ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-800"}`}
            >
              Active <span className="ml-1 text-xs opacity-80">{(activeQuery.data?.length ?? 0) + (acknowledgedQuery.data?.length ?? 0)}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === "history"}
              onClick={() => {
                setView("history");
                setCursor(undefined);
                setSelectedId(null);
              }}
              className={`rounded-sm px-3 py-1.5 text-sm ${view === "history" ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-800"}`}
            >
              History
            </button>
          </div>
          <div className="flex flex-wrap gap-2">
            {view === "history" && (
              <>
                <select
                  aria-label="History range"
                  className="rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
                  value={preset}
                  onChange={(event) => changePreset(event.target.value)}
                >
                  <option value="24">Last 24 hours</option>
                  <option value="168">Last 7 days</option>
                  <option value="720">Last 30 days</option>
                  <option value="custom">Custom range</option>
                </select>
                <select
                  aria-label="Alarm status"
                  className="rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
                  value={status}
                  onChange={(event) => {
                    setStatus(event.target.value as StatusFilter);
                    setCursor(undefined);
                    setSelectedId(null);
                  }}
                >
                  <option value="ALL">All states</option>
                  <option value="ACTIVE">Active</option>
                  <option value="ACKNOWLEDGED">Acknowledged</option>
                  <option value="CLEARED">Cleared</option>
                </select>
              </>
            )}
            <input
              aria-label="Search events"
              className="min-w-[220px] rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100 placeholder:text-slate-500"
              placeholder="Search subject or identifier"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        </div>
        {view === "history" && preset === "custom" && (
          <div className="mt-3 grid gap-2 border-t border-slate-800 pt-3 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              Start (local timezone)
              <input
                aria-label="Events start"
                type="datetime-local"
                className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
                value={customStart}
                onChange={(event) => {
                  setCustomStart(event.target.value);
                  setCursor(undefined);
                }}
              />
            </label>
            <label className="text-xs text-slate-400">
              End (local timezone)
              <input
                aria-label="Events end"
                type="datetime-local"
                className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
                value={customEnd}
                onChange={(event) => {
                  setCustomEnd(event.target.value);
                  setCursor(undefined);
                }}
              />
            </label>
            {!historyWindow.valid && <p className="text-sm text-yellow-400 sm:col-span-2">Select a start time before the end time to request history.</p>}
          </div>
        )}
      </div>

      {loading && <div className="mt-4 rounded-sm border border-slate-800 bg-slate-900 p-5 text-sm text-slate-400">Loading requested events…</div>}
      {error && (
        <div className="mt-4 rounded-sm border border-red-900 bg-red-950/40 p-4 text-sm text-red-300">Unable to retrieve the requested event records.</div>
      )}
      {!loading && !error && visibleAlarms.length === 0 && (
        <div className="mt-4 rounded-sm border border-slate-800 bg-slate-900 p-8 text-center text-sm text-slate-500">
          {view === "active"
            ? "No active or acknowledged alarm condition is currently reported."
            : "No events in this range. Try a different time range or state filter."}
        </div>
      )}

      {!loading && !error && visibleAlarms.length > 0 && (
        <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="overflow-hidden rounded-sm border border-slate-800 bg-slate-900">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-3 py-2">State</th>
                    <th className="px-3 py-2">Subject</th>
                    <th className="px-3 py-2">Latest value</th>
                    <th className="px-3 py-2">First occurrence</th>
                    <th className="px-3 py-2">Acknowledgement</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleAlarms.map((alarm) => (
                    <tr
                      key={alarm.id}
                      onClick={() => setSelectedId(alarm.id)}
                      className={`cursor-pointer border-t border-slate-800 ${selected?.id === alarm.id ? "bg-slate-800/70" : "hover:bg-slate-800/40"}`}
                    >
                      <td className="px-3 py-2">
                        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATUS_COLORS[alarm.status]}`}>{alarm.status}</span>
                      </td>
                      <td className="max-w-[280px] truncate px-3 py-2 font-medium text-slate-200">
                        {alarm.subject_key}
                        <p className="mt-0.5 font-mono text-[11px] text-slate-500">{alarm.id.slice(0, 8)}…</p>
                      </td>
                      <td className="px-3 py-2">{alarm.presentation_value}{alarm.presentation_unit === "%" ? "%" : ` ${alarm.presentation_unit ?? ""}`}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-slate-400">{new Date(alarm.opened_at).toLocaleString()}</td>
                      <td className="px-3 py-2 text-slate-400">
                        {alarm.acknowledged_at ? new Date(alarm.acknowledged_at).toLocaleString() : "Unacknowledged"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {view === "history" && (
              <div className="flex items-center justify-between border-t border-slate-800 px-4 py-3 text-sm">
                <span className="text-slate-500">Showing one bounded history page of up to 50 records.</span>
                {historyQuery.data?.next_cursor && (
                  <button
                    type="button"
                    onClick={() => {
                      setCursor(historyQuery.data?.next_cursor ?? undefined);
                      setSelectedId(null);
                    }}
                    className="rounded-sm border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800"
                  >
                    Next page →
                  </button>
                )}
              </div>
            )}
          </div>
          {selected && (
            <aside className="rounded-sm border border-slate-800 bg-slate-900 p-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Event detail</p>
              <div className="mt-3 flex items-center gap-2">
                <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATUS_COLORS[selected.status]}`}>{selected.status}</span>
                <span className="text-sm text-slate-400">last value {selected.presentation_value}{selected.presentation_unit === "%" ? "%" : ` ${selected.presentation_unit ?? ""}`}</span>
              </div>
              <h2 className="mt-3 wrap-break-word text-lg font-semibold text-slate-100">{selected.subject_key}</h2>
              <dl className="mt-4 space-y-2 text-sm">
                <div>
                  <dt className="text-slate-500">First occurrence</dt>
                  <dd>{new Date(selected.opened_at).toLocaleString()}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Latest recorded state</dt>
                  <dd>{selected.cleared_at ? `Cleared ${new Date(selected.cleared_at).toLocaleString()}` : "Not cleared"}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Acknowledgement</dt>
                  <dd>{selected.acknowledged_at ? new Date(selected.acknowledged_at).toLocaleString() : "Unacknowledged"}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Integration identifier</dt>
                  <dd className="break-all font-mono text-xs">{selected.integration_id}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Rule identifier</dt>
                  <dd className="break-all font-mono text-xs">{selected.rule_id}</dd>
                </div>
              </dl>
              {selected.managed_asset_id && (
                <Link className="mt-4 inline-flex text-sm font-medium text-blue-400 hover:underline" to={`/equipment/${selected.managed_asset_id}`}>
                  Open affected equipment →
                </Link>
              )}
              {selected.status === "ACTIVE" && (
                <button
                  type="button"
                  disabled={acknowledgeMutation.isPending}
                  onClick={() => acknowledgeMutation.mutate(selected.id)}
                  className="mt-4 w-full rounded-sm bg-yellow-800 px-3 py-1.5 text-sm font-medium text-yellow-100 hover:bg-yellow-700 disabled:opacity-50"
                >
                  Acknowledge active event
                </button>
              )}
              {acknowledgeMutation.isError && (
                <p className="mt-2 text-sm text-red-400">Unable to acknowledge this event. Your permissions may not allow acknowledgement.</p>
              )}
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
