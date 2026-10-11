import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TelemetryReading } from "@/features/telemetry/api";
import { groupHistoryPoints, TelemetryTrend } from "@/features/telemetry/TelemetryTrend";

function point(id: string, value: number, unit: string): TelemetryReading {
  return {
    id, integration_id: "integration", managed_asset_id: "asset", external_identifier: "sensor",
    series_key: "temperature", provenance_status: "pinned", registry_version: "1",
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
      { ...point("old", 77, "degF"), unit: "degC", presentation_value: 25, presentation_unit: "degC" },
      point("new", 25, "degC"),
    ]} />);
    expect(screen.getByRole("img", { name: "Trend from 25 to 25 degC" })).toBeInTheDocument();
  });
});


describe("G7 provenance separation", () => {
  it("labels and charts unverified groups separately without spanning their ranges", () => {
    render(<TelemetryTrend points={[
      { ...point("a", 10, "degC"), series_key: "same", provenance_status: "pinned", mapping_revision_id: "r" },
      { ...point("b", 20, "degC"), series_key: "same", provenance_status: "pinned", mapping_revision_id: "r" },
      { ...point("c", 100, "degC"), series_key: "same", provenance_status: "unverified_registry" },
      { ...point("d", 200, "degC"), series_key: "same", provenance_status: "unverified_registry" },
    ]} />);
    expect(screen.getByText(/Mixed provenance/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Trend from 10 to 20 degC" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Trend from 100 to 200 degC" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Trend from 10 to 200 degC" })).not.toBeInTheDocument();
    expect(screen.getByText("Unverified registry")).toBeInTheDocument();
  });
});


it("never joins different series or registry versions just because units match", () => {
  const base = point("a", 10, "degC");
  const groups = groupHistoryPoints([
    base, { ...base, id: "b", series_key: "other" },
    { ...base, id: "c", registry_version: "2" },
  ]);
  expect(groups).toHaveLength(3);
});

it("keeps daily rows out of reading contract groups", () => {
  const base = point("a", 10, "degC");
  expect(groupHistoryPoints([base, { ...base, id: "daily", resolution: "daily", provenance_status: null }])).toHaveLength(2);
});

it("shows a single unverified reading with its label even without a line", () => {
  render(<TelemetryTrend points={[{ ...point("a", 10, "degC"), provenance_status: "unverified_legacy" }]} />);
  expect(screen.getByText("Unverified legacy")).toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});
