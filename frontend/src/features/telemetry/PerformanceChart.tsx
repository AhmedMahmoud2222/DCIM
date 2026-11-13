import { useQuery } from "@tanstack/react-query";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { AlarmRule, listAlarmRules } from "./alarmRulesApi";
import { PerformanceSeries } from "./performance";

const format = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 });
const display = (value: number | null) => value == null ? "Unavailable" : format.format(value);

async function integrationRules(integrationId: string) {
  const rules: AlarmRule[] = [];
  let page: AlarmRule[];
  do {
    page = await listAlarmRules({ integrationId, offset: rules.length, limit: 500 });
    rules.push(...page);
  } while (page.length === 500);
  return rules;
}

export function PerformanceChart({ series, start, end }: { series: PerformanceSeries; start: string; end: string }) {
  const canReadRules = useHasPermission("alarm:read");
  const rules = useQuery({ queryKey: ["telemetry-thresholds", series.integration_id], queryFn: () => integrationRules(series.integration_id), enabled: canReadRules });
  const applicable = (rules.data ?? []).filter(r => r.enabled && !r.needs_attention && r.metric === series.metric && r.unit === series.unit &&
    r.registry_version === series.registry_version && r.threshold !== null && ["threshold_low", "threshold_high"].includes(r.rule_type));
  const low = series.minimum ?? 0;
  const high = series.maximum ?? low;
  const from = Date.parse(start);
  const duration = Date.parse(end) - from;
  const x = (time: string) => 5 + 990 * (Date.parse(time) - from) / duration;
  const y = (value: number) => high === low ? 50 : 95 - 90 * (value - low) / (high - low);
  const stats = [["Minimum", series.minimum], ["Maximum", series.maximum], ["Average", series.average], ["P50", series.p50], ["P95", series.p95]] as const;
  return <section className="space-y-3 rounded border border-line-subtle bg-surface-panel p-4 text-fg-primary">
    <h2 className="break-all font-semibold">{series.metric} ({series.unit}) · {series.external_identifier}</h2>
    <p className="break-all text-sm">Series {series.series_key} · {series.sample_count} samples</p>
    <dl className="flex flex-wrap gap-5">{stats.map(([name,value]) => <div key={name}><dt className="text-sm font-medium text-fg-secondary">{name}</dt><dd className="font-semibold">{display(value)}</dd></div>)}</dl>
    {series.p95 !== null && <p className="text-sm">P50 and P95 use linear interpolation across raw samples in the full window.</p>}
    {series.partial_days > 0 && <p>{series.partial_days} partially covered UTC day(s) have retained aggregates excluded from these statistics.</p>}
    {series.p95 == null && <p>Exact percentiles are unavailable for this selection.</p>}
    {series.points.length > 0 && <figure>
      <figcaption>Average by time bucket ({series.unit}). Blank intervals have no eligible readings.</figcaption>
      <svg viewBox="0 0 1000 100" preserveAspectRatio="none" role="img" aria-label={`Time series of ${series.metric} in ${series.unit}; average by UTC time bucket`} className="h-36 w-full bg-surface-inset">
        {series.points.map((p,i) => {
          const prev = series.points[i-1];
          const center = (x(p.start) + x(p.end)) / 2;
          return <g key={p.start}>
            {prev && Date.parse(prev.end) === Date.parse(p.start) && <line x1={(x(prev.start)+x(prev.end))/2} y1={y(prev.average)} x2={center} y2={y(p.average)} stroke="var(--viz-accent)" strokeWidth="2" vectorEffect="non-scaling-stroke" />}
            <circle cx={center} cy={y(p.average)} r="2.5" fill="var(--viz-accent)"><title>{p.start} — {display(p.average)} {series.unit}</title></circle>
          </g>;
        })}
      </svg>
      <div className="flex flex-wrap justify-between text-xs"><time>{start}</time><time>{end}</time></div>
    </figure>}
    <p className="text-sm">{!canReadRules ? "Thresholds require alarm read access." : rules.isPending ? "Loading thresholds…" : rules.isError ? "Thresholds unavailable." : applicable.length ? "Compared with current configured thresholds. Historical rule settings are unavailable." : "No comparable current thresholds."}</p>
    {applicable.map(r => {
      const breached = r.rule_type === "threshold_high" ? high > r.threshold! : low < r.threshold!;
      return <p key={r.id}>{r.name} · {r.rule_type === "threshold_high" ? "Above" : "Below"} {r.threshold} {series.unit} · {series.sample_count ? breached ? "Breached in window" : "No observed breach" : "No samples"}</p>;
    })}
    <details><summary>View {series.points.length} observed buckets</summary>
      <div role="region" aria-label="Telemetry bucket values" tabIndex={0} className="max-h-80 overflow-auto"><table className="w-full text-left text-sm"><caption className="sr-only">UTC bucket values in {series.unit}</caption><thead><tr>{["UTC start","UTC end","Resolution","Count","Min","Max","Average"].map(h => <th scope="col" key={h}>{h}</th>)}</tr></thead>
        <tbody>{series.points.map(p => <tr key={p.start}><td>{p.start}</td><td>{p.end}</td><td>{p.resolution}</td><td>{p.sample_count}</td><td>{display(p.minimum)}</td><td>{display(p.maximum)}</td><td>{display(p.average)}</td></tr>)}</tbody>
      </table></div>
    </details>
  </section>;
}
