import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as equipmentApi from "@/features/equipment/api";
import * as api from "@/features/network/api";
import { CablesPage } from "@/features/network/CablesPage";
import { cable, userWith } from "@/features/network/fixtures";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api", async (importActual) => ({ ...(await importActual<typeof api>()), listCables: vi.fn(), installCable: vi.fn(), removeCable: vi.fn(), deleteCable: vi.fn(), createCable: vi.fn() }));
vi.mock("@/features/equipment/api");

const page = (...items: api.Cable[]) => ({ items, total: items.length, limit: 200, offset: 0 });

describe("CablesPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  });

  it("shows identity, both endpoints, lifecycle and provenance", async () => {
    vi.mocked(api.listCables).mockResolvedValue(page(
      cable({ status: "installed", installed_at: "2026-10-02T00:00:00Z", source: "discovery_confirmed" }),
    ));
    renderWithProviders(<CablesPage />, { user: userWith("cable:read") });
    expect(await screen.findByText("PP1-A01")).toBeInTheDocument();
    expect(screen.getByText("edge-sw-1 · Eth1/1")).toBeInTheDocument();
    expect(screen.getByText("core-sw-1 · Eth1/24")).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /PP1-A01/ })).getByText("installed")).toBeInTheDocument();
    expect(screen.getByText("from confirmed discovery")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Trace cable PP1-A01" })).toHaveAttribute("href", "/topology/trace?port=pa");
  });

  it("masks an endpoint outside the viewer's scope", async () => {
    const masked = cable();
    masked.endpoints[1] = { end: "B", restricted: true, port: null };
    vi.mocked(api.listCables).mockResolvedValue(page(masked));
    renderWithProviders(<CablesPage />, { user: userWith("cable:read") });
    expect(await screen.findByText("restricted")).toBeInTheDocument();
    expect(screen.queryByText(/core-sw-1/)).toBeNull();
  });

  it("read-only users get no lifecycle or creation controls", async () => {
    vi.mocked(api.listCables).mockResolvedValue(page(cable()));
    renderWithProviders(<CablesPage />, { user: userWith("cable:read") });
    await screen.findByText("PP1-A01");
    expect(screen.queryByRole("button", { name: /Install cable/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Remove cable/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Delete cable/ })).toBeNull();
    expect(screen.queryByLabelText("Record a cable")).toBeNull();
  });

  it("installs, removes and deletes using the cable's version; removed cables have no actions", async () => {
    vi.mocked(api.listCables).mockResolvedValue(page(cable(), cable({ id: "c2", label: "OLD", status: "removed", removed_at: "2026-10-03T00:00:00Z" })));
    vi.mocked(api.installCable).mockResolvedValue(cable({ status: "installed" }));
    vi.mocked(api.removeCable).mockResolvedValue(cable({ status: "removed" }));
    vi.mocked(api.deleteCable).mockResolvedValue(undefined);
    renderWithProviders(<CablesPage />, { user: userWith("cable:read", "cable:manage") });
    await userEvent.click(await screen.findByRole("button", { name: "Install cable PP1-A01" }));
    await waitFor(() => expect(api.installCable).toHaveBeenCalledWith(expect.objectContaining({ id: "c1", version: 1 })));
    await userEvent.click(screen.getByRole("button", { name: "Remove cable PP1-A01" }));
    await waitFor(() => expect(api.removeCable).toHaveBeenCalled());
    await userEvent.click(screen.getByRole("button", { name: "Delete cable PP1-A01" }));
    await waitFor(() => expect(api.deleteCable).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Remove cable OLD" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Install cable OLD" })).toBeNull();
  });

  it("surfaces a server refusal", async () => {
    vi.mocked(api.listCables).mockResolvedValue(page(cable()));
    const { ApiError } = await vi.importActual<typeof import("@/lib/apiClient")>("@/lib/apiClient");
    vi.mocked(api.installCable).mockRejectedValue(new ApiError(409, "Conflict", "Only a planned cable can be installed", null));
    renderWithProviders(<CablesPage />, { user: userWith("cable:read", "cable:manage") });
    await userEvent.click(await screen.findByRole("button", { name: "Install cable PP1-A01" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only a planned cable");
  });
});
