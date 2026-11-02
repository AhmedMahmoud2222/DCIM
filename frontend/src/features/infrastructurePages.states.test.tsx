import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FloorPlansPage } from "@/features/floor-plans/FloorPlansPage";
import { ManagedAssetsPage } from "@/features/managed-assets/ManagedAssetsPage";
import * as racksApi from "@/features/racks/api";
import { RacksPage } from "@/features/racks/RacksPage";
import { apiFetch, ApiError } from "@/lib/apiClient";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/racks/api");
vi.mock("@/lib/apiClient", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/apiClient")>()), apiFetch: vi.fn() }));

const page = (items: unknown[], total = items.length) => ({ items, total, limit: 200, offset: 0 });
const never = () => new Promise<never>(() => undefined);

/** The list request each page makes, how to answer it, and what the page says. */
const PAGES = [
  {
    name: "Managed Assets",
    ui: <ManagedAssetsPage />,
    what: "managed assets",
    permission: "managed_asset:read",
    empty: "No managed assets yet.",
    row: "TAG-1",
    answer: (value: unknown) => vi.mocked(apiFetch).mockImplementation(() => (value instanceof Promise ? value : Promise.resolve(value)) as never),
    fail: (error: unknown) => vi.mocked(apiFetch).mockRejectedValue(error),
    data: page([{ id: "a1", asset_tag: "TAG-1", asset_type: "equipment", lifecycle_status: "active", serial_number: null }]),
  },
  {
    name: "Floor Plans",
    ui: <FloorPlansPage />,
    what: "rooms",
    permission: "location:read",
    empty: "No rooms yet — create one under Locations first.",
    row: "Room One",
    answer: (value: unknown) => vi.mocked(racksApi.listRooms).mockReturnValue((value instanceof Promise ? value : Promise.resolve(value)) as never),
    fail: (error: unknown) => vi.mocked(racksApi.listRooms).mockRejectedValue(error),
    data: page([{ id: "r1", name: "Room One", code: "R1", room_type: "data_hall" }]),
  },
  {
    name: "Racks",
    ui: <RacksPage />,
    what: "racks",
    permission: "rack:read",
    empty: "No racks yet. Create one to get started.",
    row: "Rack A1",
    answer: (value: unknown) => {
      vi.mocked(racksApi.listRooms).mockResolvedValue(page([]) as never);
      vi.mocked(racksApi.listRacks).mockReturnValue((value instanceof Promise ? value : Promise.resolve(value)) as never);
    },
    fail: (error: unknown) => {
      vi.mocked(racksApi.listRooms).mockResolvedValue(page([]) as never);
      vi.mocked(racksApi.listRacks).mockRejectedValue(error);
    },
    data: page([{ id: "k1", name: "Rack A1", asset_tag: "RK-1", lifecycle_status: "active", placement: null }]),
  },
] as const;

beforeEach(() => {
  vi.resetAllMocks();
});

describe.each(PAGES)("$name list states", (p) => {
  const render = () => renderWithProviders(p.ui, { user: ADMINISTRATOR_TEST_USER });

  it("has one h1 and renders its rows in a named, scrollable, keyboard-focusable table", async () => {
    p.answer(p.data);
    render();

    expect(await screen.findByText(p.row)).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const region = screen.getByRole("region", { name: /table$/ });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.className).toContain("overflow-x-auto");
    for (const header of within(region).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
  });

  it("announces loading politely and shows no table, no rows and no empty message", async () => {
    p.answer(never());
    render();

    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent(`Loading ${p.what}…`);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
  });

  it("says the list is empty only after a successful empty response", async () => {
    p.answer(page([]));
    render();

    expect(await screen.findByText(p.empty)).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a failure as an alert with a working retry, never as an empty list", async () => {
    p.fail(new Error("Upstream timed out"));
    const user = userEvent.setup();
    render();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${p.what}.`);
    expect(alert).toHaveTextContent("Upstream timed out");
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    p.answer(p.data);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(p.row)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it(`shows Access restricted naming ${p.permission} for a 403, never an empty table`, async () => {
    p.fail(new ApiError(403, "Forbidden", "Missing permission", null));
    render();

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText(p.permission)).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Managed Assets lifecycle values stay visible", () => {
  it("shows an unrecognised lifecycle word as itself", async () => {
    vi.mocked(apiFetch).mockResolvedValue(page([{ id: "a1", asset_tag: "TAG-9", asset_type: "equipment", lifecycle_status: "quarantined", serial_number: "SN9" }]));
    renderWithProviders(<ManagedAssetsPage />, { user: ADMINISTRATOR_TEST_USER });

    expect(await screen.findByText("quarantined")).toBeInTheDocument();
    expect(screen.getByText("SN9")).toBeInTheDocument();
  });
});

describe("Racks keeps its actions", () => {
  it("keeps the New Rack and Bulk Import buttons in the header, gated as before", async () => {
    vi.mocked(racksApi.listRooms).mockResolvedValue(page([]) as never);
    vi.mocked(racksApi.listRacks).mockResolvedValue(page([]) as never);
    renderWithProviders(<RacksPage />, { user: { ...ADMINISTRATOR_TEST_USER, permission_codes: ["rack:read", "rack:import"] } });

    expect(await screen.findByRole("button", { name: "New Rack" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bulk Import" })).toBeInTheDocument();
  });
});
