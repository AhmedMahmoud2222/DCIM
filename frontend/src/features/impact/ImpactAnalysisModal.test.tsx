import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as impactApi from "@/features/impact/api";
import { ImpactAnalysisModal } from "@/features/impact/ImpactAnalysisModal";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ImpactSimulationResult } from "@/types";

vi.mock("@/features/impact/api");

function makeResult(overrides: Partial<ImpactSimulationResult> = {}): ImpactSimulationResult {
  return {
    target_type: "power_node", target_id: "node-1", directly_impacted: [], indirectly_impacted: [],
    lost_redundancy_paths: [], affected_services: [], ...overrides,
  };
}

describe("ImpactAnalysisModal", () => {
  it("renders nothing when no target is set", () => {
    renderWithProviders(<ImpactAnalysisModal target={null} onClose={vi.fn()} />);
    expect(screen.queryByTestId("impact-analysis-modal")).not.toBeInTheDocument();
  });

  it("runs the simulation on open and shows a no-impact message for an empty result", async () => {
    vi.mocked(impactApi.simulateImpact).mockResolvedValue(makeResult());

    renderWithProviders(
      <ImpactAnalysisModal target={{ type: "power_node", id: "node-1", label: "PDU-1 Outlet 3" }} onClose={vi.fn()} />,
    );

    expect(await screen.findByText("Simulate failure: PDU-1 Outlet 3")).toBeInTheDocument();
    expect(await screen.findByText("No modeled equipment is affected by this failure.")).toBeInTheDocument();
    expect(impactApi.simulateImpact).toHaveBeenCalledWith({ target_type: "power_node", target_id: "node-1" });
  });

  it("renders directly/indirectly impacted equipment and lost redundancy narratives", async () => {
    vi.mocked(impactApi.simulateImpact).mockResolvedValue(
      makeResult({
        directly_impacted: [
          {
            equipment_id: "eq-1", asset_tag: "SRV-1", hostname: "srv-1", service: "prod-api", hop: 1,
            impact_type: "power_loss", message: "srv-1 loses all modeled power feeds — full outage",
          },
        ],
        indirectly_impacted: [
          {
            equipment_id: "eq-2", asset_tag: "SRV-2", hostname: "srv-2", service: "prod-api", hop: 2,
            impact_type: "degraded_redundancy", message: "srv-2 loses PSU A; running single-corded on PSU B",
          },
        ],
        lost_redundancy_paths: ["srv-2 loses PSU A; running single-corded on PSU B"],
        affected_services: ["prod-api"],
      }),
    );

    renderWithProviders(
      <ImpactAnalysisModal target={{ type: "power_node", id: "node-1", label: "Outlet 3" }} onClose={vi.fn()} />,
    );

    expect(await screen.findByText("srv-1")).toBeInTheDocument();
    expect(screen.getByText("power loss")).toBeInTheDocument();
    expect(screen.getByText("srv-2")).toBeInTheDocument();
    expect(screen.getByText("degraded redundancy")).toBeInTheDocument();
    expect(screen.getByTestId("impact-lost-redundancy")).toHaveTextContent("srv-2 loses PSU A; running single-corded on PSU B");
    expect(screen.getByText("prod-api")).toBeInTheDocument();
    expect(screen.getAllByTestId("impact-item")).toHaveLength(2);
  });

  it("calls onClose when the close button is clicked", async () => {
    vi.mocked(impactApi.simulateImpact).mockResolvedValue(makeResult());
    const onClose = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(<ImpactAnalysisModal target={{ type: "network_port", id: "port-1", label: "eth0" }} onClose={onClose} />);

    await waitFor(() => expect(impactApi.simulateImpact).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("shows an error message when the simulation fails", async () => {
    const { ApiError } = await import("@/lib/apiClient");
    vi.mocked(impactApi.simulateImpact).mockRejectedValue(new ApiError(404, "Not Found", "PowerNode not found.", null));

    renderWithProviders(<ImpactAnalysisModal target={{ type: "power_node", id: "node-missing", label: "Missing" }} onClose={vi.fn()} />);

    expect(await screen.findByText("PowerNode not found.")).toBeInTheDocument();
  });
});
