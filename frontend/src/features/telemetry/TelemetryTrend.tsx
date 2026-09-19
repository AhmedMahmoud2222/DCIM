import { TelemetryReading } from "@/features/telemetry/api";

/** Compact bounded SVG trend: history is already limited server-side, so this never
 * asks the browser to render an unbounded telemetry stream. */
export function TelemetryTrend({ points }: { points: TelemetryReading[] }) {
  if (points.length < 2) return null;
  const values = points.map((point) => point.value);
  const min = Math.min(...values); const max = Math.max(...values); const range = max - min || 1;
  const path = points.map((point, index) => {
    const x = (index / (points.length - 1)) * 100;
    const y = 44 - ((point.value - min) / range) * 40;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return <svg viewBox="0 0 100 48" role="img" aria-label={`Trend from ${min} to ${max}`} className="mb-3 h-24 w-full rounded bg-slate-950">
    <title>Metric trend: minimum {min}, maximum {max}</title><path d={path} fill="none" stroke="#38bdf8" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
  </svg>;
}
