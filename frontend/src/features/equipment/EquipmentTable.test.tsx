import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import * as equipmentApi from "@/features/equipment/api";
import * as racksApi from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";
import { Equipment, Page, Rack } from "@/types";

vi.mock("@/features/equipment/api");
vi.mock("@/features/racks/api");

const asset = (overrides: Record<string, unknown>) =>
  ({
    id: "e1",
    hostname: "core-sw-01",
    asset_tag: "AT-0001",
    lifecycle_status: "active",
    placement: null,
    ...overrides,
  }) as unknown as Equipment;

const page = (items: Equipment[], total = items.length): Page<Equipment> => ({ items, total, limit: 200, offset: 0 });

const rackedAt = (rack_id: string, u_start: number, u_end: number, side: string) => ({
  placement_type: "rack_mounted",
  rack_id,
  u_start,
  u_end,
  side,
});

beforeEach(() => {
  vi.mocked(racksApi.listRacks).mockResolvedValue({
    items: [{ id: "r1", name: "Rack A1" } as unknown as Rack],
    total: 1,
    limit: 200,
    offset: 0,
  });
  vi.mocked(equipmentApi.listEquipmentModels).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
});

describe("Equipment table", () => {
  it("is a named table in a keyboard-focusable scroll region with scoped column headers", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([asset({})]));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    const table = await screen.findByRole("table", { name: "Equipment" });
    const region = screen.getByRole("region", { name: "Equipment table" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toContainElement(table);
    expect(region.className).toContain("overflow-x-auto");

    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual(["Hostname", "Asset Tag", "Status", "Placement"]);
    for (const header of headers) expect(header).toHaveAttribute("scope", "col");
  });

  it("keeps every column and the row text: hostname link, asset tag, lifecycle word, rack placement", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(
      page([asset({ placement: rackedAt("r1", 12, 15, "front") })]),
    );
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    const row = (await screen.findByRole("link", { name: "core-sw-01" })).closest("tr")!;
    expect(screen.getByRole("link", { name: "core-sw-01" })).toHaveAttribute("href", "/equipment/e1");
    expect(within(row).getByText("AT-0001")).toHaveClass("font-mono");
    expect(within(row).getByText("active")).toBeInTheDocument();
    const cells = within(row).getAllByRole("cell");
    expect(cells).toHaveLength(4);
    expect(cells[3]).toHaveTextContent("Rack A1 — U12-15 (front)");
  });

  it("sets the U range in tabular figures that do not wrap, and shows non-rack placement and unplaced text", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(
      page([
        asset({ id: "e1", hostname: "a", placement: rackedAt("r1", 2, 3, "rear") }),
        asset({ id: "e2", hostname: "b", asset_tag: "AT-2", placement: { placement_type: "floor_standing" } }),
        asset({ id: "e3", hostname: "c", asset_tag: "AT-3", placement: null }),
      ]),
    );
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    await screen.findByRole("link", { name: "a" });
    const range = screen.getByText("U2-3");
    expect(range).toHaveClass("tabular-nums", "whitespace-nowrap");
    expect(screen.getByText("floor_standing")).toBeInTheDocument();
    // "unplaced" must be readable text, not the previous near-invisible slate-600.
    const unplaced = screen.getByText("unplaced");
    expect(unplaced.className).not.toMatch(/slate-600/);
    expect(unplaced.parentElement).toHaveClass("text-fg-secondary");
  });

  it("does not pin any column: on a narrow screen every column is reached by scrolling, none is covered", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([asset({})]));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    const table = await screen.findByRole("table", { name: "Equipment" });
    for (const cell of [...within(table).getAllByRole("columnheader"), ...within(table).getAllByRole("cell")]) {
      expect(cell.className).not.toMatch(/\bsticky\b/);
    }
    expect(screen.getByRole("region", { name: "Equipment table" }).className).toContain("overflow-x-auto");
  });

  it("states how many rows exist, and says so when the list is cut short", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([asset({})]));
    const { unmount } = renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });
    expect(await screen.findByTestId("row-count")).toHaveTextContent("1 equipment");
    unmount();

    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([asset({})], 340));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });
    expect(await screen.findByTestId("row-count")).toHaveTextContent("Showing 1 of 340 equipment. The remaining 339 are not listed here.");
  });

  it("shows the empty state inside the table, not a blank page", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([]));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    expect(await screen.findByText("No equipment yet.")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Equipment" })).toBeInTheDocument();
  });
});

describe("Equipment table states", () => {
  it("announces loading politely and keeps the page heading", async () => {
    vi.mocked(equipmentApi.listEquipment).mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Loading equipment…");
    expect(screen.getByRole("heading", { name: "Equipment" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("announces a failure as an alert and retries on request", async () => {
    vi.mocked(equipmentApi.listEquipment).mockRejectedValueOnce(new Error("Gateway timeout"));
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([asset({})]));
    const user = userEvent.setup();
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the equipment list.");
    expect(alert).toHaveTextContent("Gateway timeout");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "core-sw-01" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("shows Access restricted, naming the permission, for a 403 and never an empty or zero table", async () => {
    vi.mocked(equipmentApi.listEquipment).mockRejectedValue(new ApiError(403, "Forbidden", "Missing permission", null));
    renderWithProviders(<EquipmentPage />, { user: ADMINISTRATOR_TEST_USER });

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("equipment:read")).toBeInTheDocument();
    // The page keeps exactly one h1 (its own).
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("No equipment yet.")).not.toBeInTheDocument();
    expect(screen.queryByTestId("row-count")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
