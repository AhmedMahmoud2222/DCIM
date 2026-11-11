import { BAND_META, type ThermalBand } from "./thermalState";

const TONE: Record<ThermalBand, string> = {
  normal: "border-tone-ok-fg bg-tone-ok-bg text-tone-ok-fg",
  caution: "border-tone-caution-fg bg-tone-caution-bg text-tone-caution-fg",
  warning: "border-tone-serious-fg bg-tone-serious-bg text-tone-serious-fg",
  critical: "border-tone-danger-fg bg-tone-danger-bg text-tone-danger-fg",
  unknown: "border-dashed border-tone-unknown-fg bg-tone-unknown-bg text-tone-unknown-fg",
};

function Glyph({ band }: { band: ThermalBand }) {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true" focusable="false" fill="currentColor">
      {band === "normal" && <circle cx="6" cy="6" r="4" />}
      {band === "caution" && <path d="M6 1.5 10.8 10H1.2Z" fill="none" stroke="currentColor" strokeWidth="1.4" />}
      {band === "warning" && <path d="M6 1.5 10.8 10H1.2Z" />}
      {band === "critical" && <path d="M4 1.2h4l2.8 2.8v4L8 10.8H4L1.2 8V4Z" />}
      {band === "unknown" && <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeDasharray="1.6 1.4" strokeWidth="1.2" />}
    </svg>
  );
}

export function ThermalBandChip({ band, reason }: { band: ThermalBand; reason?: string }) {
  return (
    <span title={reason} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-2 py-0.5 text-xs ${TONE[band]}`}>
      <Glyph band={band} />
      <span className="sr-only">Thermal state: </span>
      {BAND_META[band].label}
    </span>
  );
}
