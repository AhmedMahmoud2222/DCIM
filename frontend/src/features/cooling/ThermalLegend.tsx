import type { HeatMap, ThermalMetric } from "@/types";
import { SENSOR_STATE_META, legendTicks, metricLabel, ramp, scaleBounds } from "./thermalMath";

const gradient = (metric: ThermalMetric) => `linear-gradient(to right, ${ramp(metric).map((c, i, a) => `rgb(${c.join(" ")}) ${Math.round((i / (a.length - 1)) * 100)}%`).join(", ")})`;

/** Legend with text for every encoding. Colour is never the only cue: states have glyphs and labels, interpolated
 * cells are labelled as such, and airflow provenance is spelled out. */
export function ThermalLegend({ map }: { map: HeatMap | null | undefined }) {
  const bounds = map ? scaleBounds(map) : null;
  const unit = map?.presentation_unit ?? "";
  return (
    <div className="mt-3 grid gap-4 text-xs text-slate-400 md:grid-cols-2" data-testid="thermal-legend">
      <section aria-label="Colour scale">
        <p className="mb-1 font-medium text-slate-300">{map ? metricLabel(map.metric) : "Value"} scale ({unit || "—"})</p>
        {map && bounds ? (
          <>
            <div className="h-3 w-full max-w-xs rounded-sm border border-slate-700" style={{ background: gradient(map.metric) }} role="img" aria-label={`Colour scale from ${bounds.min} to ${bounds.max} ${unit}`} />
            <ol className="mt-1 flex max-w-xs justify-between" data-testid="legend-ticks">
              {legendTicks(bounds.min, bounds.max).map((t, i) => (
                // position on the scale is the identity: a flat field repeats the same value at every position
                <li key={`tick-${i}`}>{t}</li>
              ))}
            </ol>
            <p className="mt-1" data-testid="legend-range">
              Minimum {bounds.min} {unit}, maximum {bounds.max} {unit}
            </p>
          </>
        ) : (
          <p data-testid="legend-no-scale">No valid values to scale yet.</p>
        )}
        {map && map.thresholds.length > 0 && (
          <ul className="mt-1" data-testid="legend-thresholds">
            {map.thresholds.map((t) => (
              <li key={`${t.rule_type}-${t.threshold}`}>
                {t.rule_type === "threshold_high" ? "High" : "Low"} alarm threshold {t.threshold} {t.unit ?? unit} ({t.name})
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Data provenance and state">
        <p className="mb-1 font-medium text-slate-300">What you are looking at</p>
        <ul className="space-y-0.5">
          <li data-testid="legend-interpolated">
            <span className="inline-block h-3 w-4 align-middle" style={{ background: "linear-gradient(90deg, rgb(59 130 246 / .55), rgb(239 68 68 / .55))" }} /> Tinted cells: interpolated from sensors, not measured
          </li>
          {(Object.keys(SENSOR_STATE_META) as (keyof typeof SENSOR_STATE_META)[]).map((state) => (
            <li key={state} data-testid={`legend-${state}`}>
              <span aria-hidden="true" style={{ color: SENSOR_STATE_META[state].colour }}>{SENSOR_STATE_META[state].glyph}</span> {SENSOR_STATE_META[state].label}: {SENSOR_STATE_META[state].description}
            </li>
          ))}
          <li data-testid="legend-airflow">→ solid arrow: measured magnitude · → dashed arrow: configured design or stale value. Direction is always the configured orientation.</li>
          <li data-testid="legend-zones">Dashed outlines: operator-configured zones and aisles · thick solid outline: contained aisle · white line: containment boundary · yellow dotted: opening</li>
        </ul>
      </section>
    </div>
  );
}
