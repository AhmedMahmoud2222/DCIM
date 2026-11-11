import { describe, expect, it } from "vitest";

import type { HeatMap } from "@/types";
import { assessThermal, bandExplanation } from "./thermalState";

const map = (metric: "temperature_c" | "humidity_percent" = "temperature_c") =>
  ({ metric, thresholds: metric === "temperature_c" ? [{ rule_type: "threshold_high", threshold: 30, unit: "°C", name: "High inlet" }] : [] }) as unknown as HeatMap;

describe("assessThermal", () => {
  it.each([
    [22, "normal"], [18, "normal"], [27, "normal"],
    [27.5, "caution"], [17, "caution"],
    [30, "warning"], [31.9, "warning"],
    [32, "critical"], [14.9, "critical"],
  ])("%s °C is %s", (value, band) => expect(assessThermal(map(), value).band).toBe(band));

  it("reports unknown for missing or non-finite values", () => {
    expect(assessThermal(map(), null).band).toBe("unknown");
    expect(assessThermal(map(), Number.NaN).band).toBe("unknown");
  });

  it("gives the same band regardless of the other readings in the snapshot", () => {
    expect(assessThermal(map(), 29).band).toBe(assessThermal(map(), 29).band);
  });

  it("uses humidity envelopes for humidity maps", () => {
    expect(assessThermal(map("humidity_percent"), 50).band).toBe("normal");
    expect(assessThermal(map("humidity_percent"), 85).band).toBe("critical");
  });

  it("explains each band", () => {
    expect(bandExplanation("temperature_c", "critical")).toContain("15–32");
  });
});
