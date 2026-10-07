import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as equipmentApi from "@/features/equipment/api";
import * as api from "@/features/network/api";
import { userWith } from "@/features/network/fixtures";
import { PassThroughsPage } from "@/features/network/PassThroughsPage";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api", async (importActual) => ({
  ...(await importActual<typeof api>()),
  listPassThroughs: vi.fn(),
  createPassThrough: vi.fn(),
  deletePassThrough: vi.fn(),
}));
vi.mock("@/features/equipment/api");

const port = (id: string, name: string) => ({ port_id: id, port_name: name, media_type: "copper", equipment_id: "e1", equipment_hostname: "pp-1", equipment_asset_tag: "EQ-9" });
const item = (): api.PassThrough => ({
  id: "t1", equipment_id: "e1", equipment_hostname: "pp-1", label: "ROW-1", version: 3, created_at: "x",
  ports: [port("f1", "Front01"), port("r1", "Rear01")],
});
const page = (items: api.PassThrough[]) => ({ items, total: items.length, limit: 200, offset: 0 });

describe("PassThroughsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue({
      items: [{ id: "e1", hostname: "pp-1", asset_tag: "EQ-9" }], total: 1, limit: 200, offset: 0,
    } as never);
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({
      ports: [{ id: "f1", display_name: "Front01" }, { id: "r1", display_name: "Rear01" }],
    } as never);
  });

  it("lists pass-throughs with a trace link and deletes with the stored version", async () => {
    vi.mocked(api.listPassThroughs).mockResolvedValue(page([item()]));
    vi.mocked(api.deletePassThrough).mockResolvedValue(undefined);
    renderWithProviders(<PassThroughsPage />, { user: userWith("cable:read", "cable:manage") });
    expect(await screen.findByText("Front01 ↔ Rear01")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Trace from Front01" })).toHaveAttribute("href", "/topology/trace?port=f1");
    await userEvent.click(screen.getByRole("button", { name: /Delete pass-through ROW-1/ }));
    await waitFor(() => expect(api.deletePassThrough).toHaveBeenCalled());
    expect(vi.mocked(api.deletePassThrough).mock.calls[0][0]).toMatchObject({ id: "t1", version: 3 });
  });

  it("records a pass-through between two ports of one device", async () => {
    vi.mocked(api.listPassThroughs).mockResolvedValue(page([]));
    vi.mocked(api.createPassThrough).mockResolvedValue(item());
    renderWithProviders(<PassThroughsPage />, { user: userWith("cable:read", "cable:manage") });
    expect(await screen.findByText("No pass-throughs recorded.")).toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText("Pass-through device"), "e1");
    await userEvent.selectOptions(await screen.findByLabelText("Pass-through port a"), "f1");
    const submit = screen.getByRole("button", { name: "Record pass-through" });
    expect(submit).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Pass-through port b"), "r1");
    await userEvent.click(submit);
    await waitFor(() => expect(api.createPassThrough).toHaveBeenCalledWith({ port_a_id: "f1", port_b_id: "r1", label: null }));
  });

  it("shows server refusals and hides management controls from read-only users", async () => {
    const { ApiError } = await vi.importActual<typeof import("@/lib/apiClient")>("@/lib/apiClient");
    vi.mocked(api.listPassThroughs).mockResolvedValue(page([item()]));
    vi.mocked(api.deletePassThrough).mockRejectedValue(new ApiError(409, "Conflict", "modified by another request", null));
    const view = renderWithProviders(<PassThroughsPage />, { user: userWith("cable:read", "cable:manage") });
    await userEvent.click(await screen.findByRole("button", { name: /Delete pass-through/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("modified by another request");
    view.unmount();
    renderWithProviders(<PassThroughsPage />, { user: userWith("cable:read") });
    await screen.findByText("Front01 ↔ Rear01");
    expect(screen.queryByRole("button", { name: /Delete pass-through/ })).toBeNull();
    expect(screen.queryByRole("form", { name: "Record a pass-through" })).toBeNull();
  });
});
