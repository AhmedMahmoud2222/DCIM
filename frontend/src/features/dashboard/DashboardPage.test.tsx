import { screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { getOpenAlarms } from "@/features/telemetry/api";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/auth/useAuth", () => ({ useAuth: () => ({ user: { full_name: "Operator" } }) }));
vi.mock("@/features/power/api", () => ({
  getDashboardSummary: async () => null,
  getDashboardExceptions: async () => [],
}));
vi.mock("@/features/telemetry/api");

it("renders presented alarm values and units on the dashboard", async () => {
  vi.mocked(getOpenAlarms).mockImplementation(async (status) => status === "ACTIVE" ? [
    {
      id: "availability-alarm", rule_id: "rule-1", integration_id: "integration-1", managed_asset_id: null,
      subject_key: "availability", status: "ACTIVE", opened_at: "2026-01-01T00:00:00Z",
      acknowledged_at: null, cleared_at: null,
      last_value: 1, unit: "1", presentation_value: 100, presentation_unit: "%",
    },
    {
      id: "temperature-alarm", rule_id: "rule-2", integration_id: "integration-1", managed_asset_id: null,
      subject_key: "temperature", status: "ACTIVE", opened_at: "2026-01-01T00:00:00Z",
      acknowledged_at: null, cleared_at: null,
      last_value: 77, unit: "degF", presentation_value: 25, presentation_unit: "degC",
    },
  ] : []);
  renderWithProviders(<DashboardPage />);
  expect(await screen.findByText("availability · 100%")).toBeInTheDocument();
  expect(screen.getByText("temperature · 25 degC")).toBeInTheDocument();
  expect(screen.queryByText("temperature · 77")).not.toBeInTheDocument();
});
