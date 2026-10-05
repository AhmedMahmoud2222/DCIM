import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
        id: "daily-1", integration_id: "integration-1", managed_asset_id: "eq-1",
        external_identifier: "availability", metric: "availability", unit: "1", value: 0.98,
        presentation_unit: "%", presentation_value: 98, resolution: "daily",
        minimum_value: 0.97, maximum_value: 0.99,
        presentation_minimum_value: 97, presentation_maximum_value: 99, sample_count: 24,
        occurred_at: "2026-01-01T00:00:00Z", received_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "daily-2", integration_id: "integration-1", managed_asset_id: "eq-1",
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
});
