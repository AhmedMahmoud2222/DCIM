import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as catalogApi from "@/features/catalog-designer/api";
import * as equipmentApi from "@/features/equipment/api";
import * as powerApi from "@/features/power/api";
import { FaceplateOverlay } from "@/features/racks/FaceplateOverlay";
import { renderWithProviders } from "@/test/renderWithProviders";
import {
  CatalogGraphic,
  CatalogGraphicMarker,
  CatalogModelRevisionDetail,
  EquipmentPort,
  EquipmentPowerInlet,
  LatestPortStatus,
} from "@/types";

vi.mock("@/features/catalog-designer/api");
vi.mock("@/features/equipment/api");
vi.mock("@/features/power/api");

beforeEach(() => {
  // jsdom has no createObjectURL/revokeObjectURL implementation.
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:mock-url"), revokeObjectURL: vi.fn() });
  vi.mocked(catalogApi.fetchGraphicImage).mockResolvedValue(new Blob(["fake-bytes"], { type: "image/png" }));
  vi.mocked(powerApi.getEquipmentPowerSummary).mockResolvedValue({
    equipment_asset_id: "equip-1", feed_nodes: [], redundancy_classification: "no_power_modeled",
    effective_demand_kw: null, data_quality: "unknown",
  });
});

function makeMarker(overrides: Partial<CatalogGraphicMarker> = {}): CatalogGraphicMarker {
  return {
    id: "marker-1", catalog_graphic_id: "graphic-1", revision_version: 1, marker_type: "network_port",
    network_port_template_id: "tmpl-1", power_supply_template_id: null, label: "eth0", marker_x: 0.5, marker_y: 0.5,
    sort_order: 0, ...overrides,
  };
}

function makeGraphic(markers: CatalogGraphicMarker[]): CatalogGraphic {
  return {
    id: "graphic-1", catalog_model_revision_id: "rev-1", revision_version: 1, side: "front",
    original_filename: "front.png", mime_type: "image/png", file_size_bytes: 100, width_px: 800, height_px: 600,
    uploaded_at: "2026-01-01T00:00:00Z", markers,
  };
}

function makeRevision(graphics: CatalogGraphic[]): CatalogModelRevisionDetail {
  return {
    id: "rev-1", catalog_model_id: "model-1", revision_number: 1, lifecycle_status: "published", dimension_unit: "mm",
    width_value: 440, height_value: 44.45, depth_value: 600, rack_unit_height: 1, weight_unit: "kg", weight_value: 10,
    mounting_orientation: null, supported_placement_types: null, airflow_direction: null, rated_power_w: null,
    typical_power_w: null, max_power_w: null, heat_dissipation_btu_hr: null, power_redundancy_mode: null,
    cloned_from_revision_id: null, created_by_user_id: "user-1", published_at: "2026-01-01T00:00:00Z",
    published_by_user_id: "user-1", retired_at: null, retired_by_user_id: null, retirement_reason: null,
    allow_installation_when_retired: false, version: 1, created_at: "2026-01-01T00:00:00Z",
    network_ports: [], power_supplies: [], monitoring_metrics: [], graphics,
  };
}

function makePort(overrides: Partial<EquipmentPort> = {}): EquipmentPort {
  return {
    id: "port-1", equipment_id: "equip-1", network_port_template_id: "tmpl-1", stable_key: "eth0",
    display_name: "eth0", media_type: "copper", supported_speeds_mbps: [1000], connector_type: "rj45",
    role: "access", side: "front", module_group: null, sort_order: 0, connection: null, ...overrides,
  };
}

function makeInlet(overrides: Partial<EquipmentPowerInlet> = {}): EquipmentPowerInlet {
  return {
    id: "inlet-1", equipment_id: "equip-1", power_supply_template_id: "psu-1", power_node_id: "node-1",
    stable_key: "psu-1", label: "PSU 1", connector_type: "C14", sort_order: 0, ...overrides,
  };
}

function makeStatus(overrides: Partial<LatestPortStatus> = {}): LatestPortStatus {
  return {
    binding_id: "binding-1", equipment_id: "equip-1", target_type: "network_port", equipment_port_id: "port-1",
    equipment_power_inlet_id: null, label: null, status_level: "UP",
    payload: { link_state: "UP", bandwidth_util_pct: 10, error_rate_pct: 0 },
    sampled_at: "2026-01-01T00:00:00Z", received_at: "2026-01-01T00:00:00Z", ...overrides,
  };
}

describe("FaceplateOverlay telemetry overlay", () => {
  it("renders a healthy status ring-3 with no alert halo for an UP link", async () => {
    vi.mocked(catalogApi.getRevision).mockResolvedValue(makeRevision([makeGraphic([makeMarker()])]));
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [makePort()], power_inlets: [] });

    renderWithProviders(
      <FaceplateOverlay
        catalogModelRevisionId="rev-1"
        equipmentId="equip-1"
        side="front"
        portStatusByPortId={{ "port-1": makeStatus({ status_level: "UP" }) }}
      />,
    );

    const marker = await waitFor(() => screen.getByTestId("faceplate-marker"));
    expect(marker).toHaveAttribute("data-telemetry-status", "UP");
    expect(screen.getByTestId("telemetry-status-ring")).toBeInTheDocument();
    expect(screen.queryByTestId("telemetry-alert-halo")).not.toBeInTheDocument();
  });

  it("renders a pulsing alert halo for a DOWN link", async () => {
    vi.mocked(catalogApi.getRevision).mockResolvedValue(makeRevision([makeGraphic([makeMarker()])]));
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [makePort()], power_inlets: [] });

    renderWithProviders(
      <FaceplateOverlay
        catalogModelRevisionId="rev-1"
        equipmentId="equip-1"
        side="front"
        portStatusByPortId={{
          "port-1": makeStatus({
            status_level: "DOWN", payload: { link_state: "DOWN", bandwidth_util_pct: 0, error_rate_pct: 0 },
          }),
        }}
      />,
    );

    const marker = await waitFor(() => screen.getByTestId("faceplate-marker"));
    expect(marker).toHaveAttribute("data-telemetry-status", "DOWN");
    expect(screen.getByTestId("telemetry-alert-halo")).toBeInTheDocument();
    expect(marker.getAttribute("aria-label")).toContain("Link DOWN");
  });

  it("renders a critical halo for a power marker over threshold", async () => {
    const marker = makeMarker({
      id: "marker-2", marker_type: "power_supply", network_port_template_id: null, power_supply_template_id: "psu-1",
    });
    vi.mocked(catalogApi.getRevision).mockResolvedValue(makeRevision([makeGraphic([marker])]));
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [], power_inlets: [makeInlet()] });

    renderWithProviders(
      <FaceplateOverlay
        catalogModelRevisionId="rev-1"
        equipmentId="equip-1"
        side="front"
        portStatusByInletId={{
          "inlet-1": makeStatus({
            target_type: "power_inlet", equipment_port_id: null, equipment_power_inlet_id: "inlet-1",
            status_level: "CRITICAL", payload: { current_amps: 9.5, active_power_watts: 1140, voltage: 120 },
          }),
        }}
      />,
    );

    const markerEl = await waitFor(() => screen.getByTestId("faceplate-marker"));
    expect(markerEl).toHaveAttribute("data-telemetry-status", "CRITICAL");
    expect(screen.getByTestId("telemetry-alert-halo")).toBeInTheDocument();
    expect(markerEl.getAttribute("aria-label")).toContain("1140 W");
  });

  it("renders no telemetry ring-3 when no binding exists for the marker", async () => {
    vi.mocked(catalogApi.getRevision).mockResolvedValue(makeRevision([makeGraphic([makeMarker()])]));
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [makePort()], power_inlets: [] });

    renderWithProviders(<FaceplateOverlay catalogModelRevisionId="rev-1" equipmentId="equip-1" side="front" />);

    const marker = await waitFor(() => screen.getByTestId("faceplate-marker"));
    expect(marker).not.toHaveAttribute("data-telemetry-status");
    expect(screen.queryByTestId("telemetry-status-ring")).not.toBeInTheDocument();
    expect(screen.queryByTestId("telemetry-alert-halo")).not.toBeInTheDocument();
  });
});
