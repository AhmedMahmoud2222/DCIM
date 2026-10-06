import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TelemetryReading } from "@/features/telemetry/api";
import { TelemetryTrend } from "@/features/telemetry/TelemetryTrend";

function point(id: string, value: number, unit: string): TelemetryReading {
  return {
    id, integration_id: "integration", managed_asset_id: "asset", external_identifier: "sensor",
    metric: "temperature_c", value, unit, presentation_value: value, presentation_unit: unit,
    occurred_at: "2026-01-01T00:00:00Z", received_at: "2026-01-01T00:00:00Z",
  };
}

describe("TelemetryTrend", () => {
  it.each([
    [point("legacy", 77, "degF"), point("canonical", 25, "degC")],
    [point("legacy", 1, "1"), point("canonical", 100, "%")],
    [point("legacy", 25, "degrees C"), point("canonical", 25, "degC")],
  ])("does not plot incompatible presentation units as one numeric series", (legacy, canonical) => {
    render(<TelemetryTrend points={[legacy, canonical]} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText(/history contains multiple units/)).toBeInTheDocument();
  });

  it("plots equal presentation units using their presented values", () => {
    render(<TelemetryTrend points={[
      { ...point("old", 77, "degF"), presentation_value: 25, presentation_unit: "degC" },
      point("new", 25, "degC"),
    ]} />);
    expect(screen.getByRole("img", { name: "Trend from 25 to 25 degC" })).toBeInTheDocument();
  });
});
