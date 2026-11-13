import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { deriveFreshness } from "@/features/status/freshness";
import { MeasurementFreshness } from "@/features/status/StatusBadges";
import { apiFetch } from "@/lib/apiClient";
import { useNow } from "@/lib/useNow";
import { Equipment, Page, Site } from "@/types";
import { TelemetryReading } from "./api";
import { getPerformance, performanceCsv } from "./performance";
import { PerformanceChart } from "./PerformanceChart";

const DAY = 86_400_000;
const control = "min-h-10 rounded border border-slate-600 bg-surface-inset px-3 py-1";
const ranges = [{ label: "24h", days: 1 }, { label: "7d", days: 7 }, { label: "30d", days: 30 }, { label: "90d", days: 90 }];
function identity(s: Pick<TelemetryReading, "series_key" | "integration_id" | "external_identifier" | "unit" | "registry_version" | "metric">) {
  return JSON.stringify([s.series_key, s.integration_id, s.external_identifier, s.unit, s.registry_version, s.metric]);
}
function useChoices<T>(path: string, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: ["telemetry-choices",path], enabled, initialPageParam: 0,
    queryFn: ({ pageParam }) => apiFetch<Page<T>>(`${path}${path.includes("?") ? "&" : "?"}limit=200&offset=${pageParam}`),
    getNextPageParam: page => page.items.length && page.offset + page.items.length < page.total ? page.offset + page.items.length : undefined,
  });
}

function presetWindow(days: number) {
  const end = Date.now();
  return { start: new Date(end - days * DAY).toISOString(), end: new Date(end).toISOString() };
}

export function TelemetryPage() {
  const allowed = useHasPermission("telemetry:read");
  return allowed ? <PerformanceViewer /> : <p role="alert">Telemetry read access is required.</p>;
}

function PerformanceViewer() {
  const [params] = useSearchParams();
  const [site, setSite] = useState("");
  const [asset, setAsset] = useState(params.get("equipment") ?? "");
  const [metric, setMetric] = useState(params.get("metric") ?? "");
  const [selectedSeries, setSelectedSeries] = useState("");
  const [preset, setPreset] = useState("24h");
  const [window, setWindow] = useState(() => presetWindow(1));
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const now = useNow(5_000);
  const sites = useChoices<Site>("/sites", useHasPermission("location:read"));
  const equipment = useChoices<Equipment>(site ? `/equipment?site_id=${encodeURIComponent(site)}` : "/equipment", useHasPermission("equipment:read"));
  const equipmentOptions = equipment.data?.pages.flatMap(p => p.items) ?? [];
  const live = useQuery({
    queryKey: ["performance-live",asset,site], enabled: !!asset, refetchInterval: 5_000,
    queryFn: () => apiFetch<TelemetryReading[]>(`/telemetry/latest?${new URLSearchParams({ managed_asset_id: asset, ...(site ? {site_id:site} : {}), per_series:"true", limit:"100" })}`),
  });
  const recent = new Map<string, TelemetryReading>();
  for (const r of live.data ?? []) {
    const key = identity(r);
    if (!recent.has(key)) recent.set(key,r);
  }
  const liveMetrics = Array.from(new Set(Array.from(recent.values(), r => r.metric)));
  const currentMetric = metric || liveMetrics[0] || "";
  const start = preset === "custom" ? Date.parse(customStart + ":00Z") : Date.parse(window.start);
  const end = preset === "custom" ? Date.parse(customEnd + ":00Z") : Date.parse(window.end);
  const valid = Number.isFinite(start) && Number.isFinite(end) && start < end && end - start <= 366 * DAY;
  const from = valid ? new Date(start).toISOString() : "";
  const to = valid ? new Date(end).toISOString() : "";
  const history = useQuery({
    queryKey: ["performance-history",asset,site,currentMetric,from,to],
    enabled: Boolean(asset && currentMetric && valid),
    queryFn: () => getPerformance(asset,site,currentMetric,from,to),
  });
  const matching = (history.data?.series ?? []).filter(s => !selectedSeries || identity(s) === selectedSeries);
  const [exportError,setExportError] = useState("");
  const [exporting,setExporting] = useState(false);
  async function exportChart() {
    setExporting(true);
    setExportError("");
    try {
      const response = await getPerformance(asset,site,currentMetric,from,to);
      const rows = response.series.filter(s => !selectedSeries || identity(s) === selectedSeries);
      const url = URL.createObjectURL(new Blob([performanceCsv(rows,asset,site,from,to)], {type:"text/csv;charset=utf-8"}));
      const link = document.createElement("a");
      link.href = url;
      link.download = "telemetry-history.csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setExportError("Export failed. Check access and retry."); }
    finally { setExporting(false); }
  }
  function chooseAsset(id: string) {
    setAsset(id);
    setMetric("");
    setSelectedSeries("");
    setExportError("");
  }
  return <div className="space-y-5">
    <h1 className="text-2xl font-semibold">Telemetry</h1>
    <div className="flex flex-wrap items-end gap-3">
      <label className="grid gap-1">Site<select className={control} value={site} onChange={e => {setSite(e.target.value); chooseAsset("");}}>
        <option value="">All accessible sites</option>
        {sites.data?.pages.flatMap(p => p.items).map(s => <option key={s.id} value={s.id}>{s.name} ({s.code})</option>)}
      </select></label>
      {sites.hasNextPage && <button className={control} onClick={() => void sites.fetchNextPage()}>Load more sites</button>}
      <label className="grid gap-1">Equipment<select className={control} value={asset} onChange={e => chooseAsset(e.target.value)}>
        <option value="">Select equipment</option>
        {asset && !equipmentOptions.some(e => e.id === asset) && <option value={asset}>{asset}</option>}
        {equipmentOptions.map(e => <option key={e.id} value={e.id}>{e.asset_tag}{e.hostname ? " · " + e.hostname : ""}</option>)}
      </select></label>
      {equipment.hasNextPage && <button className={control} onClick={() => void equipment.fetchNextPage()}>Load more equipment</button>}
    </div>
    {(sites.isError || equipment.isError) && <p role="alert">Some filter options could not be loaded.</p>}
    {!asset ? <p>Select equipment to inspect its telemetry.</p> : <>
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Live readings</h2>
        {live.isPending && <p role="status">Loading live readings…</p>}
        {live.isError && <p role="alert">Live readings could not be refreshed.</p>}
        {live.data && <p className="text-sm">Updated {new Date(live.dataUpdatedAt).toISOString()} · refreshes every 5 seconds</p>}
        {live.data?.length === 0 && <p>No live readings for this equipment.</p>}
        {live.data?.length === 100 && <p>Showing the 100 most recently updated series. Older series may be omitted.</p>}
        <div className="flex flex-wrap gap-2">{Array.from(recent, ([key,r]) => <button key={key} aria-pressed={selectedSeries === key} className={control + " text-left aria-pressed:ring-2 aria-pressed:ring-focus"} onClick={() => {setMetric(r.metric); setSelectedSeries(key);}}>
          <span className="block">{r.metric} · {r.external_identifier}</span>
          <strong>{r.value} {r.unit}</strong>
          <span className="block text-sm">{r.occurred_at} · <MeasurementFreshness result={deriveFreshness({ sourceType:"equipment_live_metric", measuredAt:r.occurred_at, expectedIntervalSeconds:r.expected_poll_interval_seconds, now })} /></span>
        </button>)}</div>
      </section>
      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Performance history</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-1">
            <label htmlFor="history-metric">Metric</label>
            <input id="history-metric" aria-describedby="metric-hint" className={control} list="telemetry-metrics" value={metric} placeholder={liveMetrics[0]} onChange={e => {setMetric(e.target.value);setSelectedSeries("");}} />
            <small id="metric-hint">Select a live metric or enter a historical metric name.</small>
          </div>
          <datalist id="telemetry-metrics">{liveMetrics.map(m => <option key={m} value={m}/>)}</datalist>
          <label className="grid gap-1">Range<select className={control} value={preset} onChange={e => {
            setPreset(e.target.value);
            const choice = ranges.find(r => r.label === e.target.value);
            if (choice) setWindow(presetWindow(choice.days));
            else {setCustomStart(window.start.slice(0,16));setCustomEnd(window.end.slice(0,16));}
          }}>{ranges.map(r => <option key={r.label}>{r.label}</option>)}<option value="custom">Custom</option></select></label>
          {preset !== "custom" && <button className={control} onClick={() => setWindow(presetWindow(ranges.find(r => r.label === preset)!.days))}>Update window to now</button>}
          {preset === "custom" && <><label className="grid gap-1">Start (UTC)<input type="datetime-local" className={control} value={customStart} onChange={e => setCustomStart(e.target.value)}/></label>
            <label className="grid gap-1">End (UTC)<input type="datetime-local" className={control} value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></label></>}
          {selectedSeries && <button className={control} onClick={() => setSelectedSeries("")}>Show all series for {currentMetric}</button>}
          <button className={control} disabled={exporting || !matching.length || history.isError || !valid} onClick={() => void exportChart()}>{exporting ? "Exporting…" : "Export chart buckets (CSV)"}</button>
        </div>
        {exportError && <p role="alert">{exportError}</p>}
        {!valid && <p role="alert">Choose an end after the start and a range of up to 366 days.</p>}
        {valid && currentMetric && <p className="text-sm">{currentMetric} · {from} to {to} (end exclusive). Charts and CSV use up to 300 buckets per series.</p>}
        {history.isFetching && <p role="status">Loading performance history…</p>}
        {history.isError && <div><p role="alert">{history.error.message}</p><button className={control} onClick={() => void history.refetch()}>Retry history</button></div>}
        {history.data && !history.isError && !matching.length && <p>No history for this selection.</p>}
        {!history.isError && matching.map(s => <PerformanceChart key={identity(s)} series={s} start={from} end={to} pollSeconds={recent.get(identity(s))?.expected_poll_interval_seconds}/>)}
      </section>
    </>}
  </div>;
}
