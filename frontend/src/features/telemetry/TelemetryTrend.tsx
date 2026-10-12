import { TelemetryReading } from "@/features/telemetry/api";

const labels = {
  pinned: "Pinned", inferred: "Inferred", operator_resolved: "Operator resolved",
  unverified_registry: "Unverified registry", unverified_legacy: "Unverified legacy",
};

export function provenanceLabel(point: TelemetryReading): string {
  if (point.resolution === "daily") return "Aggregate provenance pending";
  return point.provenance_status ? labels[point.provenance_status] : "Provenance unavailable";
}

/** Keep distinct stored contracts separate. No conversion or precision claim is made here. */
export function groupHistoryPoints(points: TelemetryReading[]): TelemetryReading[][] {
  const groups = new Map<string, TelemetryReading[]>();
  for (const point of points) {
    // Missing series identity cannot justify combining independent sources. Aggregate provenance
    // is deliberately not inferred from its registry version (the lineage phase is separate).
    const key = JSON.stringify([
      point.series_key ?? point.id, point.provenance_status ?? "unavailable", point.unit,
      point.registry_version ?? null, point.presentation_unit, point.resolution ?? "raw",
      point.resolution === "daily" ? null : point.mapping_revision_id ?? null,
    ]);
    const group = groups.get(key) ?? [];
    group.push(point);
    groups.set(key, group);
  }
  return [...groups.values()];
}

function GroupTrend({ points }: { points: TelemetryReading[] }) {
  if (points.length < 2) return <p className="text-xs text-slate-400">One history point; no trend line.</p>;
  const values = points.map((point) => point.presentation_value);
  const unit = points[0].presentation_unit;
  const min = Math.min(...values); const max = Math.max(...values); const range = max - min || 1;
  const path = points.map((point, index) => {
    const x = (index / (points.length - 1)) * 100;
    const y = 44 - ((point.presentation_value - min) / range) * 40;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return <svg viewBox="0 0 100 48" role="img" aria-label={`Trend from ${min} to ${max} ${unit}`} className="mb-3 h-24 w-full rounded-sm bg-slate-950">
    <title>Metric trend: minimum {min} {unit}, maximum {max} {unit}</title><path d={path} fill="none" stroke="#38bdf8" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
  </svg>;
}

/** Flat bounded history is grouped in the client; every group has its own summary and axis. */
export function TelemetryTrend({ points }: { points: TelemetryReading[] }) {
  if (!points.length) return null;
  const groups = groupHistoryPoints(points);
  const mixedUnits = new Set(points.map((point) => point.presentation_unit)).size > 1;
  return <div>
    {groups.length > 1 && <p className="mb-3 text-xs text-amber-300">Mixed provenance or distinct series: each group is shown separately.</p>}
    {mixedUnits && <p className="mb-3 text-xs text-slate-400">This history contains multiple units. Review the values and units below.</p>}
    {groups.map((group) => <section key={group[0].id} className="mb-3" aria-label={`${provenanceLabel(group[0])} history group`}>
      <p className="text-xs text-slate-300"><span title="Contract provenance records how Central interpreted a reading; it does not certify a physical sensor.">{provenanceLabel(group[0])}</span> <span className="text-slate-400">{group[0].series_key ?? "Series unavailable"}</span></p>
      {/* Preserve the existing mixed-presentation-unit guard for the whole response. */}
      {!mixedUnits && <GroupTrend points={group} />}
    </section>)}
  </div>;
}
