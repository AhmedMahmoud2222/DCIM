import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Layout3DPage } from "@/features/spatial3d/Layout3DPage";
import * as floorPlansApi from "@/features/floor-plans/api";
import * as racksApi from "@/features/racks/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Page, Room, RoomSpatialView } from "@/types";

vi.mock("@/features/floor-plans/api");
vi.mock("@/features/racks/api");

const room: Room = { id: "room-1", floor_id: "floor-1", code: "A", name: "DC-1 Hall A", room_type: "hall", version: 1 };

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

const spatialView: RoomSpatialView = {
  room_id: "room-1",
  room_name: "DC-1 Hall A",
  active_floor_plan_id: null,
  active_floor_plan_revision: null,
  room_width_mm: 10_000,
  room_height_mm: 8_000,
  generated_at: "2026-09-27T00:00:00Z",
  racks: [
    { id: "rack-1", asset_tag: "RCK-001", name: "Rack A1", x_mm: 900, y_mm: 900, rotation_deg: 0, spatial_object_id: null, height_u: 42 },
  ],
  equipment: [],
  objects: [],
  rack_equipment: [
    { id: "eq-1", rack_id: "rack-1", asset_tag: "SRV-001", hostname: "srv-001", u_start: 3, u_end: 5, side: "front" },
  ],
};

describe("Layout3DPage", () => {
  beforeEach(() => {
    vi.mocked(racksApi.listRooms).mockResolvedValue(mockPage([room]));
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue(spatialView);
  });

  it("renders the active room's racks in the 3D scene", async () => {
    renderWithProviders(<Layout3DPage />);

    expect(await screen.findByRole("button", { name: /Select rack Rack A1, 42U capacity, 1 placed asset/ })).toBeInTheDocument();
  });

  it("shows rack context (capacity and placed equipment count) after selecting a rack", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Layout3DPage />);

    await user.click(await screen.findByRole("button", { name: /Select rack Rack A1/ }));

    expect(await screen.findByRole("heading", { name: "Rack A1" })).toBeInTheDocument();
    expect(screen.getByText("42U")).toBeInTheDocument();
  });

  it("flags a rack with no recorded x/y as a fallback position, not unplaced", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockResolvedValue({
      ...spatialView,
      racks: [{ ...spatialView.racks[0], x_mm: null, y_mm: null }],
    });
    renderWithProviders(<Layout3DPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: /position estimated/ })).toBeInTheDocument());
    expect(screen.getByText(/no recorded floor position yet/)).toBeInTheDocument();
  });

  it("shows an error state when the spatial view fails to load", async () => {
    vi.mocked(floorPlansApi.getRoomSpatialView).mockRejectedValue(new Error("boom"));
    renderWithProviders(<Layout3DPage />);

    expect(await screen.findByText("Unable to load this room's spatial data.")).toBeInTheDocument();
  });
});
