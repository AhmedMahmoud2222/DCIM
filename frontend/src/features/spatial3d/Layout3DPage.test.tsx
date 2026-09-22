import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as floorPlansApi from "@/features/floor-plans/api";
import * as racksApi from "@/features/racks/api";
import type { Page, Rack, RackMountedEquipment, Room, RoomSpatialView } from "@/types";
import { Layout3DPage } from "./Layout3DPage";

vi.mock("@/features/floor-plans/api", async () => {
  const actual = await vi.importActual<typeof floorPlansApi>("@/features/floor-plans/api");
  return { ...actual, getRoomSpatialView: vi.fn() };
});
vi.mock("@/features/racks/api", async () => {
  const actual = await vi.importActual<typeof racksApi>("@/features/racks/api");
  return { ...actual, listRooms: vi.fn(), listRacks: vi.fn() };
});

const mockedFloorPlans = vi.mocked(floorPlansApi);
const mockedRacks = vi.mocked(racksApi);

const rooms: Page<Room> = { items: [{ id: "room-1", floor_id: "floor-1", code: "R1", name: "Room One", room_type: "data_hall", version: 1 }], total: 1, limit: 200, offset: 0 };

function equipmentItem(overrides: Partial<RackMountedEquipment>): RackMountedEquipment {
  return { id: "eq-default", asset_tag: "EQ-0", hostname: "eq-0", rack_id: "rack-1", u_start: 1, u_end: 2, side: "front", ...overrides };
}

function baseView(overrides: Partial<RoomSpatialView> = {}): RoomSpatialView {
  return {
    room_id: "room-1",
    room_name: "Room One",
    active_floor_plan_id: null,
    active_floor_plan_revision: null,
    room_width_mm: 10000,
    room_height_mm: 10000,
    generated_at: "2026-01-01T00:00:00Z",
    racks: [{ id: "rack-1", asset_tag: "RACK-1", name: "Rack 1", x_mm: 1000, y_mm: 1000, rotation_deg: 0, spatial_object_id: null, height_u: 12 }],
    equipment: [],
    rack_equipment: [],
    objects: [],
    ...overrides,
  };
}

function invRack(overrides: Partial<Rack>): Rack {
  return {
    id: "inv-default", asset_tag: "INV-0", lifecycle_status: "active", model_revision_id: "rev-1", name: "Inv rack",
    owner: null, notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null, ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(<Layout3DPage />, { wrapper });
}

beforeEach(() => {
  mockedRacks.listRooms.mockResolvedValue(rooms);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Layout3DPage loading/empty/error states", () => {
  it("shows a loading state before spatial data resolves", async () => {
    mockedFloorPlans.getRoomSpatialView.mockReturnValue(new Promise(() => {}));
    mockedRacks.listRacks.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(await screen.findByText(/Loading authoritative spatial data/)).toBeInTheDocument();
  });

  it("shows an error state when the spatial view fails to load", async () => {
    mockedFloorPlans.getRoomSpatialView.mockRejectedValue(new Error("boom"));
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();
    expect(await screen.findByText(/Unable to load this room's spatial data/)).toBeInTheDocument();
  });

  it("shows an empty-inventory message when nothing is unplaced", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listRacks.mockResolvedValue({ items: [invRack({ id: "rack-1", placement: { room_id: "room-1", x_mm: 1, y_mm: 1, rotation_deg: 0, effective_from: "2026-01-01" } })], total: 1, limit: 200, offset: 0 });
    renderPage();
    expect(await screen.findByText("Every inventory rack has an active room placement.")).toBeInTheDocument();
  });
});

describe("Layout3DPage U-based equipment rendering", () => {
  it("positions equipment by u_start/u_end, not array order, and sizes height by U span", async () => {
    // Deliberately listed out of U order to prove array position never drives layout.
    // Links render their hostname as visible text (the accessible name); the U-range
    // detail lives in `title`, which `getByTitle` reads directly since these are plain
    // HTML anchors (unlike the SVG <title> child element in NetworkPage's topology).
    const equipment = [
      equipmentItem({ id: "top", hostname: "top-host", u_start: 12, u_end: 13 }), // topmost U of a 12U rack
      equipmentItem({ id: "tall", hostname: "tall-host", u_start: 3, u_end: 7 }), // 4U tall, mid-rack
      equipmentItem({ id: "bottom", hostname: "bottom-host", u_start: 1, u_end: 2 }), // bottom U
    ];
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({ rack_equipment: equipment }));
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();

    const topLink = await screen.findByTitle(/Open top-host · U12–U12/);
    expect(topLink.style.top).toBe("0%");

    const tallLink = screen.getByTitle(/Open tall-host · U3–U6/);
    expect(parseFloat(tallLink.style.height)).toBeCloseTo((4 / 12) * 100, 1);

    const bottomLink = screen.getByTitle(/Open bottom-host · U1–U1/);
    expect(parseFloat(bottomLink.style.top)).toBeCloseTo((11 / 12) * 100, 1);
  });

  it("renders every placed equipment item — no cap at a fixed count", async () => {
    const equipment = Array.from({ length: 10 }, (_, index) =>
      equipmentItem({ id: `eq-${index}`, asset_tag: `EQ-${index}`, hostname: `host-${index}`, u_start: index + 1, u_end: index + 2 }),
    );
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({ racks: [{ id: "rack-1", asset_tag: "RACK-1", name: "Rack 1", x_mm: 1000, y_mm: 1000, rotation_deg: 0, spatial_object_id: null, height_u: 42 }], rack_equipment: equipment }));
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();

    for (const item of equipment) {
      expect(await screen.findByText(item.hostname as string)).toBeInTheDocument();
    }
  });

  it("respects front/rear side — a rear item does not appear on the front face", async () => {
    const equipment = [equipmentItem({ id: "rear-item", hostname: "rear-host", side: "rear", u_start: 5, u_end: 6 })];
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({ rack_equipment: equipment }));
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    const { container } = renderPage();

    await screen.findByTitle(/Open rear-host/);
    const frontFace = container.querySelector(".layout3d-rack-front");
    const backFace = container.querySelector(".layout3d-rack-back");
    expect(within(frontFace as HTMLElement).queryByText("rear-host")).not.toBeInTheDocument();
    expect(within(backFace as HTMLElement).queryByText("rear-host")).toBeInTheDocument();
  });
});

describe("Layout3DPage unplaced inventory", () => {
  it("lists a genuinely unplaced rack (no placement anywhere), separate from coordinate-incomplete ones", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({
      racks: [
        { id: "rack-1", asset_tag: "RACK-1", name: "Placed rack", x_mm: 1000, y_mm: 1000, rotation_deg: 0, spatial_object_id: null, height_u: 12 },
        { id: "rack-2", asset_tag: "RACK-2", name: "Coordinate-incomplete rack", x_mm: null, y_mm: null, rotation_deg: null, spatial_object_id: null, height_u: 12 },
      ],
    }));
    mockedRacks.listRacks.mockResolvedValue({
      items: [
        invRack({ id: "rack-1", name: "Placed rack", placement: { room_id: "room-1", x_mm: 1000, y_mm: 1000, rotation_deg: 0, effective_from: "2026-01-01" } }),
        invRack({ id: "rack-2", name: "Coordinate-incomplete rack", placement: { room_id: "room-1", x_mm: null, y_mm: null, rotation_deg: null, effective_from: "2026-01-01" } }),
        invRack({ id: "rack-3", name: "Genuinely unplaced rack", placement: null }),
        invRack({ id: "rack-4", name: "Rack in another room", placement: { room_id: "room-other", x_mm: 5, y_mm: 5, rotation_deg: 0, effective_from: "2026-01-01" } }),
      ],
      total: 4,
      limit: 200,
      offset: 0,
    });
    renderPage();

    // The "Unplaced inventory" heading renders immediately; wait for its actual content
    // (the racks-inventory query resolving) rather than the heading itself.
    await screen.findByText("Genuinely unplaced rack");
    const unplacedHeading = screen.getByText("Unplaced inventory");
    const unplacedSection = unplacedHeading.closest("div") as HTMLElement;
    expect(within(unplacedSection).getByText("Genuinely unplaced rack")).toBeInTheDocument();
    expect(within(unplacedSection).queryByText("Placed rack")).not.toBeInTheDocument();
    expect(within(unplacedSection).queryByText("Coordinate-incomplete rack")).not.toBeInTheDocument();
    expect(within(unplacedSection).queryByText("Rack in another room")).not.toBeInTheDocument();
    expect(screen.getByText(/no recorded floor position yet/)).toBeInTheDocument();
  });
});

describe("Layout3DPage equipment link keyboard activation", () => {
  it("Enter on a focused equipment link activates the link, not the ancestor rack-selection control", async () => {
    // Regression: the rack-selection div's own Enter/Space handler must not intercept
    // a keydown bubbling up from a nested equipment link — that would both hijack
    // keyboard navigation to the equipment page and preventDefault the link's native
    // activation.
    const equipment = [equipmentItem({ id: "eq-1", hostname: "kbd-host" })];
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({ rack_equipment: equipment }));
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    const user = userEvent.setup();
    renderPage();

    const link = await screen.findByText("kbd-host");
    link.focus();
    await user.keyboard("{Enter}");

    // The rack-selection side effect (onSelect) must NOT have fired: the aside still
    // shows the unselected prompt, not "Open rack elevation".
    expect(screen.getByText("Select a rack in the scene.")).toBeInTheDocument();
    expect(screen.queryByText("Open rack elevation")).not.toBeInTheDocument();
  });
});

describe("Layout3DPage keyboard camera controls", () => {
  it("pans the scene from the keyboard when the viewport itself is focused", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    const user = userEvent.setup();
    const { container } = renderPage();

    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    const world = container.querySelector(".layout3d-world") as HTMLElement;
    const before = world.style.transform;
    viewport.focus();
    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(world.style.transform).not.toBe(before));
  });

  it("does not pan when arrow keys are pressed on a nested control (e.g. a focused rack)", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    const user = userEvent.setup();
    const { container } = renderPage();

    const rackButton = await screen.findByRole("button", { name: /Select rack Rack 1/ });
    const world = container.querySelector(".layout3d-world") as HTMLElement;
    const before = world.style.transform;
    rackButton.focus();
    await user.keyboard("{ArrowRight}");
    expect(world.style.transform).toBe(before);
  });
});
