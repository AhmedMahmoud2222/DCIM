import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as accessApi from "@/features/access/api";
import * as api from "@/features/power/analyticsApi";
import { PowerAnalyticsPage } from "@/features/power/PowerAnalyticsPage";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/access/api");
vi.mock("@/features/power/analyticsApi");

const MANAGER = {
  id: "u1", email: "m@example.com", full_name: "Manager", role_names: ["DCIM Manager"],
  permission_codes: ["power:read", "power:manage"],
};
const VIEWER = { ...MANAGER, id: "u2", role_names: ["Viewer"], permission_codes: ["power:read"] };

const scope = {
  scope: "site", id: "s1", load_kw: 10, allocated_kw: 12, unserved_kw: 0, capacity_kw: 40, capacity_basis: "ups",
  headroom_kw: 30, utilization_pct: 25, quality: "measured" as const, equipment_count: 1,
};
const rollup = {
  site_id: "s1", generated_at: "2026-10-08T12:00:00Z", metric: "power_kw", unit: "kW", site: scope, rooms: [], racks: [],
  nodes: [
    {
      id: "n1", node_type: "ups", label: "UPS-A", rated_kw: 20, capacity_kw: 20, load_kw: 5, allocated_kw: 6, headroom_kw: 15,
      allocated_headroom_kw: 14, utilization_pct: 25, quality: "measured" as const, level: "ok" as const,
      protection_state: null, interrupted: false, overloaded: false, warnings: [],
    },
    {
      id: "n2", node_type: "protection_device", label: "BRK-A", rated_kw: 7, capacity_kw: 7, load_kw: 9, allocated_kw: 9,
      headroom_kw: -2, allocated_headroom_kw: -2, utilization_pct: 128.6, quality: "estimated" as const,
      level: "overload" as const, protection_state: "closed" as const, interrupted: false, overloaded: true, warnings: [],
    },
  ],
  equipment: [
    { id: "e1", label: "srv-1", scenario: "one_feed_failed", quality: "stale" as const, demand_kw: 10, served: true, live_inlets: 1, inlet_count: 2 },
  ],
  warnings: ["srv-1: redundancy lost (one_feed_failed)"],
};
const device = {
  id: "d1", housing_asset_id: "a1", site_id: "s1", label: "BRK-A", device_type: "breaker", rating_a: 32, voltage_v: 230,
  poles: 1, phase_config: "single", rated_kw: 7.36, state: "closed" as const, status: "in_service", state_changed_at: null,
  retired_at: null, upstream_node_ids: ["u"], downstream_node_ids: ["x", "y"], version: 3,
};
const forecast = (over: Partial<api.Forecast> = {}): api.Forecast => ({
  metric: "power_kw", unit: "kW", method: "linear_ols_v1", status: "sparse", no_forecast_reason: "needs at least 7 measured days",
  current_load_kw: 10, capacity_kw: 40, headroom_kw: 30, utilization_pct: 25, window_start: "2026-09-08T00:00:00Z",
  window_end: "2026-10-08T00:00:00Z", bucket_count: 10, measured_bucket_count: 10, day_count: 3, sample_count: 600,
  coverage_ratio: 1, slope_kw_per_day: null, r_squared: null, horizon_days: 365, exhaustion_date: null,
  days_to_exhaustion: null, confidence: "none", ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(accessApi.listAllSites).mockResolvedValue({
    items: [{ id: "s1", city_id: "c", code: "S1", name: "Site One", timezone: "UTC" }], total: 1, limit: 200, offset: 0,
  });
  vi.mocked(api.getRollup).mockResolvedValue(rollup);
});

const render = (user = MANAGER) => renderWithProviders(<PowerAnalyticsPage />, { user });

describe("PowerAnalyticsPage", () => {
  it("shows load, headroom, quality and warnings without colour-only meaning", async () => {
    render();
    expect(await screen.findByTestId("site-load")).toHaveTextContent("10.00 kW");
    expect(screen.getAllByText("Measured").length).toBeGreaterThan(0);
    const table = screen.getByRole("table", { name: "Power nodes" });
    const brk = within(table).getByRole("row", { name: /BRK-A/ });
    expect(brk).toHaveTextContent("-2.00 kW");
    expect(brk).toHaveTextContent("Overload, closed");
    expect(within(brk).getByText("Estimated")).toBeInTheDocument();
    expect(screen.getByText("srv-1: redundancy lost (one_feed_failed)")).toBeInTheDocument();
    const eq = within(screen.getByRole("table", { name: "Equipment redundancy" })).getByRole("row", { name: /srv-1/ });
    expect(eq).toHaveTextContent("one feed failed");
    expect(eq).toHaveTextContent("1 of 2");
    expect(within(eq).getByText("Stale")).toBeInTheDocument();
  });

  it("reports unserved demand as an alert and an API failure as an alert", async () => {
    vi.mocked(api.getRollup).mockResolvedValue({ ...rollup, site: { ...scope, unserved_kw: 4 } });
    const first = render();
    expect(await screen.findByRole("alert")).toHaveTextContent("4.00 kW of demand has no live power path");
    first.unmount();
    vi.mocked(api.getRollup).mockRejectedValue(new ApiError(403, "Forbidden", "Permission denied", null));
    render();
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  });

  it("changes breaker state with the current version and refreshes", async () => {
    vi.mocked(api.listProtectionDevices).mockResolvedValue({ items: [device], total: 1, limit: 200, offset: 0 });
    vi.mocked(api.setProtectionState).mockResolvedValue({ ...device, state: "tripped", version: 4 });
    render();
    await userEvent.click(await screen.findByRole("tab", { name: "Protection" }));
    const select = await screen.findByLabelText("State for BRK-A");
    await userEvent.selectOptions(select, "tripped");
    await waitFor(() => expect(api.setProtectionState).toHaveBeenCalledWith("d1", "tripped", 3));
  });

  it("shows protection state read-only without power:manage and surfaces a version conflict", async () => {
    vi.mocked(api.listProtectionDevices).mockResolvedValue({ items: [{ ...device, state: "unknown" }], total: 1, limit: 200, offset: 0 });
    const first = render(VIEWER);
    await userEvent.click(await screen.findByRole("tab", { name: "Protection" }));
    expect(await screen.findByText("Read only")).toBeInTheDocument();
    expect(screen.getByTestId("state-BRK-A")).toHaveTextContent("unknown (not reported)");
    expect(screen.queryByLabelText("State for BRK-A")).not.toBeInTheDocument();
    first.unmount();
    vi.mocked(api.setProtectionState).mockRejectedValue(new ApiError(409, "Conflict", "Resource has been modified", null));
    render();
    await userEvent.click(await screen.findByRole("tab", { name: "Protection" }));
    await userEvent.selectOptions(await screen.findByLabelText("State for BRK-A"), "open");
    expect(await screen.findByRole("alert")).toHaveTextContent("Resource has been modified");
  });

  it("explains why there is no forecast and lists the inputs", async () => {
    vi.mocked(api.getHistory).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(api.getForecast).mockResolvedValue(forecast());
    render();
    await userEvent.click(await screen.findByRole("tab", { name: "History and forecast" }));
    expect(await screen.findByTestId("forecast-status")).toHaveTextContent("Not enough data");
    expect(screen.getByText(/Reason: needs at least 7 measured days/)).toBeInTheDocument();
    expect(screen.getByText("linear_ols_v1")).toBeInTheDocument();
    expect(screen.getByText("none projected")).toBeInTheDocument();
    expect(screen.getByText("No snapshots recorded yet for this site.")).toBeInTheDocument();
  });

  it("shows an exhaustion date and a labelled trend when history exists", async () => {
    const points = Array.from({ length: 5 }, (_, i) => ({
      bucket_start: `2026-10-0${i + 1}T00:00:00Z`, bucket_end: `2026-10-0${i + 1}T01:00:00Z`, scope_type: "site", scope_id: "s1",
      unit: "kW", load_kw: 10 + i, load_basis: "measured", effective_capacity_kw: 40, headroom_kw: 30 - i, utilization_pct: 25,
      sample_count: 60, expected_samples: 60, coverage_ratio: 1, quality: "measured" as const,
    }));
    vi.mocked(api.getHistory).mockResolvedValue({ items: points, total: 5, limit: 200, offset: 0 });
    vi.mocked(api.getForecast).mockResolvedValue(
      forecast({ status: "good", no_forecast_reason: null, exhaustion_date: "2026-12-01T00:00:00Z", confidence: "high" }),
    );
    render();
    await userEvent.click(await screen.findByRole("tab", { name: "History and forecast" }));
    expect(await screen.findByTestId("forecast-status")).toHaveTextContent("Forecast available");
    expect(screen.getByText("2026-12-01")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Load trend from 10.00 to 14.00 kW over 5 hours/ })).toBeInTheDocument();
  });

  it("generates a report, shows progress states and downloads only a completed one", async () => {
    const job = (status: api.ReportJob["status"], extra: Partial<api.ReportJob> = {}): api.ReportJob => ({
      id: `j-${status}`, format: "csv", site_id: "s1", status, attempts: 1, row_count: status === "completed" ? 12 : null,
      failure_code: null, created_at: "2026-10-08T12:00:00Z", finished_at: null, ...extra,
    });
    vi.mocked(api.listReports).mockResolvedValue({
      items: [job("queued"), job("completed"), job("failed", { failure_code: "GENERATION_FAILED" })], total: 3, limit: 50, offset: 0,
    });
    vi.mocked(api.createReport).mockResolvedValue(job("queued"));
    vi.mocked(api.downloadReport).mockResolvedValue(new Blob(["a,b"], { type: "text/csv" }));
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    render();
    await userEvent.click(await screen.findByRole("tab", { name: "Reports" }));
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("Failed (GENERATION_FAILED)")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Download/ })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Generate report" }));
    await waitFor(() => expect(api.createReport).toHaveBeenCalledWith("s1", "csv"));
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(api.downloadReport).toHaveBeenCalledWith("j-completed", "csv"));
  });

  it("hides report generation from read-only users and shows a download error", async () => {
    vi.mocked(api.listReports).mockResolvedValue({
      items: [{ id: "j1", format: "json", site_id: "s1", status: "completed", attempts: 1, row_count: 3, failure_code: null, created_at: "2026-10-08T12:00:00Z", finished_at: null }],
      total: 1, limit: 50, offset: 0,
    });
    vi.mocked(api.downloadReport).mockRejectedValue(new ApiError(404, "Not Found", "Report not found", null));
    render(VIEWER);
    await userEvent.click(await screen.findByRole("tab", { name: "Reports" }));
    expect(screen.queryByRole("button", { name: "Generate report" })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Download JSON" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Report not found");
  });

  it("exposes the views as an accessible tablist", async () => {
    render();
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Capacity", "Protection", "History and forecast", "Reports"]);
    expect(screen.getByRole("tablist", { name: "Power analytics views" })).toBeInTheDocument();
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "tab-capacity");
  });
});
