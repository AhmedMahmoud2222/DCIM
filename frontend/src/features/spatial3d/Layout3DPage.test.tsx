import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  return { ...actual, listRooms: vi.fn(), listUnplacedRacks: vi.fn() };
});

const UNPLACED_PAGE_SIZE = 10;
const emptyUnplacedPage: Page<Rack> = { items: [], total: 0, limit: UNPLACED_PAGE_SIZE, offset: 0 };

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
  mockedRacks.listUnplacedRacks.mockResolvedValue(emptyUnplacedPage);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Layout3DPage loading/empty/error states", () => {
  it("shows a loading state before spatial data resolves", async () => {
    mockedFloorPlans.getRoomSpatialView.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(await screen.findByText(/Loading authoritative spatial data/)).toBeInTheDocument();
  });

  it("shows an error state when the spatial view fails to load", async () => {
    mockedFloorPlans.getRoomSpatialView.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText(/Unable to load this room's spatial data/)).toBeInTheDocument();
  });

  it("shows an empty-inventory message when nothing is unplaced (total: 0)", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listUnplacedRacks.mockResolvedValue(emptyUnplacedPage);
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
    renderPage();

    for (const item of equipment) {
      expect(await screen.findByText(item.hostname as string)).toBeInTheDocument();
    }
  });

  it("respects front/rear side — a rear item does not appear on the front face", async () => {
    const equipment = [equipmentItem({ id: "rear-item", hostname: "rear-host", side: "rear", u_start: 5, u_end: 6 })];
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({ rack_equipment: equipment }));
    const { container } = renderPage();

    await screen.findByTitle(/Open rear-host/);
    const frontFace = container.querySelector(".layout3d-rack-front");
    const backFace = container.querySelector(".layout3d-rack-back");
    expect(within(frontFace as HTMLElement).queryByText("rear-host")).not.toBeInTheDocument();
    expect(within(backFace as HTMLElement).queryByText("rear-host")).toBeInTheDocument();
  });
});

describe("Layout3DPage unplaced inventory (GET /racks/unplaced)", () => {
  it("shows only what GET /racks/unplaced returns — coordinate-incomplete and elsewhere-placed racks never appear (the backend already excluded them)", async () => {
    // This endpoint is the sole authority for what's unplaced; the page renders exactly
    // its `items`, never re-deriving the set from a separately-fetched full inventory.
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView({
      racks: [
        { id: "rack-1", asset_tag: "RACK-1", name: "Placed rack", x_mm: 1000, y_mm: 1000, rotation_deg: 0, spatial_object_id: null, height_u: 12 },
        { id: "rack-2", asset_tag: "RACK-2", name: "Coordinate-incomplete rack", x_mm: null, y_mm: null, rotation_deg: null, spatial_object_id: null, height_u: 12 },
      ],
    }));
    mockedRacks.listUnplacedRacks.mockResolvedValue({
      items: [invRack({ id: "rack-3", name: "Genuinely unplaced rack", placement: null })],
      total: 1,
      limit: UNPLACED_PAGE_SIZE,
      offset: 0,
    });
    renderPage();

    await screen.findByText("Genuinely unplaced rack");
    const unplacedHeading = screen.getByText("Unplaced inventory");
    const unplacedSection = unplacedHeading.closest("div") as HTMLElement;
    expect(within(unplacedSection).getByText("Genuinely unplaced rack")).toBeInTheDocument();
    expect(within(unplacedSection).queryByText("Placed rack")).not.toBeInTheDocument();
    expect(within(unplacedSection).queryByText("Coordinate-incomplete rack")).not.toBeInTheDocument();
    expect(screen.getByText(/no recorded floor position yet/)).toBeInTheDocument();
  });

  it("shows an API-failure message distinct from the empty-result message", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listUnplacedRacks.mockRejectedValue(new Error("network down"));
    renderPage();

    expect(await screen.findByText("Unable to load unplaced racks.")).toBeInTheDocument();
    expect(screen.queryByText("Every inventory rack has an active room placement.")).not.toBeInTheDocument();
  });

  it("shows the full set with no pagination controls when everything fits on one page", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    mockedRacks.listUnplacedRacks.mockResolvedValue({
      items: [invRack({ id: "only-one", name: "Only Unplaced Rack", placement: null })],
      total: 1,
      limit: UNPLACED_PAGE_SIZE,
      offset: 0,
    });
    renderPage();

    await screen.findByText("Only Unplaced Rack");
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument();
  });

  it("shows a total count and Prev/Next controls, and pages correctly, when results span multiple pages", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const page1 = { items: Array.from({ length: UNPLACED_PAGE_SIZE }, (_, i) => invRack({ id: `p1-${i}`, name: `Rack P1-${i}`, placement: null })), total: 25, limit: UNPLACED_PAGE_SIZE, offset: 0 };
    const page2 = { items: Array.from({ length: UNPLACED_PAGE_SIZE }, (_, i) => invRack({ id: `p2-${i}`, name: `Rack P2-${i}`, placement: null })), total: 25, limit: UNPLACED_PAGE_SIZE, offset: UNPLACED_PAGE_SIZE };
    mockedRacks.listUnplacedRacks.mockImplementation((_limit, offset) => Promise.resolve(offset === 0 ? page1 : page2));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Showing 1–10 of 25")).toBeInTheDocument();
    expect(screen.getByText("Rack P1-0")).toBeInTheDocument();
    const prevButton = screen.getByRole("button", { name: "Prev" });
    const nextButton = screen.getByRole("button", { name: "Next" });
    expect(prevButton).toBeDisabled();
    expect(nextButton).not.toBeDisabled();

    await user.click(nextButton);

    expect(await screen.findByText("Showing 11–20 of 25")).toBeInTheDocument();
    expect(screen.getByText("Rack P2-0")).toBeInTheDocument();
    expect(screen.queryByText("Rack P1-0")).not.toBeInTheDocument();
    expect(mockedRacks.listUnplacedRacks).toHaveBeenCalledWith(UNPLACED_PAGE_SIZE, UNPLACED_PAGE_SIZE);
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

describe("Layout3DPage wheel-zoom (native, non-passive listener)", () => {
  // React's delegated onWheel is passive by default, so a synthetic wheel event never
  // exercises the bug (preventDefault() on it never throws). These tests dispatch a
  // real DOM WheelEvent directly, exactly as the browser does, to prove the native
  // listener attached to the viewport actually calls preventDefault() and zooms.
  function dispatchWheel(target: Element, deltaY: number) {
    const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event;
  }

  // The native listener is attached from a useEffect keyed to the viewport element's own
  // mount, which commits in the same render pass that "Room One" becomes visible — but
  // effects still run as a separate, asynchronous phase after that commit. A slower CI
  // runner can observe the text before the effect has flushed, so every test in this
  // block confirms attachment (via a zero-magnitude, side-effect-free probe event) before
  // asserting on real wheel behavior, rather than assuming one findByText resolution
  // implies the effect already ran.
  async function waitForWheelListenerAttached(viewport: HTMLElement) {
    await waitFor(() => expect(dispatchWheel(viewport, 0).defaultPrevented).toBe(true));
  }

  it("default-prevents a cancelable wheel event over the viewport", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;

    await waitForWheelListenerAttached(viewport);
    expect(dispatchWheel(viewport, -100).defaultPrevented).toBe(true);
  });

  it("one wheel event changes the zoom scale exactly once", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    const world = container.querySelector(".layout3d-world") as HTMLElement;
    const zoomSlider = screen.getByLabelText("Zoom") as HTMLInputElement;
    await waitForWheelListenerAttached(viewport);
    const before = Number(zoomSlider.value);

    dispatchWheel(viewport, -100);
    await waitFor(() => expect(Number(zoomSlider.value)).toBeCloseTo(before + 0.1, 5));
    expect(world.style.transform).toContain(`scale(${Number(zoomSlider.value)})`);
  });

  it("clamps zoom at the maximum after repeated zoom-in wheel events", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    const zoomSlider = screen.getByLabelText("Zoom") as HTMLInputElement;
    await waitForWheelListenerAttached(viewport);

    for (let i = 0; i < 30; i++) dispatchWheel(viewport, -1000);
    await waitFor(() => expect(Number(zoomSlider.value)).toBeCloseTo(1.45, 5));

    dispatchWheel(viewport, -1000);
    await waitFor(() => expect(Number(zoomSlider.value)).toBeCloseTo(1.45, 5));
  });

  it("clamps zoom at the minimum after repeated zoom-out wheel events", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    const zoomSlider = screen.getByLabelText("Zoom") as HTMLInputElement;
    await waitForWheelListenerAttached(viewport);

    for (let i = 0; i < 30; i++) dispatchWheel(viewport, 1000);
    await waitFor(() => expect(Number(zoomSlider.value)).toBeCloseTo(0.45, 5));

    dispatchWheel(viewport, 1000);
    await waitFor(() => expect(Number(zoomSlider.value)).toBeCloseTo(0.45, 5));
  });

  it("removes the native wheel listener when the viewport unmounts", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container, unmount } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    await waitForWheelListenerAttached(viewport);
    const removeSpy = vi.spyOn(viewport, "removeEventListener");

    unmount();

    expect(removeSpy).toHaveBeenCalledWith("wheel", expect.any(Function));
  });

  it("the zoom slider still changes the scene transform independently of the wheel listener", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const { container } = renderPage();
    await screen.findByText("Room One");
    const world = container.querySelector(".layout3d-world") as HTMLElement;
    const before = world.style.transform;

    fireEvent.change(screen.getByLabelText("Zoom"), { target: { value: "1.2" } });

    await waitFor(() => expect(world.style.transform).not.toBe(before));
    expect(world.style.transform).toContain("scale(1.2)");
  });

  it("Reset / fit still restores the default transform after wheel-zooming", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("Room One");
    const viewport = container.querySelector(".layout3d-viewport") as HTMLElement;
    const world = container.querySelector(".layout3d-world") as HTMLElement;
    const original = world.style.transform;

    dispatchWheel(viewport, -300);
    await waitFor(() => expect(world.style.transform).not.toBe(original));

    await user.click(screen.getByRole("button", { name: "Reset / fit" }));
    await waitFor(() => expect(world.style.transform).toBe(original));
  });
});

describe("Layout3DPage keyboard camera controls", () => {
  it("pans the scene from the keyboard when the viewport itself is focused", async () => {
    mockedFloorPlans.getRoomSpatialView.mockResolvedValue(baseView());
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
