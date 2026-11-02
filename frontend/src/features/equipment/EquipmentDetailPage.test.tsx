import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentDetailPage } from "@/features/equipment/EquipmentDetailPage";
import * as equipmentApi from "@/features/equipment/api";
import * as powerApi from "@/features/power/api";
import * as racksApi from "@/features/racks/api";
import * as telemetryApi from "@/features/telemetry/api";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import { Equipment } from "@/types";

vi.mock("@/features/equipment/api");
vi.mock("@/features/power/api");
vi.mock("@/features/racks/api");
vi.mock("@/features/telemetry/api");

const equipment: Equipment = {
  id: "eq-1",
  asset_tag: "SRV-001",
  lifecycle_status: "active",
  model_revision_id: "rev-1",
  catalog_model_revision_id: null,
  hostname: "srv-001",
  owner: null,
  service: null,
  environment: null,
  notes: null,
  version: 1,
  created_at: "2026-01-01T00:00:00Z",
  placement: {
    placement_type: "floor_standing",
    room_id: "room-1",
    rack_id: null,
    u_start: null,
    u_end: null,
    side: null,
    effective_from: "2026-01-01T00:00:00Z",
  },
};

const routeOptions = { route: "/equipment/eq-1", path: "/equipment/:equipmentId" };

describe("EquipmentDetailPage — move mutation error announcement", () => {
  beforeEach(() => {
    vi.mocked(equipmentApi.getEquipment).mockResolvedValue(equipment);
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [], power_inlets: [] });
    vi.mocked(racksApi.listRooms).mockResolvedValue({
      items: [{ id: "room-1", floor_id: "floor-1", code: "A", name: "Room A", room_type: "server", version: 1 }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(racksApi.listRacks).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(powerApi.getEquipmentPowerSummary).mockResolvedValue({
      equipment_asset_id: "eq-1",
      feed_nodes: [],
      redundancy_classification: "no_power_modeled",
      effective_demand_kw: null,
      data_quality: "ok",
    });
    vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([]);
    vi.mocked(telemetryApi.getAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(telemetryApi.getLatestPortStatusForEquipment).mockResolvedValue([]);
  });

  it("announces a 409 move conflict immediately via role=alert, not silent red text", async () => {
    vi.mocked(equipmentApi.moveEquipment).mockRejectedValue(new ApiError(409, "Conflict", "U-range conflict.", null));
    const user = userEvent.setup();

    renderWithProviders(<EquipmentDetailPage />, routeOptions);

    await user.click(await screen.findByRole("button", { name: "Move" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Placement room" }), "room-1");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "That U-range conflicts with existing equipment, or someone else moved this item — reload and try again.",
    );
    expect(equipmentApi.moveEquipment).toHaveBeenCalled();
  });

  it("shows the raw error message via role=alert for a non-conflict failure", async () => {
    vi.mocked(equipmentApi.moveEquipment).mockRejectedValue(new Error("Network error"));
    const user = userEvent.setup();

    renderWithProviders(<EquipmentDetailPage />, routeOptions);

    await user.click(await screen.findByRole("button", { name: "Move" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Placement room" }), "room-1");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Network error");
  });

  it("renders API presentation values instead of canonical storage values", async () => {
    vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([
      {
        id: "reading-1", integration_id: "integration-1", managed_asset_id: "eq-1",
        external_identifier: "availability", metric: "availability", unit: "1", value: 0.995,
        presentation_unit: "%", presentation_value: 99.5,
        occurred_at: "2026-01-01T00:00:00Z", received_at: "2026-01-01T00:00:00Z",
      },
    ]);
    vi.mocked(telemetryApi.getTelemetryHistory).mockResolvedValue([
      {
        id: "daily-1", series_key: "availability-series", integration_id: "integration-1", managed_asset_id: "eq-1",
        external_identifier: "availability", metric: "availability", unit: "1", value: 0.98,
        presentation_unit: "%", presentation_value: 98, resolution: "daily",
        minimum_value: 0.97, maximum_value: 0.99,
        presentation_minimum_value: 97, presentation_maximum_value: 99, sample_count: 24,
        occurred_at: "2026-01-01T00:00:00Z", received_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "daily-2", series_key: "availability-series", integration_id: "integration-1", managed_asset_id: "eq-1",
        external_identifier: "availability", metric: "availability", unit: "1", value: 0.99,
        presentation_unit: "%", presentation_value: 99, resolution: "daily",
        occurred_at: "2026-01-02T00:00:00Z", received_at: "2026-01-02T00:00:00Z",
      },
    ]);

    renderWithProviders(<EquipmentDetailPage />, routeOptions);

    expect(await screen.findByText("99.5")).toBeInTheDocument();
    expect(screen.getByText("%")).toBeInTheDocument();
    expect(screen.queryByText("0.995")).not.toBeInTheDocument();
    expect(await screen.findByText(/98 avg \(97–99, n=24\)/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Trend from 98 to 99 %" })).toBeInTheDocument();
    expect(screen.queryByText(/0\.98 avg/)).not.toBeInTheDocument();
  });
  it("renders alarm history using presentation values and units", async () => {
    vi.mocked(telemetryApi.getAlarmHistory).mockResolvedValue({ items: [
      {
        id: "availability-alarm", rule_id: "rule-1", integration_id: "integration-1", managed_asset_id: "eq-1",
        subject_key: "availability", status: "CLEARED", opened_at: "2026-01-01T00:00:00Z",
        acknowledged_at: null, cleared_at: "2026-01-01T01:00:00Z",
        last_value: 1, unit: "1", presentation_value: 100, presentation_unit: "%",
      },
      {
        id: "temperature-alarm", rule_id: "rule-2", integration_id: "integration-1", managed_asset_id: "eq-1",
        subject_key: "temperature", status: "CLEARED", opened_at: "2026-01-01T00:00:00Z",
        acknowledged_at: null, cleared_at: "2026-01-01T01:00:00Z",
        last_value: 77, unit: "degF", presentation_value: 25, presentation_unit: "degC",
      },
    ], next_cursor: null });
    renderWithProviders(<EquipmentDetailPage />, routeOptions);
    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("25 degC")).toBeInTheDocument();
    expect(screen.queryByText("77")).not.toBeInTheDocument();
  });

  describe("live metric freshness", () => {
    const NOW = Date.parse("2026-10-24T12:00:00.000Z");
    const reading = (metric: string, ageSeconds: number | null, interval: number | null | undefined, occurred?: string) => ({
      id: `reading-${metric}`, integration_id: "integration-1", managed_asset_id: "eq-1",
      external_identifier: metric, metric, unit: "1", value: 1, presentation_unit: "W", presentation_value: 1,
      occurred_at: occurred ?? new Date(NOW - (ageSeconds ?? 0) * 1000).toISOString(),
      received_at: occurred ?? new Date(NOW - (ageSeconds ?? 0) * 1000).toISOString(),
      expected_poll_interval_seconds: interval,
    });
    const card = async (metric: string) => (await screen.findByText(metric)).closest("button") as HTMLElement;

    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(NOW);
    });
    afterEach(() => vi.useRealTimers());

    it("classifies each metric at the exact 1.25x and 2x boundaries of its own expected interval", async () => {
      vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([
        reading("at_1x", 60, 60), reading("at_1_25x", 75, 60), reading("over_1_25x", 76, 60),
        reading("at_2x", 120, 60), reading("over_2x", 121, 60),
      ]);
      renderWithProviders(<EquipmentDetailPage />, routeOptions);

      const expected: Array<[metric: string, label: string, age: string]> = [
        ["at_1x", "Fresh", "measured 1 min ago"],
        ["at_1_25x", "Fresh", "measured 1 min ago"],
        ["over_1_25x", "Late", "measured 1 min ago"],
        ["at_2x", "Late", "measured 2 min ago"],
        ["over_2x", "Stale", "measured 2 min ago"],
      ];
      for (const [metric, label, age] of expected) {
        const chip = within(await card(metric)).getByTestId("measurement-freshness");
        expect(chip).toHaveTextContent(label);
        expect(chip).toHaveTextContent(age);
      }
    });

    it("no longer says Current", async () => {
      vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([reading("power", 10, 60)]);
      renderWithProviders(<EquipmentDetailPage />, routeOptions);

      const metric = await card("power");
      expect(within(metric).getByText("Fresh")).toBeInTheDocument();
      expect(metric).not.toHaveTextContent("Current");
      expect(metric).toHaveTextContent("occurred");
    });

    it.each([
      ["no expected interval", reading("m", 10, null), "no expected poll interval"],
      ["an absent interval", reading("m", 10, undefined), "no expected poll interval"],
      ["a zero interval", reading("m", 10, 0), "expected poll interval is not valid"],
      ["a negative interval", reading("m", 10, -5), "expected poll interval is not valid"],
      ["a future measurement time", reading("m", -30, 60), "measurement time is in the future"],
      ["an unparseable measurement time", reading("m", null, 60, "not-a-date"), "measurement time is not valid"],
    ])("shows Unknown, never Fresh or a zero age, for %s", async (_label, item, reason) => {
      vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([item]);
      renderWithProviders(<EquipmentDetailPage />, routeOptions);

      const chip = within(await card("m")).getByTestId("measurement-freshness");
      expect(chip).toHaveTextContent("Unknown");
      expect(chip).toHaveTextContent(reason);
      expect(chip).not.toHaveTextContent(/\bFresh\b|\bago\b/);
    });

    it("labels the result as derived so it is not mistaken for a collector status", async () => {
      vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([reading("power", 10, 60)]);
      renderWithProviders(<EquipmentDetailPage />, routeOptions);

      const chip = within(await card("power")).getByTestId("measurement-freshness");
      expect(within(chip).getByText("Measurement freshness (derived):", { exact: false })).toHaveClass("sr-only");
      expect(chip.querySelector("[title]")).toHaveAttribute("title", expect.stringMatching(/derived in the browser/i));
    });
  });

});
