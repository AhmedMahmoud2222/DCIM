/** Derived measurement freshness: how old a measurement is compared with how often it is expected.
 *
 * This is computed in the browser from a real measurement time and the expected polling interval.
 * It is NOT a backend status and is not collector connectivity: a collector can be healthy while a
 * metric is stale, and the other way round. Only sources whose measurement time and expected
 * interval are both real belong here. */

export type FreshnessState = "fresh" | "late" | "stale" | "unknown";

export type FreshnessUnknownReason =
  | "missing_timestamp"
  | "invalid_timestamp"
  | "future_timestamp"
  | "missing_interval"
  | "invalid_interval"
  | "invalid_now"
  | "invalid_configuration";

/** Multiples of the expected interval. Age up to `lateAfter` is Fresh, up to `staleAfter` is Late,
 * and anything older is Stale. */
export interface FreshnessThresholds {
  lateAfter: number;
  staleAfter: number;
}

export type FreshnessSourceType = "equipment_live_metric";

/** The single place the thresholds are chosen. Add a source type here when it has real measurement
 * times and a real expected interval; do not reuse a type for a source that only has heartbeats or
 * poll attempts. */
export const FRESHNESS_THRESHOLDS: Readonly<Record<FreshnessSourceType, FreshnessThresholds>> = {
  equipment_live_metric: { lateAfter: 1.25, staleAfter: 2 },
};

export function isValidThresholds(value: FreshnessThresholds | null | undefined): value is FreshnessThresholds {
  return (
    !!value &&
    Number.isFinite(value.lateAfter) &&
    Number.isFinite(value.staleAfter) &&
    value.lateAfter > 0 &&
    value.staleAfter > value.lateAfter
  );
}

export interface FreshnessInput {
  sourceType: FreshnessSourceType;
  /** When the value was measured (not when it was received or polled). ISO text, Date or epoch ms. */
  measuredAt: string | number | Date | null | undefined;
  expectedIntervalSeconds: number | null | undefined;
  /** Supplied by the caller so the result is deterministic. */
  now: Date | number;
}

export interface FreshnessResult {
  state: FreshnessState;
  /** Whole age in seconds; null when it cannot be known. Never 0 for a missing measurement. */
  ageSeconds: number | null;
  reason: FreshnessUnknownReason | null;
}

const unknown = (reason: FreshnessUnknownReason, ageSeconds: number | null = null): FreshnessResult => ({
  state: "unknown",
  ageSeconds,
  reason,
});

function toMillis(value: string | number | Date): number {
  if (value instanceof Date) return value.getTime();
  if (typeof value === "number") return value;
  return value.trim() === "" ? Number.NaN : Date.parse(value);
}

/** Classifies one measurement. A future timestamp (clock skew or bad data) is Unknown rather than a
 * negative age, so it can never read as Fresh. */
export function deriveFreshness(
  input: FreshnessInput,
  thresholds: Readonly<Record<FreshnessSourceType, FreshnessThresholds>> = FRESHNESS_THRESHOLDS,
): FreshnessResult {
  const config = thresholds[input.sourceType];
  if (!isValidThresholds(config)) return unknown("invalid_configuration");

  const nowMs = toMillis(input.now);
  if (!Number.isFinite(nowMs)) return unknown("invalid_now");

  if (input.measuredAt == null || (typeof input.measuredAt === "string" && input.measuredAt.trim() === "")) {
    return unknown("missing_timestamp");
  }
  const measuredMs = toMillis(input.measuredAt);
  if (!Number.isFinite(measuredMs)) return unknown("invalid_timestamp");
  if (measuredMs > nowMs) return unknown("future_timestamp");

  const interval = input.expectedIntervalSeconds;
  if (interval == null) return unknown("missing_interval");
  if (typeof interval !== "number" || !Number.isFinite(interval) || interval <= 0) return unknown("invalid_interval");

  const ageMs = nowMs - measuredMs;
  const ageSeconds = Math.floor(ageMs / 1000);
  const intervalMs = interval * 1000;
  if (ageMs <= config.lateAfter * intervalMs) return { state: "fresh", ageSeconds, reason: null };
  if (ageMs <= config.staleAfter * intervalMs) return { state: "late", ageSeconds, reason: null };
  return { state: "stale", ageSeconds, reason: null };
}

/** Compact human age: "42 s", "5 min", "3 h", "2 d". */
export function formatAge(ageSeconds: number): string {
  if (ageSeconds < 60) return `${ageSeconds} s`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)} min`;
  if (ageSeconds < 86_400) return `${Math.floor(ageSeconds / 3600)} h`;
  return `${Math.floor(ageSeconds / 86_400)} d`;
}

export const UNKNOWN_REASON_TEXT: Readonly<Record<FreshnessUnknownReason, string>> = {
  missing_timestamp: "no measurement time",
  invalid_timestamp: "measurement time is not valid",
  future_timestamp: "measurement time is in the future",
  missing_interval: "no expected poll interval",
  invalid_interval: "expected poll interval is not valid",
  invalid_now: "current time is not valid",
  invalid_configuration: "freshness thresholds are not valid",
};
