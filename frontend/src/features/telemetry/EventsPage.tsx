import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { EmptyState, PageHeader, StatusBadge } from "@/components/ui/ProductUi";
import { acknowledgeAlarm, Alarm, getOpenAlarms, listAlarmHistory } from "@/features/telemetry/api";

type ViewMode = "active" | "history";
type StatusFilter = "ALL" | Alarm["status"];

function statusTone(status: Alarm["status"]) {
  if (status === "ACTIVE") return "critical" as const;
  if (status === "ACKNOWLEDGED") return "warning" as const;
  return "healthy" as const;
}

function csvCell(value: string | number | null | undefined) {
  const text = value == null ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function exportCsv(alarms: Alarm[], fileName: string) {
  const headings = ["opened_at", "acknowledged_at", "cleared_at", "status", "subject_key", "last_value", "integration_id", "managed_asset_id", "rule_id"];
  const rows = alarms.map((alarm) => [alarm.opened_at, alarm.acknowledged_at, alarm.cleared_at, alarm.status, alarm.subject_key, alarm.last_value, alarm.integration_id, alarm.managed_asset_id, alarm.rule_id].map(csvCell).join(","));
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
      const start = new Date(customStart); const end = new Date(customEnd);
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
    return [...(activeQuery.data ?? []), ...(acknowledgedQuery.data ?? [])].sort((left, right) => new Date(right.opened_at).getTime() - new Date(left.opened_at).getTime());
  }, [acknowledgedQuery.data, activeQuery.data, historyQuery.data?.items, view]);
  const visibleAlarms = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return requestedAlarms;
    return requestedAlarms.filter((alarm) => [alarm.subject_key, alarm.integration_id, alarm.managed_asset_id, alarm.rule_id, alarm.status].some((value) => value?.toLowerCase().includes(needle)));
  }, [requestedAlarms, search]);
  const selected = visibleAlarms.find((alarm) => alarm.id === selectedId) ?? visibleAlarms[0] ?? null;
  const loading = view === "active" ? activeQuery.isLoading || acknowledgedQuery.isLoading : historyQuery.isLoading;
  const error = view === "active" ? activeQuery.error || acknowledgedQuery.error : historyQuery.error;

  function changePreset(value: string) {
    setPreset(value); setCursor(undefined); setSelectedId(null);
    if (value !== "custom") { setCustomStart(""); setCustomEnd(""); }
  }

  return <div className="page">
    <PageHeader eyebrow="Operations" title="Events" description="Current alarm conditions and retained lifecycle history. Data is bounded by the existing alarm APIs; no event state is created in the browser." actions={<button className="action-secondary" onClick={() => exportCsv(visibleAlarms, `dcim-events-${view}.csv`)} disabled={visibleAlarms.length === 0}>Export current results CSV</button>} />

    <div className="surface p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="events-tabs" role="tablist" aria-label="Events view">
          <button role="tab" aria-selected={view === "active"} className={view === "active" ? "is-active" : ""} onClick={() => { setView("active"); setSelectedId(null); }}>Active <span>{(activeQuery.data?.length ?? 0) + (acknowledgedQuery.data?.length ?? 0)}</span></button>
          <button role="tab" aria-selected={view === "history"} className={view === "history" ? "is-active" : ""} onClick={() => { setView("history"); setCursor(undefined); setSelectedId(null); }}>History</button>
        </div>
        <div className="flex flex-wrap gap-2">
          {view === "history" && <><select aria-label="History range" className="field w-auto !py-2" value={preset} onChange={(event) => changePreset(event.target.value)}><option value="24">Last 24 hours</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option><option value="custom">Custom range</option></select><select aria-label="Alarm status" className="field w-auto !py-2" value={status} onChange={(event) => { setStatus(event.target.value as StatusFilter); setCursor(undefined); setSelectedId(null); }}><option value="ALL">All states</option><option value="ACTIVE">Active</option><option value="ACKNOWLEDGED">Acknowledged</option><option value="CLEARED">Cleared</option></select></>}
          <input aria-label="Search events" className="field min-w-[220px] !py-2" placeholder="Search subject or identifier" value={search} onChange={(event) => setSearch(event.target.value)} />
        </div>
      </div>
      {view === "history" && preset === "custom" && <div className="mt-3 grid gap-2 border-t border-slate-800 pt-3 sm:grid-cols-2"><label className="text-xs text-slate-400">Start (local timezone)<input aria-label="Events start" type="datetime-local" className="field mt-1" value={customStart} onChange={(event) => { setCustomStart(event.target.value); setCursor(undefined); }} /></label><label className="text-xs text-slate-400">End (local timezone)<input aria-label="Events end" type="datetime-local" className="field mt-1" value={customEnd} onChange={(event) => { setCustomEnd(event.target.value); setCursor(undefined); }} /></label>{!historyWindow.valid && <p className="text-sm text-amber-200 sm:col-span-2">Select a start time before the end time to request history.</p>}</div>}
    </div>

    {loading && <div className="surface-muted p-5 text-sm text-slate-400">Loading requested events…</div>}
    {error && <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">Unable to retrieve the requested event records.</div>}
    {!loading && !error && visibleAlarms.length === 0 && <EmptyState title={view === "active" ? "No active events" : "No events in this range"} detail={view === "active" ? "No active or acknowledged alarm condition is currently reported. This is an intentional quiet-state view." : "Try a different time range or state filter. Historical records remain queried through bounded pages."} />}

    {!loading && !error && visibleAlarms.length > 0 && <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="surface overflow-hidden">
        <div className="overflow-x-auto"><table className="data-table"><thead><tr><th>State</th><th>Subject</th><th>Latest value</th><th>First occurrence</th><th>Acknowledgement</th></tr></thead><tbody>{visibleAlarms.map((alarm) => <tr key={alarm.id} className={selected?.id === alarm.id ? "bg-indigo-500/10" : "cursor-pointer"} onClick={() => setSelectedId(alarm.id)}><td><StatusBadge label={alarm.status} tone={statusTone(alarm.status)} /></td><td><button className="max-w-[280px] truncate text-left font-medium text-slate-200 hover:text-indigo-300">{alarm.subject_key}</button><p className="mt-1 font-mono text-[11px] text-slate-500">{alarm.id.slice(0, 8)}…</p></td><td>{alarm.last_value}</td><td className="whitespace-nowrap text-slate-400">{new Date(alarm.opened_at).toLocaleString()}</td><td className="text-slate-400">{alarm.acknowledged_at ? new Date(alarm.acknowledged_at).toLocaleString() : "Unacknowledged"}</td></tr>)}</tbody></table></div>
        {view === "history" && <div className="flex items-center justify-between border-t border-slate-800 px-4 py-3 text-sm"><span className="text-slate-500">Showing one bounded history page of up to 50 records.</span>{historyQuery.data?.next_cursor && <button className="action-secondary !py-1.5" onClick={() => { setCursor(historyQuery.data?.next_cursor ?? undefined); setSelectedId(null); }}>Next page →</button>}</div>}
      </div>
      {selected && <aside className="surface p-5"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-indigo-300">Event detail</p><div className="mt-4 flex items-center gap-2"><StatusBadge label={selected.status} tone={statusTone(selected.status)} /><span className="text-sm text-slate-400">last value {selected.last_value}</span></div><h2 className="mt-4 break-words text-lg font-semibold text-slate-100">{selected.subject_key}</h2><dl className="mt-5 space-y-3 text-sm"><div><dt>First occurrence</dt><dd>{new Date(selected.opened_at).toLocaleString()}</dd></div><div><dt>Latest recorded state</dt><dd>{selected.cleared_at ? `Cleared ${new Date(selected.cleared_at).toLocaleString()}` : "Not cleared"}</dd></div><div><dt>Acknowledgement</dt><dd>{selected.acknowledged_at ? new Date(selected.acknowledged_at).toLocaleString() : "Unacknowledged"}</dd></div><div><dt>Integration identifier</dt><dd className="break-all font-mono text-xs">{selected.integration_id}</dd></div><div><dt>Rule identifier</dt><dd className="break-all font-mono text-xs">{selected.rule_id}</dd></div></dl>{selected.managed_asset_id && <Link className="mt-5 inline-flex text-sm font-medium text-indigo-300 hover:text-indigo-200" to={`/equipment/${selected.managed_asset_id}`}>Open affected equipment →</Link>}{selected.status === "ACTIVE" && <button className="action-primary mt-5 w-full" disabled={acknowledgeMutation.isPending} onClick={() => acknowledgeMutation.mutate(selected.id)}>Acknowledge active event</button>}{acknowledgeMutation.isError && <p className="mt-2 text-sm text-rose-300">Unable to acknowledge this event. Your permissions may not allow acknowledgement.</p>}</aside>}
    </div>}
  </div>;
}
