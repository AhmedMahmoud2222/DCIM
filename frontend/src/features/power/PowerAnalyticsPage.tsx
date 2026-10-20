import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { listAllSites } from "@/features/access/api";
import {
  Forecast,
  NodeRollup,
  ProtectionDevice,
  ProtectionState,
  Quality,
  ReportJob,
  Snapshot,
  createReport,
  downloadReport,
  getForecast,
  getHistory,
  getRollup,
  listProtectionDevices,
  listReports,
  setProtectionState,
} from "@/features/power/analyticsApi";
import { ApiError } from "@/lib/apiClient";

type Tab = "capacity" | "protection" | "history" | "reports";
const TABS: { id: Tab; label: string }[] = [
  { id: "capacity", label: "Capacity" },
  { id: "protection", label: "Protection" },
  { id: "history", label: "History and forecast" },
  { id: "reports", label: "Reports" },
];

const QUALITY_TEXT: Record<Quality, string> = {
  measured: "Measured",
  stale: "Stale",
  estimated: "Estimated",
  mixed: "Mixed",
  missing: "No data",
};
const QUALITY_STYLE: Record<Quality, string> = {
  measured: "bg-green-900 text-green-200",
  stale: "bg-amber-900 text-amber-200",
  estimated: "bg-sky-900 text-sky-200",
  mixed: "bg-amber-900 text-amber-200",
  missing: "bg-slate-700 text-slate-300",
};

export function QualityBadge({ quality }: { quality: Quality }) {
  return <span className={`rounded-sm px-1.5 py-0.5 text-xs ${QUALITY_STYLE[quality]}`}>{QUALITY_TEXT[quality]}</span>;
}

const FORECAST_TEXT: Record<Forecast["status"], string> = {
  good: "Forecast available",
  flat: "No growth expected",
  already_over_capacity: "Already over capacity",
  stale: "Data is stale",
  sparse: "Not enough data",
  missing: "No data",
  estimated_mixed: "Too much estimated data",
};

function fmtKw(v: number | null | undefined): string {
  return v === null || v === undefined ? "n/a" : `${v.toFixed(2)} kW`;
}
function fmtPct(v: number | null): string {
  return v === null ? "n/a" : `${v.toFixed(1)}%`;
}
function errorText(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : "Request failed";
}

function ErrorLine({ error }: { error: unknown }) {
  return (
    <p role="alert" className="text-sm text-red-400">
      {errorText(error)}
    </p>
  );
}

const LEVEL_TEXT: Record<NodeRollup["level"], string> = {
  ok: "OK",
  warning: "Warning",
  critical: "Critical",
  overload: "Overload",
  unknown: "Unknown",
};

function CapacityTab({ siteId }: { siteId: string }) {
  const q = useQuery({ queryKey: ["power-analytics", "rollup", siteId], queryFn: () => getRollup(siteId) });
  if (q.isPending) return <p role="status">Loading capacity.</p>;
  if (q.isError) return <ErrorLine error={q.error} />;
  const r = q.data;
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
        <div><dt className="text-slate-400">Load</dt><dd data-testid="site-load">{fmtKw(r.site.load_kw)}</dd></div>
        <div><dt className="text-slate-400">Capacity</dt><dd>{fmtKw(r.site.capacity_kw)}</dd></div>
        <div><dt className="text-slate-400">Headroom</dt><dd>{fmtKw(r.site.headroom_kw)}</dd></div>
        <div><dt className="text-slate-400">Utilization</dt><dd>{fmtPct(r.site.utilization_pct)}</dd></div>
        <div><dt className="text-slate-400">Data quality</dt><dd><QualityBadge quality={r.site.quality} /></dd></div>
      </dl>
      {r.site.unserved_kw > 0 && (
        <p role="alert" className="text-sm text-amber-300">
          {fmtKw(r.site.unserved_kw)} of demand has no live power path.
        </p>
      )}
      {r.warnings.length > 0 && (
        <section aria-label="Redundancy and path warnings">
          <h3 className="mb-1 text-sm font-semibold">Warnings</h3>
          <ul className="list-disc pl-5 text-sm text-amber-300">
            {r.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        </section>
      )}
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Power nodes</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Node</th><th scope="col">Type</th><th scope="col">Load</th><th scope="col">Allocated</th>
            <th scope="col">Capacity</th><th scope="col">Headroom</th><th scope="col">Utilization</th>
            <th scope="col">Status</th><th scope="col">Data</th>
          </tr>
        </thead>
        <tbody>
          {r.nodes.map((n) => (
            <tr key={n.id} className="border-t border-slate-800">
              <th scope="row" className="font-normal">{n.label}</th>
              <td>{n.node_type.replace(/_/g, " ")}</td>
              <td>{fmtKw(n.load_kw)}</td>
              <td>{fmtKw(n.allocated_kw)}</td>
              <td>{fmtKw(n.capacity_kw)}</td>
              <td className={n.headroom_kw !== null && n.headroom_kw < 0 ? "text-red-400" : ""}>{fmtKw(n.headroom_kw)}</td>
              <td>{fmtPct(n.utilization_pct)}</td>
              <td>{LEVEL_TEXT[n.level]}{n.protection_state ? `, ${n.protection_state}` : ""}</td>
              <td><QualityBadge quality={n.quality} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Equipment redundancy</caption>
        <thead className="text-slate-400">
          <tr><th scope="col">Equipment</th><th scope="col">Scenario</th><th scope="col">Live feeds</th><th scope="col">Demand</th><th scope="col">Data</th></tr>
        </thead>
        <tbody>
          {r.equipment.map((e) => (
            <tr key={e.id} className="border-t border-slate-800">
              <th scope="row" className="font-normal">{e.label}</th>
              <td>{e.scenario.replace(/_/g, " ")}</td>
              <td>{e.live_inlets} of {e.inlet_count}</td>
              <td>{fmtKw(e.demand_kw)}</td>
              <td><QualityBadge quality={e.quality} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const STATES: ProtectionState[] = ["closed", "open", "tripped", "unknown"];

function ProtectionTab({ siteId }: { siteId: string }) {
  const canManage = useHasPermission("power:manage");
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["power-analytics", "devices", siteId], queryFn: () => listProtectionDevices(siteId) });
  const m = useMutation({
    mutationFn: (v: { d: ProtectionDevice; state: ProtectionState }) => setProtectionState(v.d.id, v.state, v.d.version),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["power-analytics"] }),
  });
  if (q.isPending) return <p role="status">Loading protection devices.</p>;
  if (q.isError) return <ErrorLine error={q.error} />;
  if (q.data.items.length === 0) return <p>No protection devices at this site.</p>;
  return (
    <div className="space-y-2">
      {m.isError && <ErrorLine error={m.error} />}
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Breakers and protection devices</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Device</th><th scope="col">Type</th><th scope="col">Rating</th><th scope="col">Rated power</th>
            <th scope="col">Poles</th><th scope="col">Links</th><th scope="col">State</th><th scope="col">Change state</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((d) => (
            <tr key={d.id} className="border-t border-slate-800">
              <th scope="row" className="font-normal">{d.label}</th>
              <td>{d.device_type}</td>
              <td>{d.rating_a} A at {d.voltage_v} V</td>
              <td>{fmtKw(d.rated_kw)}</td>
              <td>{d.poles} ({d.phase_config})</td>
              <td>{d.upstream_node_ids.length} up, {d.downstream_node_ids.length} down</td>
              <td data-testid={`state-${d.label}`}>{d.state}{d.state === "unknown" ? " (not reported)" : ""}</td>
              <td>
                {canManage ? (
                  <select
                    aria-label={`State for ${d.label}`}
                    value={d.state}
                    disabled={m.isPending || d.retired_at !== null}
                    onChange={(e) => m.mutate({ d, state: e.target.value as ProtectionState })}
                    className="rounded-sm border border-slate-700 bg-slate-800 px-1 py-0.5 text-xs"
                  >
                    {STATES.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                ) : (
                  <span className="text-slate-500">Read only</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TrendChart({ points }: { points: Snapshot[] }) {
  const loads = points.filter((p) => p.load_kw !== null);
  if (loads.length < 2) return <p className="text-sm text-slate-400">Not enough history to draw a trend.</p>;
  const vals = loads.map((p) => p.load_kw as number);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const d = loads
    .map((p, i) => `${i === 0 ? "M" : "L"}${((i / (loads.length - 1)) * 100).toFixed(2)},${(44 - (((p.load_kw as number) - min) / range) * 40).toFixed(2)}`)
    .join(" ");
  return (
    <svg viewBox="0 0 100 48" role="img" aria-label={`Load trend from ${min.toFixed(2)} to ${max.toFixed(2)} kW over ${loads.length} hours`} className="h-28 w-full rounded-sm bg-slate-950">
      <path d={d} fill="none" stroke="#38bdf8" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function ForecastPanel({ f }: { f: Forecast }) {
  return (
    <section aria-label="Forecast" className="rounded-sm border border-slate-800 p-3 text-sm">
      <h3 className="mb-1 font-semibold">Forecast: <span data-testid="forecast-status">{FORECAST_TEXT[f.status]}</span></h3>
      {f.no_forecast_reason && <p className="mb-2 text-slate-300">Reason: {f.no_forecast_reason}</p>}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div><dt className="text-slate-400">Current load</dt><dd>{fmtKw(f.current_load_kw)}</dd></div>
        <div><dt className="text-slate-400">Capacity</dt><dd>{fmtKw(f.capacity_kw)}</dd></div>
        <div><dt className="text-slate-400">Headroom</dt><dd>{fmtKw(f.headroom_kw)}</dd></div>
        <div><dt className="text-slate-400">Exhaustion date</dt><dd>{f.exhaustion_date ? new Date(f.exhaustion_date).toISOString().slice(0, 10) : "none projected"}</dd></div>
        <div><dt className="text-slate-400">Method</dt><dd>{f.method}</dd></div>
        <div><dt className="text-slate-400">Confidence</dt><dd>{f.confidence}</dd></div>
        <div><dt className="text-slate-400">Input window</dt><dd>{f.window_start.slice(0, 10)} to {f.window_end.slice(0, 10)}</dd></div>
        <div><dt className="text-slate-400">Samples</dt><dd>{f.sample_count} in {f.day_count} days</dd></div>
      </dl>
    </section>
  );
}

function HistoryTab({ siteId }: { siteId: string }) {
  const h = useQuery({ queryKey: ["power-analytics", "history", siteId], queryFn: () => getHistory("site", siteId) });
  const f = useQuery({ queryKey: ["power-analytics", "forecast", siteId], queryFn: () => getForecast("site", siteId) });
  return (
    <div className="space-y-4">
      {f.isPending ? <p role="status">Loading forecast.</p> : f.isError ? <ErrorLine error={f.error} /> : <ForecastPanel f={f.data} />}
      {h.isPending ? <p role="status">Loading history.</p> : h.isError ? <ErrorLine error={h.error} /> : (
        <>
          <TrendChart points={h.data.items} />
          <table className="w-full text-left text-sm">
            <caption className="mb-1 text-left text-sm font-semibold">Hourly utilization, last 7 days</caption>
            <thead className="text-slate-400"><tr><th scope="col">Hour</th><th scope="col">Load</th><th scope="col">Headroom</th><th scope="col">Samples</th><th scope="col">Data</th></tr></thead>
            <tbody>
              {h.data.items.slice(-24).map((s) => (
                <tr key={s.bucket_start} className="border-t border-slate-800">
                  <th scope="row" className="font-normal">{s.bucket_start.slice(0, 16).replace("T", " ")}</th>
                  <td>{fmtKw(s.load_kw)}</td><td>{fmtKw(s.headroom_kw)}</td>
                  <td>{s.sample_count} of {s.expected_samples}</td>
                  <td><QualityBadge quality={s.quality} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {h.data.items.length === 0 && <p>No snapshots recorded yet for this site.</p>}
        </>
      )}
    </div>
  );
}

const JOB_TEXT: Record<ReportJob["status"], string> = { queued: "Queued", running: "Running", completed: "Ready", failed: "Failed" };

function ReportsTab({ siteId }: { siteId: string }) {
  const canManage = useHasPermission("power:manage");
  const qc = useQueryClient();
  const [format, setFormat] = useState<"json" | "csv">("csv");
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const jobs = useQuery({
    queryKey: ["power-analytics", "reports"],
    queryFn: listReports,
    refetchInterval: (query) =>
      query.state.data?.items.some((j) => j.status === "queued" || j.status === "running") ? 2000 : false,
  });
  const create = useMutation({
    mutationFn: () => createReport(siteId, format),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["power-analytics", "reports"] }),
  });
  async function download(job: ReportJob) {
    setDownloadError(null);
    try {
      const blob = await downloadReport(job.id, job.format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `power-report-${job.id}.${job.format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError(errorText(e));
    }
  }
  return (
    <div className="space-y-3">
      {canManage && (
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <label className="text-sm">
            <span className="mr-2 text-slate-400">Format</span>
            <select value={format} onChange={(e) => setFormat(e.target.value as "json" | "csv")} className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm">
              <option value="csv">CSV</option>
              <option value="json">JSON</option>
            </select>
          </label>
          <button type="submit" disabled={create.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
            Generate report
          </button>
        </form>
      )}
      {create.isError && <ErrorLine error={create.error} />}
      {downloadError && <p role="alert" className="text-sm text-red-400">{downloadError}</p>}
      {jobs.isPending ? <p role="status">Loading reports.</p> : jobs.isError ? <ErrorLine error={jobs.error} /> : jobs.data.items.length === 0 ? (
        <p>No reports yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <caption className="mb-1 text-left text-sm font-semibold">Your reports</caption>
          <thead className="text-slate-400"><tr><th scope="col">Requested</th><th scope="col">Format</th><th scope="col">Status</th><th scope="col">Rows</th><th scope="col">Download</th></tr></thead>
          <tbody aria-live="polite">
            {jobs.data.items.map((j) => (
              <tr key={j.id} className="border-t border-slate-800">
                <th scope="row" className="font-normal">{j.created_at.slice(0, 16).replace("T", " ")}</th>
                <td>{j.format.toUpperCase()}</td>
                <td>{JOB_TEXT[j.status]}{j.failure_code ? ` (${j.failure_code})` : ""}</td>
                <td>{j.row_count ?? "n/a"}</td>
                <td>
                  {j.status === "completed" ? (
                    <button type="button" onClick={() => download(j)} className="rounded-sm bg-slate-800 px-2 py-0.5 text-xs hover:bg-slate-700">
                      Download {j.format.toUpperCase()}
                    </button>
                  ) : <span className="text-slate-500">Not ready</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function PowerAnalyticsPage() {
  const sites = useQuery({ queryKey: ["sites", "all"], queryFn: listAllSites });
  const [siteId, setSiteId] = useState("");
  const [tab, setTab] = useState<Tab>("capacity");
  const effective = siteId || sites.data?.items[0]?.id || "";
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Power capacity and forecast</h1>
        <Link to="/power" className="text-sm text-sky-400 underline">Back to topology</Link>
      </div>
      <p className="mb-4 max-w-2xl text-sm text-slate-400">
        Load comes from canonical power telemetry in kW. Where a device has no recent reading the figure is a nameplate
        estimate and says so. A and B feeds share one server&apos;s demand; they are never added together.
      </p>
      <label className="mb-4 block text-sm">
        <span className="mr-2 text-slate-400">Site</span>
        <select value={effective} onChange={(e) => setSiteId(e.target.value)} className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm">
          {sites.data?.items.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </label>
      <div role="tablist" aria-label="Power analytics views" className="mb-4 flex gap-1 border-b border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-sm ${tab === t.id ? "border-b-2 border-sky-400 text-slate-100" : "text-slate-400"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {!effective ? (
          <p>{sites.isError ? "Sites could not be loaded." : "No sites available."}</p>
        ) : tab === "capacity" ? <CapacityTab siteId={effective} />
          : tab === "protection" ? <ProtectionTab siteId={effective} />
          : tab === "history" ? <HistoryTab siteId={effective} />
          : <ReportsTab siteId={effective} />}
      </div>
    </div>
  );
}
