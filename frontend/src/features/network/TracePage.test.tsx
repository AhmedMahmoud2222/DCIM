import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as equipmentApi from "@/features/equipment/api";
import * as api from "@/features/network/api";
import { userWith } from "@/features/network/fixtures";
import { TracePage } from "@/features/network/TracePage";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api", async (importActual) => ({ ...(await importActual<typeof api>()), traceFromPort: vi.fn() }));
vi.mock("@/features/equipment/api");

const start: api.TracePortView = { port_id: "pa", port_name: "Eth1/1", stable_key: "eth1-1", media_type: "copper", equipment_id: "ea", equipment_hostname: "edge-sw-1", equipment_asset_tag: "EQ-1" };
const remote: api.TracePortView = { ...start, port_id: "pb", port_name: "Eth1/24", equipment_id: "eb", equipment_hostname: "core-sw-1", equipment_asset_tag: "EQ-2" };

const result = (overrides: Partial<api.TraceResult> = {}): api.TraceResult => ({
  start,
  path: [{
    link: { kind: "cable", cable: { id: "c1", label: "PP1-A01", cable_type: "copper_utp", status: "installed", length_m: null, source: "discovery_confirmed", installed_at: null, removed_at: null } },
    hop: { restricted: false, remote },
  }],
  terminated: "end_of_path",
  evidence: { authoritative: false, neighbors: [], agreement: "no_evidence" },
  previous_cables: [],
  ...overrides,
});

const render = () => renderWithProviders(<TracePage />, { user: userWith("cable:read"), route: "/topology/trace?port=pa" });

describe("TracePage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  });

  it("renders device → port → cable → remote port → remote device", async () => {
    vi.mocked(api.traceFromPort).mockResolvedValue(result());
    render();
    expect(await screen.findByText("edge-sw-1")).toBeInTheDocument();
    expect(screen.getByTestId("trace-link")).toHaveTextContent("PP1-A01");
    expect(screen.getByTestId("trace-link")).toHaveTextContent("from confirmed discovery");
    expect(screen.getByText("core-sw-1")).toBeInTheDocument();
    expect(screen.getByText("Eth1/24")).toBeInTheDocument();
  });

  it("keeps discovery evidence in its own non-authoritative section", async () => {
    vi.mocked(api.traceFromPort).mockResolvedValue(result({
      evidence: {
        authoritative: false, agreement: "disagrees",
        neighbors: [{ neighbor_id: "n1", protocol: "lldp", reconciliation_state: "proposed", status: "active", authoritative: false, last_seen_at: "x", local_port_id: "pa", remote_port_id: "pz", remote_system_name: "other-sw", remote_chassis_ident: "c", remote_port_ident: "Eth9" }],
      },
    }));
    render();
    const evidence = await screen.findByRole("region", { name: "Discovery evidence" });
    expect(evidence).toHaveTextContent("not authoritative");
    expect(evidence).toHaveTextContent("Discovery disagrees with the recorded cable");
    expect(evidence).toHaveTextContent("LLDP saw other-sw / Eth9");
    expect(screen.getByRole("region", { name: "Recorded path" })).not.toHaveTextContent("other-sw");
  });

  it("represents an unlinked port, a restricted far end and a logical-only link honestly", async () => {
    vi.mocked(api.traceFromPort).mockResolvedValue(result({ path: [], terminated: "no_link", previous_cables: [{ id: "x", label: "OLD-1", cable_type: "copper_utp", status: "removed", length_m: null, source: "manual", installed_at: null, removed_at: "2026-09-01" }] }));
    const view = render();
    expect(await screen.findByText(/no recorded cable or connection/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Previous cables" })).toHaveTextContent("OLD-1");
    view.unmount();

    vi.mocked(api.traceFromPort).mockResolvedValue(result({ path: [{ link: { kind: "cable" }, hop: { restricted: true, remote: null } }], terminated: "restricted" }));
    const second = render();
    expect(await screen.findByText(/outside your access scope/)).toBeInTheDocument();
    second.unmount();

    vi.mocked(api.traceFromPort).mockResolvedValue(result({ path: [{ link: { kind: "port_connection", note: "logical connection without a recorded physical cable" }, hop: { restricted: false, remote } }] }));
    render();
    expect(await screen.findByText(/without a recorded physical cable/)).toBeInTheDocument();
  });

  it("reports a failed trace", async () => {
    const { ApiError } = await vi.importActual<typeof import("@/lib/apiClient")>("@/lib/apiClient");
    vi.mocked(api.traceFromPort).mockRejectedValue(new ApiError(404, "Not Found", "EquipmentPort pa not found.", null));
    render();
    expect(await screen.findByRole("alert")).toHaveTextContent("not found");
  });
});
