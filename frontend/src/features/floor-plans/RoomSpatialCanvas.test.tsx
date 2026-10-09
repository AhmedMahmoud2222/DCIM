import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RoomSpatialCanvas } from "@/features/floor-plans/RoomSpatialCanvas";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { RoomOverlays, RoomSpatialView } from "@/types";

const base: RoomSpatialView = {
  room_id: "room-1", room_name: "Hall A", active_floor_plan_id: "fp", active_floor_plan_revision: 2, room_width_mm: 6000, room_height_mm: 4000,
  generated_at: "", racks: [], equipment: [], objects: [], rack_equipment: [], layout_state: "validated", incomplete_reasons: [],
  calibration: { id: "c", method: "two_point", source_units: "px", mm_per_unit: 10, error_bound_mm: 12.5, relative_error: 0.005, confidence: "medium", created_at: "" },
};
const rack = (over = {}) => ({
  id: "k1", asset_tag: "RK-1", name: "Rack 1", x_mm: 1000, y_mm: 2000, rotation_deg: 0, spatial_object_id: null, height_u: 42,
  width_mm: 600, depth_mm: 1000, height_mm: 1867, ...over,
});

describe("RoomSpatialCanvas (dimensionally accurate 2D)", () => {
  it("draws a rack at its real catalog width and depth under a single scale", () => {
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, racks: [rack(), rack({ id: "k2", name: "Rack 2", x_mm: 2000, width_mm: 800, depth_mm: 1200 })] }} />);
    const k1 = screen.getByTestId("rack-k1");
    const k2 = screen.getByTestId("rack-k2");
    const w1 = Number(k1.getAttribute("data-width-px"));
    const d1 = Number(k1.getAttribute("data-depth-px"));
    expect(w1 / d1).toBeCloseTo(0.6, 4);
    // the 800 x 1200 rack is exactly 800/600 and 1200/1000 times larger: one scale for everything
    expect(Number(k2.getAttribute("data-width-px")) / w1).toBeCloseTo(800 / 600, 4);
    expect(Number(k2.getAttribute("data-depth-px")) / d1).toBeCloseTo(1200 / 1000, 4);
  });

  it("does not use a hard-coded footprint: a different catalog size changes the drawn size", () => {
    const { unmount } = renderWithProviders(<RoomSpatialCanvas view={{ ...base, racks: [rack({ width_mm: 450, depth_mm: 700 })] }} />);
    const small = Number(screen.getByTestId("rack-k1").getAttribute("data-depth-px"));
    unmount();
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, racks: [rack({ width_mm: 450, depth_mm: 1400 })] }} />);
    expect(Number(screen.getByTestId("rack-k1").getAttribute("data-depth-px")) / small).toBeCloseTo(2, 2);
  });

  it("applies authoritative orientation", () => {
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, racks: [rack({ rotation_deg: 90 })] }} />);
    expect(screen.getByTestId("rack-k1").getAttribute("transform")).toMatch(/^rotate\(90 /);
  });

  it("shows engineering grid, ruler, room origin and scale bar, and can hide the grid", () => {
    const { rerender } = renderWithProviders(<RoomSpatialCanvas view={base} showGrid />);
    const chrome = screen.getByTestId("scene-chrome");
    expect(Number(chrome.getAttribute("data-grid-interval-mm"))).toBeGreaterThan(0);
    expect(chrome.querySelectorAll("line").length).toBeGreaterThan(8);
    expect(screen.getByTestId("room-origin")).toHaveTextContent("room origin (0, 0)");
    expect(screen.getByTestId("scale-bar")).toHaveTextContent(/\d+(\.\d+)? ?(m|mm)/);
    rerender(<RoomSpatialCanvas view={base} showGrid={false} />);
    // with the grid off only the origin cross (2 lines) and the scale bar (3 lines) remain
    expect(screen.getByTestId("scene-chrome").querySelectorAll("line").length).toBeLessThanOrEqual(5);
  });

  it("uses a finer grid when the plan is small and a coarser one when it is large", () => {
    const small = renderWithProviders(<RoomSpatialCanvas view={{ ...base, room_width_mm: 3000, room_height_mm: 2000 }} />);
    const fine = Number(screen.getByTestId("scene-chrome").getAttribute("data-grid-interval-mm"));
    small.unmount();
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, room_width_mm: 60000, room_height_mm: 40000 }} />);
    expect(Number(screen.getByTestId("scene-chrome").getAttribute("data-grid-interval-mm"))).toBeGreaterThan(fine);
  });

  it("states the calibration and its error bound next to the plan", () => {
    renderWithProviders(<RoomSpatialCanvas view={base} />);
    expect(screen.getByTestId("calibration-summary")).toHaveTextContent("1 px = 10 mm");
    expect(screen.getByTestId("calibration-summary")).toHaveTextContent("error ≤ ±12.5 mm");
    expect(screen.getByTestId("calibration-summary")).toHaveTextContent("medium confidence");
    expect(screen.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "validated");
  });

  it("lists racks with no position or footprint instead of drawing them at a guessed place", () => {
    renderWithProviders(
      <RoomSpatialCanvas
        view={{
          ...base, layout_state: "incomplete", incomplete_reasons: ["rack_position_missing:1"],
          racks: [rack(), rack({ id: "k2", name: "Rack 2", x_mm: null, y_mm: null }), rack({ id: "k3", name: "Rack 3", width_mm: null })],
        }}
      />,
    );
    expect(screen.getByTestId("rack-k1")).toBeInTheDocument();
    expect(screen.queryByTestId("rack-k2")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rack-k3")).not.toBeInTheDocument();
    const list = screen.getByTestId("unpositioned-list");
    expect(within(list).getByText(/Rack 2/)).toBeInTheDocument();
    expect(list).toHaveTextContent("no floor position recorded");
    expect(list).toHaveTextContent("catalog dimensions missing");
    expect(screen.getByTestId("incomplete-reasons")).toHaveTextContent("1 rack placed in this room has no recorded floor position");
  });

  it("explains an empty plan instead of inventing a canvas size", () => {
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, room_width_mm: null, room_height_mm: null, calibration: null, layout_state: "incomplete", incomplete_reasons: ["no_room_boundary"] }} />);
    expect(screen.getByTestId("spatial-2d-empty")).toHaveTextContent(/no room boundary/i);
    expect(screen.queryByRole("group", { name: /Scaled 2D plan/ })).not.toBeInTheDocument();
  });

  it("draws the approved boundary polygon and imported walls from their stored millimetre geometry", () => {
    renderWithProviders(
      <RoomSpatialCanvas
        view={{
          ...base,
          room_width_mm: null, room_height_mm: null,
          boundary: { id: "b", object_type: "room_outline", geometry_type: "polygon", x_mm: 0, y_mm: 0, width_mm: 6000, height_mm: 4000, rotation_deg: 0, label: null, source: "authoritative", geometry_data: { points: [[0, 0], [6000, 0], [6000, 2000], [3000, 4000], [0, 4000]] } },
          objects: [{ id: "w", object_type: "wall", geometry_type: "path", x_mm: 0, y_mm: 0, width_mm: 6000, height_mm: 0, rotation_deg: 0, label: null, source: "imported", geometry_data: { points: [[0, 0], [6000, 0]] } }],
        }}
      />,
    );
    expect(screen.getByTestId("scale-source")).toHaveTextContent("approved room boundary");
    expect(document.querySelector('[data-object-type="wall"] polyline')).not.toBeNull();
  });

  it("renders floor equipment at catalog footprint and an unmistakable pin when the footprint is unknown", () => {
    renderWithProviders(
      <RoomSpatialCanvas
        view={{
          ...base,
          equipment: [
            { id: "e1", asset_tag: "E1", hostname: "ups-1", placement_type: "floor_standing", spatial_object_id: "o", x_mm: 4000, y_mm: 500, rotation_deg: 0, width_mm: 800, depth_mm: 900, height_mm: 889, position_state: "placed", dimensions_state: "complete" },
            { id: "e2", asset_tag: "E2", hostname: "pdu-1", placement_type: "floor_standing", spatial_object_id: "o2", x_mm: 100, y_mm: 100, width_mm: null, depth_mm: null, height_mm: null, position_state: "placed", dimensions_state: "missing" },
          ],
        }}
      />,
    );
    expect(document.querySelector('[data-asset-id="e1"] rect')).not.toBeNull();
    expect(document.querySelector('[data-asset-id="e2"][data-footprint="unknown"]')).not.toBeNull();
  });

  it("colours assets by the selected overlay with a non-colour symbol and a text alternative", () => {
    const overlays: RoomOverlays = {
      room_id: "room-1", generated_at: "",
      overlays: { power: { source: "power_topology", truncated: false, items: [{ asset_id: "k1", asset_kind: "rack", state: "warning", reason: "Redundant feeds share an upstream dependency." }] } },
    };
    renderWithProviders(<RoomSpatialCanvas view={{ ...base, racks: [rack()] }} overlay="power" overlays={overlays} />);
    const link = screen.getByRole("link", { name: /Rack Rack 1.*power: Warning — Redundant feeds share/ });
    expect(link.querySelector("rect")?.getAttribute("stroke")).toBe("#f59e0b");
    expect(link).toHaveTextContent("!");
    expect(screen.getByTestId("legend-critical")).toBeInTheDocument();
    expect(within(screen.getByTestId("spatial-table")).getByText(/Warning: Redundant feeds share/)).toBeInTheDocument();
  });

  it("distinguishes measured, stale and missing environment data", () => {
    const overlays: RoomOverlays = {
      room_id: "room-1", generated_at: "",
      overlays: {
        environment: {
          source: "telemetry_latest", truncated: false, estimated_values: false,
          items: [
            { asset_id: "k1", asset_kind: "rack", state: "normal", reason: "Fresh measured value.", metric: "temperature_c", value: 24.5, unit: "degC", data_quality: "measured" },
            { asset_id: "k2", asset_kind: "rack", state: "warning", reason: "Reading is older than three poll intervals.", metric: "temperature_c", value: 31, unit: "degC", data_quality: "stale" },
            { asset_id: "k3", asset_kind: "rack", state: "unavailable", reason: "No temperature or humidity telemetry is mapped to this asset.", data_quality: "missing" },
          ],
        },
      },
    };
    renderWithProviders(
      <RoomSpatialCanvas view={{ ...base, racks: [rack(), rack({ id: "k2", name: "R2", x_mm: 2000 }), rack({ id: "k3", name: "R3", x_mm: 3000 })] }} overlay="environment" overlays={overlays} />,
    );
    expect(screen.getByRole("link", { name: /Rack Rack 1.*measured/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Rack R2.*stale/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Rack R3.*No data/ })).toBeInTheDocument();
  });
});
