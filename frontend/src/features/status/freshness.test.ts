import { describe, expect, it } from "vitest";

import {
  deriveFreshness,
  formatAge,
  FRESHNESS_THRESHOLDS,
  isValidThresholds,
  type FreshnessInput,
} from "@/features/status/freshness";

const NOW = Date.parse("2026-10-24T12:00:00.000Z");
const INTERVAL = 60; // seconds

/** A measurement taken `ageMs` before NOW. */
function measuredAgo(ageMs: number, overrides: Partial<FreshnessInput> = {}): FreshnessInput {
  return {
    sourceType: "equipment_live_metric",
    measuredAt: new Date(NOW - ageMs).toISOString(),
    expectedIntervalSeconds: INTERVAL,
    now: NOW,
    ...overrides,
  };
}

describe("deriveFreshness boundaries (interval 60 s: Fresh up to 75 s, Late up to 120 s)", () => {
  it.each([
    ["just measured", 0, "fresh"],
    ["half an interval", 30_000, "fresh"],
    ["exactly 1x", 60_000, "fresh"],
    ["1 ms under 1.25x", 74_999, "fresh"],
    ["exactly 1.25x is still Fresh", 75_000, "fresh"],
    ["1 ms over 1.25x is Late", 75_001, "late"],
    ["1.5x", 90_000, "late"],
    ["1 ms under 2x", 119_999, "late"],
    ["exactly 2x is still Late", 120_000, "late"],
    ["1 ms over 2x is Stale", 120_001, "stale"],
    ["10x", 600_000, "stale"],
  ])("%s", (_label, ageMs, expected) => {
    expect(deriveFreshness(measuredAgo(ageMs)).state).toBe(expected);
  });

  it("scales with the interval", () => {
    const base = { sourceType: "equipment_live_metric", now: NOW, measuredAt: NOW - 600_000 } as const;
    expect(deriveFreshness({ ...base, expectedIntervalSeconds: 480 }).state).toBe("fresh"); // 1.25x
    expect(deriveFreshness({ ...base, expectedIntervalSeconds: 479 }).state).toBe("late");
    expect(deriveFreshness({ ...base, expectedIntervalSeconds: 300 }).state).toBe("late"); // exactly 2x
    expect(deriveFreshness({ ...base, expectedIntervalSeconds: 299 }).state).toBe("stale");
  });

  it("reports the real age and no reason for a known state", () => {
    expect(deriveFreshness(measuredAgo(90_000))).toEqual({ state: "late", ageSeconds: 90, reason: null });
    expect(deriveFreshness(measuredAgo(0))).toEqual({ state: "fresh", ageSeconds: 0, reason: null });
  });

  it("accepts a Date, epoch milliseconds or ISO text for the measurement and for now", () => {
    const iso = new Date(NOW - 30_000).toISOString();
    for (const measuredAt of [iso, new Date(iso), NOW - 30_000]) {
      for (const now of [NOW, new Date(NOW)]) {
        expect(deriveFreshness({ ...measuredAgo(0), measuredAt, now }).state).toBe("fresh");
      }
    }
  });
});

describe("deriveFreshness never shows missing data as Fresh or zero", () => {
  it.each([
    ["null", null],
    ["undefined", undefined],
    ["empty text", ""],
    ["blank text", "   "],
  ])("missing measurement time (%s)", (_label, measuredAt) => {
    expect(deriveFreshness(measuredAgo(0, { measuredAt }))).toEqual({
      state: "unknown",
      ageSeconds: null,
      reason: "missing_timestamp",
    });
  });

  it.each([
    ["words", "yesterday"],
    ["malformed date", "2026-13-45T99:00:00Z"],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["invalid Date", new Date("nope")],
  ])("invalid measurement time (%s)", (_label, measuredAt) => {
    const result = deriveFreshness(measuredAgo(0, { measuredAt }));
    expect(result).toEqual({ state: "unknown", ageSeconds: null, reason: "invalid_timestamp" });
  });

  it("a measurement time after now is Unknown, not Fresh and not a negative age", () => {
    for (const aheadMs of [1, 1_000, 3_600_000]) {
      expect(deriveFreshness(measuredAgo(-aheadMs))).toEqual({
        state: "unknown",
        ageSeconds: null,
        reason: "future_timestamp",
      });
    }
  });

  it.each([
    ["null", null, "missing_interval"],
    ["undefined", undefined, "missing_interval"],
    ["zero", 0, "invalid_interval"],
    ["negative", -60, "invalid_interval"],
    ["NaN", Number.NaN, "invalid_interval"],
    ["Infinity", Number.POSITIVE_INFINITY, "invalid_interval"],
    ["a string", "60" as unknown as number, "invalid_interval"],
  ])("expected interval %s is Unknown", (_label, expectedIntervalSeconds, reason) => {
    expect(deriveFreshness(measuredAgo(10_000, { expectedIntervalSeconds }))).toEqual({
      state: "unknown",
      ageSeconds: null,
      reason,
    });
  });

  it("an invalid now is Unknown", () => {
    expect(deriveFreshness(measuredAgo(0, { now: Number.NaN })).reason).toBe("invalid_now");
    expect(deriveFreshness(measuredAgo(0, { now: new Date("nope") })).reason).toBe("invalid_now");
  });
});

describe("freshness configuration", () => {
  it("ships the approved defaults and they are valid", () => {
    expect(FRESHNESS_THRESHOLDS.equipment_live_metric).toEqual({ lateAfter: 1.25, staleAfter: 2 });
    for (const thresholds of Object.values(FRESHNESS_THRESHOLDS)) expect(isValidThresholds(thresholds)).toBe(true);
  });

  it.each([
    ["stale not above late", { lateAfter: 2, staleAfter: 2 }],
    ["stale below late", { lateAfter: 2, staleAfter: 1.25 }],
    ["zero late", { lateAfter: 0, staleAfter: 2 }],
    ["negative late", { lateAfter: -1, staleAfter: 2 }],
    ["NaN", { lateAfter: Number.NaN, staleAfter: 2 }],
    ["Infinity", { lateAfter: 1.25, staleAfter: Number.POSITIVE_INFINITY }],
  ])("rejects invalid thresholds (%s) and degrades to Unknown instead of misclassifying", (_label, thresholds) => {
    expect(isValidThresholds(thresholds)).toBe(false);
    const result = deriveFreshness(measuredAgo(0), { equipment_live_metric: thresholds });
    expect(result).toEqual({ state: "unknown", ageSeconds: null, reason: "invalid_configuration" });
  });

  it("uses an injected configuration", () => {
    const strict = { equipment_live_metric: { lateAfter: 1, staleAfter: 1.5 } };
    expect(deriveFreshness(measuredAgo(75_000), strict).state).toBe("late");
    expect(deriveFreshness(measuredAgo(90_001), strict).state).toBe("stale");
  });

  it("rejects a missing configuration entry", () => {
    const empty = {} as unknown as Parameters<typeof deriveFreshness>[1];
    expect(deriveFreshness(measuredAgo(0), empty).reason).toBe("invalid_configuration");
  });
});

describe("formatAge", () => {
  it.each([
    [0, "0 s"],
    [59, "59 s"],
    [60, "1 min"],
    [3599, "59 min"],
    [3600, "1 h"],
    [86_399, "23 h"],
    [86_400, "1 d"],
  ])("%i s -> %s", (seconds, text) => {
    expect(formatAge(seconds)).toBe(text);
  });
});
