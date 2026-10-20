import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/network/api";
import { cable, neighbor, userWith } from "@/features/network/fixtures";
import { NeighborReviewPage } from "@/features/network/NeighborReviewPage";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api");

const page = (...items: ReturnType<typeof neighbor>[]) => ({ items, total: items.length, limit: 200, offset: 0 });
const RECONCILER = userWith("discovery:read", "discovery:reconcile");

describe("NeighborReviewPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("lists evidence with its state and a stale marker, and counts open states", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(
      neighbor(),
      neighbor({ id: "n2", reconciliation_state: "ambiguous", remote_system_name: "dup-sw", effective_status: "stale" }),
      neighbor({ id: "n3", reconciliation_state: "unmatched", remote_system_name: "mystery" }),
    ));
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    expect(await screen.findByText("core-sw-1")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByLabelText("State counts")).toHaveTextContent("ambiguous: 1 · unmatched: 1 · proposed: 1 · conflict: 0");
  });

  it("explains an ambiguous neighbor and only offers explicit manual resolution", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor({
      reconciliation_state: "ambiguous", match_evidence: { remote_device: { state: "ambiguous", reason: "several inventory items share this management address" } },
    })));
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor core-sw-1/ }));
    expect(await screen.findByText(/Nothing was chosen/)).toBeInTheDocument();
    expect(screen.getByText(/several inventory items share this management address/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm proposal" })).toBeNull();
    expect(screen.getByRole("button", { name: "Confirm with these ports" })).toBeDisabled();
  });

  it("confirms a proposal with the neighbor's version", async () => {
    const proposed = neighbor();
    vi.mocked(api.listNeighbors).mockResolvedValue(page(proposed));
    vi.mocked(api.confirmNeighbor).mockResolvedValue({ ...proposed, reconciliation_state: "confirmed" });
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Confirm proposal" }));
    await waitFor(() => expect(api.confirmNeighbor).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.confirmNeighbor).mock.calls[0][0]).toMatchObject({ id: "n1", version: 4 });
    const proposal = proposed.match_evidence.proposal!;
    expect(vi.mocked(api.confirmNeighbor).mock.calls[0][1]).toMatchObject({
      expected_local_port_id: proposal.local_port_id, expected_remote_port_id: proposal.remote_port_id,
    });
  });

  it("reloads the neighbor after a 409 so the operator reviews the changed proposal", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    const { ApiError } = await vi.importActual<typeof import("@/lib/apiClient")>("@/lib/apiClient");
    vi.mocked(api.confirmNeighbor).mockRejectedValue(new ApiError(409, "Conflict", "The proposal changed since it was displayed", null));
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    const before = vi.mocked(api.listNeighbors).mock.calls.length;
    await userEvent.click(await screen.findByRole("button", { name: "Confirm proposal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("proposal changed");
    await waitFor(() => expect(vi.mocked(api.listNeighbors).mock.calls.length).toBeGreaterThan(before));
  });

  it("sends the explicitly named ports for a manual resolution", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor({ reconciliation_state: "unmatched", match_evidence: {} })));
    vi.mocked(api.confirmNeighbor).mockResolvedValue(neighbor({ reconciliation_state: "confirmed" }));
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    await userEvent.type(await screen.findByLabelText("Local port ID"), "port-a");
    await userEvent.type(screen.getByLabelText("Remote port ID"), "port-b");
    await userEvent.click(screen.getByRole("button", { name: "Confirm with these ports" }));
    await waitFor(() => expect(api.confirmNeighbor).toHaveBeenCalled());
    expect(vi.mocked(api.confirmNeighbor).mock.calls[0][1]).toMatchObject({ local_port_id: "port-a", remote_port_id: "port-b" });
  });

  it("shows server refusals instead of pretending a conflicting confirmation worked", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    const { ApiError } = await vi.importActual<typeof import("@/lib/apiClient")>("@/lib/apiClient");
    vi.mocked(api.confirmNeighbor).mockRejectedValue(new ApiError(409, "Conflict", "A port is already part of a different authoritative link", null));
    renderWithProviders(<NeighborReviewPage />, { user: RECONCILER });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Confirm proposal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("different authoritative link");
  });

  it("gives read-only users evidence but no decision controls", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    renderWithProviders(<NeighborReviewPage />, { user: userWith("discovery:read") });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    expect(await screen.findByText(/Remote chassis/)).toBeInTheDocument();
    for (const name of ["Confirm proposal", "Reject", "Re-evaluate", "Revoke decision"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("records a physical cable only for a confirmed neighbor and only on explicit action", async () => {
    const confirmed = neighbor({ reconciliation_state: "confirmed", local_port_id: "lp", remote_port_id: "rp" });
    vi.mocked(api.listNeighbors).mockResolvedValue(page(confirmed));
    vi.mocked(api.createCableFromNeighbor).mockResolvedValue(cable({ label: "FROM-LLDP" }));
    renderWithProviders(<NeighborReviewPage />, { user: userWith("discovery:read", "discovery:reconcile", "cable:manage") });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    expect(api.createCableFromNeighbor).not.toHaveBeenCalled();
    await userEvent.type(await screen.findByLabelText("Cable label"), "FROM-LLDP");
    await userEvent.click(screen.getByRole("button", { name: "Record cable" }));
    await waitFor(() => expect(api.createCableFromNeighbor).toHaveBeenCalledWith("n1", expect.objectContaining({ label: "FROM-LLDP", status: "installed" })));
  });

  it("offers no cable form for a neighbor that is only proposed", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    renderWithProviders(<NeighborReviewPage />, { user: userWith("discovery:read", "discovery:reconcile", "cable:manage") });
    await userEvent.click(await screen.findByRole("button", { name: /Review neighbor/ }));
    await screen.findByRole("button", { name: "Confirm proposal" });
    expect(screen.queryByLabelText("Record physical cable")).toBeNull();
  });
});
