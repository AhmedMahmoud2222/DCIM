import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as equipmentApi from "@/features/equipment/api";
import * as api from "@/features/network/api";
import { CablesPage } from "@/features/network/CablesPage";
import { cable, userWith } from "@/features/network/fixtures";
import { PassThroughsPage } from "@/features/network/PassThroughsPage";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api", async (importActual) => ({
  ...(await importActual<typeof api>()),
  listCables: vi.fn(),
  listPassThroughs: vi.fn(),
}));
vi.mock("@/features/equipment/api");

const page = (items: unknown[]) => ({ items, total: items.length, limit: 200, offset: 0 });
const never = () => new Promise<never>(() => undefined);
const passThrough = { id: "p1", label: "PP-01", equipment_hostname: "patch-panel-1", ports: [{ port_id: "x", port_name: "front 01", equipment_asset_tag: "AT" }, { port_id: "y", port_name: "rear 01", equipment_asset_tag: "AT" }] };

/** Both pages list records the user needs `cable:read` for; a list that fails must never read as empty. */
const PAGES = [
  {
    name: "Cables",
    ui: <CablesPage />,
    what: "cables",
    empty: "No cables recorded.",
    row: "Trace cable",
    answer: (v: unknown) => vi.mocked(api.listCables).mockReturnValue((v instanceof Promise ? v : Promise.resolve(v)) as never),
    fail: (e: unknown) => vi.mocked(api.listCables).mockRejectedValue(e),
    data: page([cable({ status: "installed" })]),
  },
  {
    name: "Pass-throughs",
    ui: <PassThroughsPage />,
    what: "pass-throughs",
    empty: "No pass-throughs recorded.",
    row: "PP-01",
    answer: (v: unknown) => vi.mocked(api.listPassThroughs).mockReturnValue((v instanceof Promise ? v : Promise.resolve(v)) as never),
    fail: (e: unknown) => vi.mocked(api.listPassThroughs).mockRejectedValue(e),
    data: page([passThrough]),
  },
] as const;

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(equipmentApi.listEquipment).mockResolvedValue(page([]) as never);
});

describe.each(PAGES)("$name list states", (p) => {
  const render = () => renderWithProviders(p.ui, { user: userWith("cable:read") });

  it("has one h1 and a named, keyboard-focusable scroll region with scoped headers", async () => {
    p.answer(p.data);
    render();

    await waitFor(() => expect(screen.getByRole("region", { name: /table$/ })).toBeInTheDocument());
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const region = screen.getByRole("region", { name: /table$/ });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.className).toContain("overflow-x-auto");
    for (const header of within(region).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
  });

  it("announces loading and shows no table and no empty message", async () => {
    p.answer(never());
    render();

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(`Loading ${p.what}…`);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
  });

  it("says the list is empty only after a successful empty response", async () => {
    p.answer(page([]));
    render();

    expect(await screen.findByText(p.empty)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a failure as an alert with retry, never as an empty list", async () => {
    p.fail(new Error("Upstream timed out"));
    const user = userEvent.setup();
    render();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${p.what}.`);
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    p.answer(p.data);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows Access restricted naming cable:read for a 403, never an empty table", async () => {
    p.fail(new ApiError(403, "Forbidden", "Missing permission", null));
    render();

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("cable:read")).toBeInTheDocument();
    expect(screen.queryByText(p.empty)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("Cable actions stay permission-driven", () => {
  it("shows Install and Delete only to a user who can manage and only for a planned cable", async () => {
    vi.mocked(api.listCables).mockResolvedValue(page([cable({ status: "planned" })]) as never);
    const { unmount } = renderWithProviders(<CablesPage />, { user: userWith("cable:read") });
    await screen.findByRole("table");
    expect(screen.queryByRole("button", { name: /^Install cable/ })).not.toBeInTheDocument();
    unmount();

    renderWithProviders(<CablesPage />, { user: userWith("cable:read", "cable:manage") });
    expect(await screen.findByRole("button", { name: /^Install cable/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Delete cable/ })).toBeInTheDocument();
  });
});
