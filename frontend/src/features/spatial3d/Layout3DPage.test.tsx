import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Layout3DPage } from "@/features/spatial3d/Layout3DPage";
import * as coolingApi from "@/features/cooling/api";
import { heatMap, layout as coolingLayout, partialMap } from "@/features/cooling/fixtures";
import * as floorPlansApi from "@/features/floor-plans/api";
import * as racksApi from "@/features/racks/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Page, Room, RoomOverlays, RoomSpatialView } from "@/types";

vi.mock("@/features/floor-plans/api");
vi.mock("@/features/racks/api");
vi.mock("@/features/cooling/api");

const room: Room = { id: "room-1", floor_id: "floor-1", code: "A", name: "DC-1 Hall A", room_type: "hall", version: 1 };

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

const rack1 = {
  id: "rack-1", asset_tag: "RCK-001", name: "Rack A1", x_mm: 900, y_mm: 1200, rotation_deg: 90, spatial_object_id: null, height_u: 42,
  width_mm: 600, depth_mm: 1000, height_mm: 1867, position_state: "placed" as const, placement_version: 3,
};

const spatialView: RoomSpatialView = {
  room_id: "room-1",
  room_name: "DC-1 Hall A",
  active_floor_plan_id: "fp-1",
  active_floor_plan_revision: 1,
  room_width_mm: 10_000,
  room_height_mm: 8_000,
  generated_at: "2026-09-27T00:00:00Z",
  racks: [rack1],
  equipment: [],
  objects: [],
  rack_equipment: [{ id: "eq-1", rack_id: "rack-1", asset_tag: "SRV-001", hostname: "srv-001", u_start: 3, u_end: 5, side: "front" }],
  calibration: { id: "c1", method: "declared_units", source_units: "mm", mm_per_unit: 1, error_bound_mm: 0.5, relative_error: 0, confidence: "high", created_at: "" },
  layout_state: "validated",
  incomplete_reasons: [],
};

describe("Layout3DPage", () => {
  beforeEach(() => {
    vi.mocked(racksApi.listRooms).mockResolvedValue(mockPage([room]));
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue(spatialView);
  });

  it("renders the room's rack as a real-scale cuboid at its authoritative coordinates and orientation", async () => {
    renderWithProviders(<Layout3DPage />);

    const cuboid = await screen.findByTestId("cuboid-rack-1");
    expect(cuboid).toHaveAttribute("data-x-mm", "900");
    expect(cuboid).toHaveAttribute("data-y-mm", "1200");
    expect(cuboid).toHaveAttribute("data-rotation-deg", "90");
    const w = Number(cuboid.getAttribute("data-width-px"));
    const d = Number(cuboid.getAttribute("data-depth-px"));
    const h = Number(cuboid.getAttribute("data-height-px"));
    expect(w / d).toBeCloseTo(0.6, 5); // 600 x 1000 mm, not a standardized CSS footprint
    expect(h / d).toBeCloseTo(1.867, 3); // 1867 mm tall (42U x 44.45 mm)
    const viewport = screen.getByTestId("scene-viewport");
    const pxPerMm = Number(viewport.getAttribute("data-scale-px-per-mm"));
    expect(d / pxPerMm).toBeCloseTo(1000, 1);
    expect(screen.getByTestId("scene-provenance")).toHaveTextContent(/Real scale/);
    expect(screen.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "validated");
    expect(screen.queryByText("≈")).not.toBeInTheDocument();
  });

  it("selecting a rack resolves to the same authoritative asset identity used elsewhere", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);

    await user.click(await screen.findByRole("button", { name: /Select rack Rack A1, 42U capacity, 1 placed asset, 600 mm by 1.00 m/ }));

    expect(await screen.findByTestId("selected-name")).toHaveTextContent("Rack A1");
    expect(screen.getByTestId("selected-asset-id")).toHaveAttribute("data-asset-id", "rack-1");
    expect(screen.getByTestId("cuboid-rack-1")).toHaveAttribute("data-asset-id", "rack-1");
    expect(screen.getByRole("link", { name: "Open rack elevation" })).toHaveAttribute("href", "/racks/rack-1");
    expect(screen.getByText("900, 1200 mm")).toBeInTheDocument();
    expect(screen.getByText("600 × 1000 mm")).toBeInTheDocument();
    expect(screen.getByText("1867 mm (42U)")).toBeInTheDocument();
  });

  it("is keyboard operable: Enter on the rack face selects it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    const face = await screen.findByRole("button", { name: /Select rack Rack A1/ });
    face.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("selected-name")).toHaveTextContent("Rack A1");
    expect(face).toHaveAttribute("aria-pressed", "true");
  });

  it("does not draw a rack with no recorded position; it is listed as incomplete, never placed at a fallback grid slot", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      racks: [rack1, { ...rack1, id: "rack-2", name: "Rack B2", x_mm: null, y_mm: null, position_state: "missing" }],
      layout_state: "incomplete",
      incomplete_reasons: ["rack_position_missing:1"],
    });
    renderWithProviders(<Layout3DPage />);

    await screen.findByTestId("cuboid-rack-1");
    expect(screen.queryByTestId("cuboid-rack-2")).not.toBeInTheDocument();
    const panel = screen.getByTestId("incomplete-panel");
    expect(within(panel).getByText(/Rack B2/)).toBeInTheDocument();
    expect(within(panel).getByText(/no floor position is recorded/)).toBeInTheDocument();
    expect(screen.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "incomplete");
    expect(screen.getByTestId("incomplete-reasons")).toHaveTextContent("1 rack placed in this room has no recorded floor position");
  });

  it("does not draw a rack whose catalog footprint is unknown", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      racks: [rack1, { ...rack1, id: "rack-3", name: "Rack C3", width_mm: null }],
    });
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    expect(screen.queryByTestId("cuboid-rack-3")).not.toBeInTheDocument();
    expect(screen.getByText(/Rack C3: catalog footprint unknown/)).toBeInTheDocument();
  });

  it("draws floor equipment from catalog dimensions and pins (not to scale) equipment with incomplete dimensions", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      equipment: [
        { id: "ups-1", asset_tag: "UPS-1", hostname: "ups-1", placement_type: "floor_standing", spatial_object_id: "o1", x_mm: 5000, y_mm: 500, rotation_deg: 0, width_mm: 800, depth_mm: 900, height_mm: 889, position_state: "placed", dimensions_state: "complete" },
        { id: "pdu-1", asset_tag: "PDU-1", hostname: "pdu-1", placement_type: "floor_standing", spatial_object_id: "o2", x_mm: 100, y_mm: 100, width_mm: 800, depth_mm: null, height_mm: null, position_state: "placed", dimensions_state: "partial" },
      ],
    });
    renderWithProviders(<Layout3DPage />);
    const ups = await screen.findByTestId("cuboid-ups-1");
    expect(ups).toHaveAttribute("data-asset-kind", "equipment");
    expect(screen.getByTestId("pin-pdu-1")).toBeInTheDocument();
    expect(screen.queryByTestId("cuboid-pdu-1")).not.toBeInTheDocument();
    expect(screen.getByText(/pdu-1: position known, catalog dimensions incomplete/)).toBeInTheDocument();
  });

  it("explains an empty scene instead of inventing a room", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      room_width_mm: null,
      room_height_mm: null,
      racks: [{ ...rack1, x_mm: null, y_mm: null, position_state: "missing" }],
      layout_state: "incomplete",
      incomplete_reasons: ["no_room_boundary", "rack_position_missing:1"],
    });
    renderWithProviders(<Layout3DPage />);
    expect(await screen.findByTestId("scene-empty")).toHaveTextContent(/nothing to scale a model against/);
    expect(screen.getByTestId("no-scene-racks")).toHaveTextContent(/without floor coordinates/);
    expect(screen.queryByTestId("scene-viewport")).not.toBeInTheDocument();
  });

  it("scales the whole scene from the approved boundary when one exists", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      room_width_mm: null,
      room_height_mm: null,
      boundary: { id: "b1", object_type: "room_outline", geometry_type: "rect", x_mm: 0, y_mm: 0, width_mm: 4000, height_mm: 3000, rotation_deg: 0, label: "Room boundary", source: "authoritative" },
    });
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    expect(screen.getByTestId("scene-boundary")).toBeInTheDocument();
    const pxPerMm = Number(screen.getByTestId("scene-viewport").getAttribute("data-scale-px-per-mm"));
    expect(pxPerMm).toBeCloseTo(760 / 4000, 5);
  });

  it("tints racks by the selected operational overlay using the same asset ids, and states the data source", async () => {
    const overlays: RoomOverlays = {
      room_id: "room-1", generated_at: "",
      overlays: { power: { source: "power_topology", truncated: false, items: [{ asset_id: "rack-1", asset_kind: "rack", state: "critical", reason: "No live power path; interrupted by BRK-A" }] } },
    };
    vi.mocked(floorPlansApi.getRoomOverlays).mockResolvedValue(overlays);
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");

    await user.selectOptions(screen.getByLabelText("Operational overlay"), "power");

    await waitFor(() => expect(screen.getByRole("button", { name: /Select rack Rack A1.*power: Critical — No live power path/ })).toBeInTheDocument());
    expect(floorPlansApi.getRoomOverlays).toHaveBeenCalledWith("room-1", ["power"]);
    expect(screen.getByTestId("overlay-legend")).toHaveTextContent(/Power topology and protection-device state/);
    await user.click(screen.getByRole("button", { name: /Select rack Rack A1/ }));
    expect(screen.getByTestId("selected-overlay")).toHaveTextContent("Critical: No live power path; interrupted by BRK-A");
    expect(screen.getByTestId("cuboid-rack-1").style.getPropertyValue("--c")).toBe("#ef4444");
  });

  it("shows missing overlay data as no data rather than as healthy", async () => {
    vi.mocked(floorPlansApi.getRoomOverlays).mockResolvedValue({ room_id: "room-1", generated_at: "", overlays: { environment: { source: "telemetry_latest", truncated: false, items: [] } } });
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    await user.selectOptions(screen.getByLabelText("Operational overlay"), "environment");
    await user.click(screen.getByRole("button", { name: /Select rack Rack A1/ }));
    expect(await screen.findByTestId("selected-overlay")).toHaveTextContent("No data for this asset");
  });

  it("adds a thermal layer on the floor plane at the same scale, with interpolated and measured elements kept apart", async () => {
    vi.mocked(coolingApi.getHeatMap).mockResolvedValue(heatMap());
    vi.mocked(coolingApi.getCoolingLayout).mockResolvedValue(coolingLayout);
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    expect(screen.queryByTestId("thermal-3d")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Thermal layer"), "temperature_c");

    const thermal = await screen.findByTestId("thermal-3d");
    await waitFor(() => expect(thermal.querySelectorAll('[data-provenance="interpolated"]').length).toBeGreaterThan(10));
    expect(thermal.querySelectorAll('g[data-provenance="measured"]')).toHaveLength(4);
    expect(coolingApi.getHeatMap).toHaveBeenCalledWith("room-1", "temperature_c");
    expect(thermal.querySelector('[data-zone-kind="hot_aisle"]')).toHaveAttribute("data-containment", "contained");
    expect(thermal.querySelectorAll("[data-unit-id]")).toHaveLength(2);
    expect(screen.getByTestId("thermal-3d-state")).toHaveTextContent("Healthy · 4 fresh, 0 stale, 0 missing");
    expect(screen.getByTestId("thermal-3d-panel")).toHaveTextContent("not validated CFD");
    expect(screen.getByTestId("legend-range")).toBeInTheDocument();
    // the scene's racks are still there at their authoritative position
    expect(screen.getByTestId("cuboid-rack-1")).toHaveAttribute("data-x-mm", "900");
  });

  it("3D thermal layer lists stale and missing sensors in text and does not call the map healthy", async () => {
    vi.mocked(coolingApi.getHeatMap).mockResolvedValue(partialMap());
    vi.mocked(coolingApi.getCoolingLayout).mockResolvedValue(coolingLayout);
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    await user.selectOptions(screen.getByLabelText("Thermal layer"), "temperature_c");
    expect(await screen.findByTestId("thermal-3d-state")).toHaveTextContent("Partial · 2 fresh, 1 stale, 1 missing");
    const list = screen.getByTestId("thermal-3d-sensors");
    expect(within(list).getByText(/Stale 3: Measured, stale/)).toBeInTheDocument();
    expect(within(list).getByText(/Missing 4: Missing, no value, never/)).toBeInTheDocument();
  });

  it("3D thermal layer reports a load failure as an alert", async () => {
    vi.mocked(coolingApi.getHeatMap).mockRejectedValue(new Error("nope"));
    vi.mocked(coolingApi.getCoolingLayout).mockResolvedValue(coolingLayout);
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);
    await screen.findByTestId("cuboid-rack-1");
    await user.selectOptions(screen.getByLabelText("Thermal layer"), "humidity_percent");
    expect(await screen.findByText("Could not load the heat map.")).toHaveAttribute("role", "alert");
  });

  it("shows an error state when the spatial view fails to load", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockRejectedValue(new Error("boom"));
    renderWithProviders(<Layout3DPage />);

    expect(await screen.findByText("Unable to load this room's spatial data.")).toBeInTheDocument();
  });
});
