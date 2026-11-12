import type { HeatMap, HeatMapThreshold, ThermalMetric } from "@/types";

export type ThermalBand = "normal" | "caution" | "warning" | "critical" | "unknown";

export interface ThermalAssessment {
  band: ThermalBand;
  reason: string;
}

/** ASHRAE TC 9.9 Class A1 inlet dry-bulb envelope (°C): recommended 18–27, allowable 15–32. */
export const ASHRAE_A1 = {
  recommended: { min: 18, max: 27 },
  allowable: { min: 15, max: 32 },
} as const;

interface Envelope {
  unit: string;
  recommended: { min: number; max: number };
  allowable: { min: number; max: number } | null;
  domain: { min: number; max: number };
}

export const ENVELOPES: Record<ThermalMetric, Envelope> = {
  temperature_c: {
    unit: "°C",
    recommended: ASHRAE_A1.recommended,
    allowable: ASHRAE_A1.allowable,
    domain: { min: 10, max: 35 },
  },
  humidity_percent: {
    unit: "% RH",
    recommended: { min: 40, max: 60 },
    allowable: { min: 20, max: 80 },
    domain: { min: 0, max: 100 },
  },
};

export const BAND_META: Record<ThermalBand, { label: string }> = {
  normal: { label: "Normal" },
  caution: { label: "Caution" },
  warning: { label: "Warning" },
  critical: { label: "Critical" },
  unknown: { label: "Unknown" },
};

const fmt = (value: number, unit: string) => `${Number(value.toFixed(1))} ${unit}`;

function crossedRule(map: HeatMap, value: number): HeatMapThreshold | null {
  return (
    map.thresholds.find((rule) =>
      rule.rule_type === "threshold_high" ? value >= rule.threshold : value <= rule.threshold,
    ) ?? null
  );
}

export function assessThermal(map: HeatMap, value: number | null | undefined): ThermalAssessment {
  const env = ENVELOPES[map.metric];
  if (value == null || !Number.isFinite(value)) {
    return { band: "unknown", reason: "No value: this cell has no supporting fresh sensor." };
  }
  if (env.allowable) {
    if (value >= env.allowable.max) {
      return { band: "critical", reason: `${fmt(value, env.unit)} is at or above the ${env.allowable.max} ${env.unit} A1 allowable maximum.` };
    }
    if (value < env.allowable.min) {
      return { band: "critical", reason: `${fmt(value, env.unit)} is below the ${env.allowable.min} ${env.unit} A1 allowable minimum.` };
    }
  }
  const rule = crossedRule(map, value);
  if (rule) {
    return { band: "warning", reason: `${fmt(value, env.unit)} crossed “${rule.name}” at ${rule.threshold} ${rule.unit ?? env.unit}.` };
  }
  if (value < env.recommended.min || value > env.recommended.max) {
    return { band: "caution", reason: `${fmt(value, env.unit)} is outside the recommended ${env.recommended.min}–${env.recommended.max} ${env.unit} envelope.` };
  }
  return { band: "normal", reason: `${fmt(value, env.unit)} is inside the recommended ${env.recommended.min}–${env.recommended.max} ${env.unit} envelope.` };
}

export function bandExplanation(metric: ThermalMetric, band: ThermalBand): string {
  const env = ENVELOPES[metric];
  switch (band) {
    case "normal":
      return `inside ${env.recommended.min}–${env.recommended.max} ${env.unit} (recommended)`;
    case "caution":
      return `outside ${env.recommended.min}–${env.recommended.max} ${env.unit}, no site alarm crossed`;
    case "warning":
      return "a configured alarm threshold is crossed";
    case "critical":
      return env.allowable
        ? `outside ${env.allowable.min}–${env.allowable.max} ${env.unit} (allowable)`
        : "configured critical limit crossed";
    case "unknown":
      return "no usable reading";
  }
}
