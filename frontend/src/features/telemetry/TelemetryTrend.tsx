import { TelemetryReading } from "@/features/telemetry/api";

/** Compact bounded SVG trend: history is already limited server-side, so this never
 * asks the browser to render an unbounded telemetry stream. */
export function TelemetryTrend({ points }: { points: TelemetryReading[] }) {
  if (points.length < 2) return null;
  if (new Set(points.map((point) => point.presentation_unit)).size > 1) {
    return <p className="mb-3 text-xs text-slate-400">This history contains multiple units. Review the values and units below.</p>;
  }
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
