import { screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { getOpenAlarms } from "@/features/telemetry/api";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/auth/useAuth", () => ({ useAuth: () => ({ user: { full_name: "Operator" } }) }));
vi.mock("@/features/power/api", () => ({
  getDashboardSummary: async () => null,
  getDashboardExceptions: async () => [
    { code: "RACK_OVER_CAPACITY", severity: "critical", message: "Rack R1 is over capacity" },
    { code: "FEED_IMBALANCE", severity: "warning", message: "Feeds are imbalanced" },
    { code: "NO_POWER_MODELED", severity: "info", message: "No power modeled" },
    { code: "NEW_BACKEND_CODE", severity: "fatal", message: "A severity this build does not know" },
  ],
}));
vi.mock("@/features/telemetry/api");

const alarm = (id: string, subject_key: string) => ({
  id, rule_id: "rule", integration_id: "integration", managed_asset_id: null, subject_key,
  status: "ACTIVE" as const, opened_at: "2026-01-01T00:00:00Z", acknowledged_at: null, cleared_at: null,
  last_value: 1, unit: "1", presentation_value: 1, presentation_unit: "%",
});

it("shows the backend severity as a label and icon beside the unchanged exception code", async () => {
  vi.mocked(getOpenAlarms).mockResolvedValue([]);
  renderWithProviders(<DashboardPage />);

  const expected: Array<[code: string, label: string]> = [
    ["RACK_OVER_CAPACITY", "Critical"],
    ["FEED_IMBALANCE", "Warning"],
    ["NO_POWER_MODELED", "Info"],
    ["NEW_BACKEND_CODE", "Unknown severity"],
  ];
  for (const [code, label] of expected) {
    const row = (await screen.findByText(code)).closest("div.flex")!;
    expect(within(row as HTMLElement).getByText(label)).toBeInTheDocument();
    expect(row.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  }
});

it("shows an ACTIVE alarm as a workflow state without implying any severity", async () => {
  vi.mocked(getOpenAlarms).mockImplementation(async (status) => (status === "ACTIVE" ? [alarm("a1", "availability")] : []));
  renderWithProviders(<DashboardPage />);

  const row = (await screen.findByText("availability · 1%")).closest("div.flex")!.parentElement!;
  const chip = within(row as HTMLElement).getByText("ACTIVE").parentElement!;
  expect(chip).toHaveAttribute("title", expect.stringMatching(/does not indicate severity/i));
  expect(chip.className).not.toMatch(/danger|red-/);
  expect(chip).not.toHaveTextContent(/critical|warning/i);
});
