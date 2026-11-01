import { FreshnessResult, FreshnessState } from "@/features/status/freshness";

/** One entry per status dimension. The dimensions are independent: a value from one is never mapped
 * into another (an ACTIVE alarm says nothing about severity, a stale collector says nothing about a
 * metric's freshness). Each model only picks the text, tone and icon variant for a value that the
 * backend, or the freshness helper, already supplied. */
export type Tone = "neutral" | "info" | "ok" | "caution" | "serious" | "danger" | "attention" | "unknown";

export interface StatusModel<Variant extends string> {
  /** Text shown to the operator. */
  label: string;
  tone: Tone;
  variant: Variant;
  /** False when the value is not one this build recognises; the raw value is still shown. */
  known: boolean;
}

// --- Asset lifecycle (managed asset, rack, equipment) -----------------------------------------
const LIFECYCLE_TONES = {
  planned: "neutral",
  installed: "info",
  active: "ok",
  maintenance: "caution",
  decommissioned: "serious",
  removed: "danger",
} as const satisfies Record<string, Tone>;

export type LifecycleVariant = keyof typeof LIFECYCLE_TONES | "unrecognised";

/** The backend word is shown unchanged. A value this build does not know keeps its own text with a
 * neutral "unrecognised" look instead of borrowing a known state's colour. */
export function lifecycleModel(value: string | null | undefined): StatusModel<LifecycleVariant> {
  const text = value == null || value === "" ? "unknown" : value;
  if (value != null && Object.prototype.hasOwnProperty.call(LIFECYCLE_TONES, value)) {
    const key = value as keyof typeof LIFECYCLE_TONES;
    return { label: text, tone: LIFECYCLE_TONES[key], variant: key, known: true };
  }
  return { label: text, tone: "unknown", variant: "unrecognised", known: false };
}

// --- Alarm workflow ---------------------------------------------------------------------------
export type AlarmWorkflowVariant = "ACTIVE" | "ACKNOWLEDGED" | "CLEARED" | "unrecognised";

/** Where an alarm is in its handling. Deliberately independent of severity: ACTIVE uses the
 * `attention` tone, which is not on the red-to-green urgency scale. */
export function alarmWorkflowModel(status: string | null | undefined): StatusModel<AlarmWorkflowVariant> {
  switch (status) {
    case "ACTIVE":
      return { label: "ACTIVE", tone: "attention", variant: "ACTIVE", known: true };
    case "ACKNOWLEDGED":
      return { label: "ACKNOWLEDGED", tone: "caution", variant: "ACKNOWLEDGED", known: true };
    case "CLEARED":
      return { label: "CLEARED", tone: "ok", variant: "CLEARED", known: true };
    default:
      return { label: status ? String(status) : "UNKNOWN", tone: "unknown", variant: "unrecognised", known: false };
  }
}

// --- Exception severity -----------------------------------------------------------------------
export type SeverityVariant = "critical" | "warning" | "info" | "unrecognised";

/** Severity exactly as the backend supplied it. A missing or new value is "Unknown severity", never
 * quietly downgraded to Info or upgraded to Critical. */
export function exceptionSeverityModel(severity: string | null | undefined): StatusModel<SeverityVariant> {
  switch (severity) {
    case "critical":
      return { label: "Critical", tone: "danger", variant: "critical", known: true };
    case "warning":
      return { label: "Warning", tone: "caution", variant: "warning", known: true };
    case "info":
      return { label: "Info", tone: "info", variant: "info", known: true };
    default:
      return { label: "Unknown severity", tone: "unknown", variant: "unrecognised", known: false };
  }
}

// --- Collector connectivity -------------------------------------------------------------------
export type ConnectivityVariant = "healthy" | "stale" | "offline" | "unrecognised";

/** The backend's heartbeat-based classification, unchanged. Not a measurement freshness. */
export function connectivityModel(health: string | null | undefined): StatusModel<ConnectivityVariant> {
  switch (health) {
    case "healthy":
      return { label: "Healthy", tone: "ok", variant: "healthy", known: true };
    case "stale":
      return { label: "Stale", tone: "caution", variant: "stale", known: true };
    case "offline":
      return { label: "Offline", tone: "danger", variant: "offline", known: true };
    default:
      return { label: health ? String(health) : "Unknown", tone: "unknown", variant: "unrecognised", known: false };
  }
}

// --- Measurement freshness (derived in the browser) -------------------------------------------
const FRESHNESS_TONES: Record<FreshnessState, Tone> = {
  fresh: "ok",
  late: "caution",
  stale: "serious",
  unknown: "unknown",
};

const FRESHNESS_LABELS: Record<FreshnessState, string> = {
  fresh: "Fresh",
  late: "Late",
  stale: "Stale",
  unknown: "Unknown",
};

export function freshnessModel(result: Pick<FreshnessResult, "state">): StatusModel<FreshnessState> {
  return {
    label: FRESHNESS_LABELS[result.state],
    tone: FRESHNESS_TONES[result.state],
    variant: result.state,
    known: result.state !== "unknown",
  };
}
