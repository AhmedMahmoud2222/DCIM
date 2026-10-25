import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CollectorsPage } from "@/features/integrations/CollectorsPage";
import { DiscoveryPage } from "@/features/integrations/DiscoveryPage";
import { IntegrationsPage } from "@/features/integrations/IntegrationsPage";
import * as api from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/integrations/api");

const never = () => new Promise<never>(() => undefined);
const integration = { id: "i1", name: "core-snmp", integration_type: "snmp", target_host: "10.0.0.1", enabled: true, last_poll_at: "2026-10-24T10:00:00Z", last_success_at: "2026-10-24T09:00:00Z", consecutive_failures: 2, has_credential: true, version: 1 };
const device = { id: "d1", external_identifier: "ext-001", status: "new", matched_managed_asset_id: null };
const collector = { id: "c1", name: "edge-01", collector_type: "edge", site_id: null, status: "active", version_string: "1", health: "offline", last_heartbeat_at: null, seconds_since_heartbeat: null };

const render = (ui: JSX.Element) => renderWithProviders(ui, { user: ADMINISTRATOR_TEST_USER });

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listIntegrations).mockResolvedValue([] as never);
  vi.mocked(api.listCollectors).mockResolvedValue([] as never);
  vi.mocked(api.listCollectorCapabilities).mockResolvedValue([] as never);
  vi.mocked(api.listDiscoveredDevices).mockResolvedValue([] as never);
  vi.mocked(api.listReconciliationDiffs).mockResolvedValue([] as never);
});

describe("Integrations list", () => {
  it("shows rows with its columns unchanged and no derived freshness or poll-age text", async () => {
    vi.mocked(api.listIntegrations).mockResolvedValue([integration] as never);
    render(<IntegrationsPage />);

    expect(await screen.findByText("core-snmp")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const headers = within(screen.getByRole("table", { name: "Integrations" })).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Name", "Type", "Target", "Enabled", "Last poll", "Last success", "Failures", "Credential", ""]);
    expect(screen.queryByTestId("measurement-freshness")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/Fresh\b|Late\b|ago\b/);
    expect(screen.getByRole("region", { name: "Integrations table" })).toHaveAttribute("tabindex", "0");
  });

  it("is loading, then empty only after a successful empty response", async () => {
    vi.mocked(api.listIntegrations).mockReturnValue(never() as never);
    const { unmount } = render(<IntegrationsPage />);
    expect(await screen.findByRole("status")).toHaveTextContent("Loading integrations…");
    expect(screen.queryByText("No integrations configured yet.")).not.toBeInTheDocument();
    unmount();

    vi.mocked(api.listIntegrations).mockResolvedValue([] as never);
    render(<IntegrationsPage />);
    expect(await screen.findByText("No integrations configured yet.")).toBeInTheDocument();
  });

  it("shows a failure as an alert with retry and a 403 as Access restricted, never as an empty list", async () => {
    vi.mocked(api.listIntegrations).mockRejectedValue(new Error("Upstream timed out"));
    const user = userEvent.setup();
    const { unmount } = render(<IntegrationsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load integrations.");
    expect(screen.queryByText("No integrations configured yet.")).not.toBeInTheDocument();
    vi.mocked(api.listIntegrations).mockResolvedValue([integration] as never);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("core-snmp")).toBeInTheDocument();
    unmount();

    vi.mocked(api.listIntegrations).mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    render(<IntegrationsPage />);
    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("integration:read")).toBeInTheDocument();
    expect(screen.queryByText("No integrations configured yet.")).not.toBeInTheDocument();
  });
});

describe("Discovery lists", () => {
  it("renders devices in a named table with the status chip text unchanged", async () => {
    vi.mocked(api.listDiscoveredDevices).mockResolvedValue([device] as never);
    render(<DiscoveryPage />);

    expect(await screen.findByText("ext-001")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Discovered devices" })).toBeInTheDocument();
    expect(screen.getByText("new")).toBeInTheDocument();
  });

  it("keeps the two lists independent: a failed device list does not blank the pending list, and neither reads as empty", async () => {
    vi.mocked(api.listDiscoveredDevices).mockRejectedValue(new Error("devices down"));
    render(<DiscoveryPage />);

    expect(await screen.findByText("Nothing pending reconciliation.")).toBeInTheDocument();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load discovered devices.");
    expect(screen.queryByText("No devices discovered yet.")).not.toBeInTheDocument();
  });

  it("does not say nothing is pending while the pending list is loading or has failed or is restricted", async () => {
    vi.mocked(api.listReconciliationDiffs).mockReturnValue(never() as never);
    const { unmount } = render(<DiscoveryPage />);
    expect(await screen.findByText("Loading pending reconciliations…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing pending reconciliation.")).not.toBeInTheDocument();
    unmount();

    vi.mocked(api.listReconciliationDiffs).mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    render(<DiscoveryPage />);
    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.queryByText("Nothing pending reconciliation.")).not.toBeInTheDocument();
  });

  it("says no devices only after a successful empty response", async () => {
    render(<DiscoveryPage />);
    expect(await screen.findByText("No devices discovered yet.")).toBeInTheDocument();
  });

  it("stacks its two columns on a narrow screen", () => {
    render(<DiscoveryPage />);
    const grid = document.querySelector(".grid.gap-6") as HTMLElement;
    expect(grid.className).toContain("lg:grid-cols-2");
    expect(grid.className).not.toMatch(/(^|\s)grid-cols-2/);
  });
});

describe("Collectors list", () => {
  it("shows collector connectivity as before, and does not derive freshness", async () => {
    vi.mocked(api.listCollectors).mockResolvedValue([collector] as never);
    render(<CollectorsPage />);

    expect(await screen.findByText("edge-01")).toBeInTheDocument();
    expect(screen.getAllByText("Offline").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("measurement-freshness")).not.toBeInTheDocument();
  });

  it("is loading, failed or restricted without ever claiming 'No collectors registered yet.'", async () => {
    vi.mocked(api.listCollectors).mockReturnValue(never() as never);
    const first = render(<CollectorsPage />);
    expect(await screen.findByText("Loading collectors…")).toBeInTheDocument();
    expect(screen.queryByText("No collectors registered yet.")).not.toBeInTheDocument();
    first.unmount();

    vi.mocked(api.listCollectors).mockRejectedValue(new Error("boom"));
    const second = render(<CollectorsPage />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not load collectors."));
    expect(screen.queryByText("No collectors registered yet.")).not.toBeInTheDocument();
    second.unmount();

    vi.mocked(api.listCollectors).mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    render(<CollectorsPage />);
    expect(await screen.findByText("collector:read")).toBeInTheDocument();
    expect(screen.queryByText("No collectors registered yet.")).not.toBeInTheDocument();
  });

  it("says none are registered only after a successful empty response", async () => {
    render(<CollectorsPage />);
    expect(await screen.findByText("No collectors registered yet.")).toBeInTheDocument();
  });
});
