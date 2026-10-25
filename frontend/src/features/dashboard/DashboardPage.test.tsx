import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { listCollectors } from "@/features/integrations/api";
import { getDashboardExceptions, getDashboardSummary } from "@/features/power/api";
import { getOpenAlarms } from "@/features/telemetry/api";
import { ApiError } from "@/lib/apiClient";
import type { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Collector, DashboardExceptionItem, DashboardSummary } from "@/types";

vi.mock("@/features/power/api");
vi.mock("@/features/telemetry/api");
vi.mock("@/features/integrations/api");

const userWith = (...permission_codes: string[]): CurrentUser => ({
  id: "u1",
  email: "ops@example.test",
  full_name: "Sample Operator",
  role_names: ["Operator"],
  permission_codes,
});
const ALL = userWith("alarm:read", "dashboard:read", "collector:read");

const alarm = (id: string, subject_key: string, opened_at: string, extra: Record<string, unknown> = {}) => ({
  id, rule_id: "rule", integration_id: "integration", managed_asset_id: null, subject_key, status: "ACTIVE" as const,
  opened_at, acknowledged_at: null, cleared_at: null, last_value: 1, unit: "1", presentation_value: 1, presentation_unit: "%", ...extra,
});

const exception = (code: string, severity: string, message = `${code} message`): DashboardExceptionItem =>
  ({ code, severity, object_type: "rack", object_id: code, message }) as DashboardExceptionItem;

const collector = (name: string, health: string, seconds: number | null): Collector =>
  ({ id: `c-${name}`, name, collector_type: "edge", site_id: null, status: "active", version_string: "1", health, last_heartbeat_at: null, seconds_since_heartbeat: seconds }) as unknown as Collector;

const SUMMARY = {
  site_summary: { sites: 3, buildings: 5, floors: 12, rooms: 28, racks: 412, equipment: 2318, power_nodes: 164 },
  rack_summary: { total_racks: 412, occupied_racks: 338, available_racks: 74 },
  capacity_summary: { total_configured_kw: 4800, nodes_with_known_capacity: 151, nodes_with_unknown_capacity: 13 },
  power_summary: { overloaded_nodes: 2, near_capacity_nodes: 9, redundancy_degraded_equipment: 6 },
} as unknown as DashboardSummary;

const never = () => new Promise<never>(() => undefined);
const section = (id: string) => document.querySelector(`[data-section="${id}"]`) as HTMLElement;
const stateOf = (id: string) => section(id).getAttribute("data-state");

function mockAll({ active = [] as unknown[], acknowledged = [] as unknown[] } = {}) {
  vi.mocked(getOpenAlarms).mockImplementation((async (status?: string) => (status === "ACTIVE" ? active : acknowledged)) as typeof getOpenAlarms);
  vi.mocked(getDashboardExceptions).mockResolvedValue([]);
  vi.mocked(getDashboardSummary).mockResolvedValue(SUMMARY);
  vi.mocked(listCollectors).mockResolvedValue([]);
}

beforeEach(() => {
  vi.resetAllMocks();
  mockAll();
});

describe("Dashboard hierarchy", () => {
  it("has one page heading and lists the sections in priority order: alarms, exceptions, collectors, inventory", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Operations overview");
    const sections = Array.from(document.querySelectorAll("[data-section]")).map((el) => el.getAttribute("data-section"));
    expect(sections).toEqual(["alarms", "exceptions", "collectors", "inventory"]);
    const h2 = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(h2).toEqual(["Active alarms", "Infrastructure exceptions", "Collector connectivity", "Infrastructure inventory"]);
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
  });

  it("puts the two-column row (alarms wider than exceptions) on large screens and one column below", () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    const row = section("alarms").parentElement!;
    expect(row.className).toContain("grid");
    expect(row.className).toContain("lg:grid-cols-3");
    expect(section("alarms").className).toContain("lg:col-span-2");
    expect(section("exceptions").parentElement).toBe(row);
    // No fixed columns below the lg breakpoint, so phones get one column.
    expect(row.className).not.toMatch(/(^|\s)grid-cols-[2-9]/);
  });
});

describe("Active alarms", () => {
  it("renders presented alarm values and units, newest first, as workflow status without any severity", async () => {
    mockAll({
      active: [
        alarm("old", "availability", "2026-01-01T00:00:00Z", { presentation_value: 100 }),
        alarm("new", "temperature", "2026-03-01T00:00:00Z", { presentation_value: 25, presentation_unit: "degC", last_value: 77, unit: "degF" }),
      ],
    });
    renderWithProviders(<DashboardPage />, { user: ALL });

    const first = await screen.findByText("temperature · 25 degC");
    expect(screen.getByText("availability · 100%")).toBeInTheDocument();
    expect(screen.queryByText("temperature · 77")).not.toBeInTheDocument();
    const rows = within(section("alarms")).getAllByRole("listitem");
    expect(rows[0]).toContainElement(first);
    expect(rows[1]).toHaveTextContent("availability");

    const chip = within(rows[0]).getByText("ACTIVE").parentElement!;
    expect(chip).toHaveAttribute("title", expect.stringMatching(/does not indicate severity/i));
    expect(chip.className).not.toMatch(/danger|red-/);
    expect(section("alarms")).not.toHaveTextContent(/critical|warning|severity/i);
  });

  it("states the active and the acknowledged counts the API returned, and never a count it did not", async () => {
    mockAll({ active: [alarm("a", "x", "2026-01-01T00:00:00Z")], acknowledged: [alarm("b", "y", "2026-01-01T00:00:00Z"), alarm("c", "z", "2026-01-02T00:00:00Z")] });
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    expect(within(section("alarms")).getByText("1")).toBeInTheDocument();
    await waitFor(() => expect(section("alarms")).toHaveTextContent("2 acknowledged, awaiting clearance"));
  });

  it("lists the newest eight and says how many there are", async () => {
    const twelve = Array.from({ length: 12 }, (_, i) => alarm(`a${i}`, `subject-${i}`, `2026-01-${String(i + 1).padStart(2, "0")}T00:00:00Z`));
    mockAll({ active: twelve });
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    expect(within(section("alarms")).getAllByRole("listitem")).toHaveLength(8);
    expect(section("alarms")).toHaveTextContent("Showing the newest 8 of 12");
    expect(screen.getByText(/subject-11 ·/)).toBeInTheDocument();
    expect(screen.queryByText(/subject-0 ·/)).not.toBeInTheDocument();
  });

  it("says '100 or more' when the request limit is reached instead of presenting a total", async () => {
    mockAll({ active: Array.from({ length: 100 }, (_, i) => alarm(`a${i}`, `s${i}`, "2026-01-01T00:00:00Z")) });
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    expect(section("alarms")).toHaveTextContent("100 or more");
  });

  it("does not turn a failed acknowledged-count request into zero", async () => {
    vi.mocked(getOpenAlarms).mockImplementation((async (status?: string) => {
      if (status === "ACKNOWLEDGED") throw new Error("boom");
      return [alarm("a", "x", "2026-01-01T00:00:00Z")];
    }) as typeof getOpenAlarms);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(section("alarms")).toHaveTextContent("The acknowledged count could not be loaded."));
    expect(section("alarms")).not.toHaveTextContent(/0 acknowledged/);
  });

  it("links an alarm to its equipment", async () => {
    mockAll({ active: [alarm("a", "x", "2026-01-01T00:00:00Z", { managed_asset_id: "eq-9" })] });
    renderWithProviders(<DashboardPage />, { user: ALL });

    expect(await screen.findByRole("link", { name: "Equipment →" })).toHaveAttribute("href", "/equipment/eq-9");
  });
});

describe("Infrastructure exceptions", () => {
  beforeEach(() => {
    vi.mocked(getDashboardExceptions).mockResolvedValue([
      exception("NO_POWER_MODELED", "info"),
      exception("NEW_BACKEND_CODE", "fatal", "A severity this build does not know"),
      exception("FEED_IMBALANCE", "warning"),
      exception("RACK_OVER_CAPACITY", "critical", "Rack R1 is over capacity"),
    ]);
  });

  it("orders by the supplied severity, keeps every code, and shows a label and icon beside it", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    const rows = within(section("exceptions")).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector("span.font-mono")?.textContent)).toEqual([
      "RACK_OVER_CAPACITY",
      "FEED_IMBALANCE",
      "NO_POWER_MODELED",
      "NEW_BACKEND_CODE",
    ]);
    const labels = ["Critical", "Warning", "Info", "Unknown severity"];
    rows.forEach((row, i) => {
      expect(within(row).getByText(labels[i])).toBeInTheDocument();
      expect(row.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    });
    expect(screen.getByText("Rack R1 is over capacity")).toBeInTheDocument();
  });

  it("summarises how many of each supplied severity there are", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    const heading = screen.getByRole("heading", { level: 2, name: "Infrastructure exceptions" }).parentElement!;
    for (const label of ["Critical", "Warning", "Info", "Unknown severity"]) expect(within(heading).getByText(label)).toBeInTheDocument();
  });

  it("shows no severity for an alarm and no alarm status for an exception: the dimensions stay separate", async () => {
    mockAll({ active: [alarm("a", "x", "2026-01-01T00:00:00Z")] });
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("RACK_OVER_CAPACITY", "critical")]);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    expect(section("exceptions")).not.toHaveTextContent(/ACTIVE|ACKNOWLEDGED|CLEARED/);
    expect(section("alarms")).not.toHaveTextContent(/Critical/);
  });
});

describe("Collector connectivity", () => {
  beforeEach(() => {
    vi.mocked(listCollectors).mockResolvedValue([
      collector("zeta", "healthy", 10),
      collector("beta", "stale", 150),
      collector("alpha", "offline", 4000),
      collector("gamma", "offline", null),
      collector("delta", "degraded", 5),
    ]);
  });

  it("shows the backend classification counts and lists only collectors that need attention, offline first", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));
    const items = within(section("collectors")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringContaining("alpha"),
      expect.stringContaining("gamma"),
      expect.stringContaining("beta"),
      expect.stringContaining("delta"),
    ]);
    expect(items[0]).toHaveTextContent("last heartbeat 1 h ago");
    expect(items[1]).toHaveTextContent("no heartbeat recorded");
    expect(items[2]).toHaveTextContent("last heartbeat 2 min ago");
    expect(within(section("collectors")).queryByText("zeta")).not.toBeInTheDocument();
    expect(section("collectors")).toHaveTextContent("Other classification 1");
  });

  it("reuses the connectivity component and never presents it as measurement freshness", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));
    expect(within(section("collectors")).getAllByText("Collector connectivity:", { exact: false })[0]).toHaveClass("sr-only");
    expect(within(section("collectors")).getAllByText("Offline").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("measurement-freshness")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/Measurement freshness/);
    expect(section("collectors")).toHaveTextContent("It does not say how fresh any measurement is");
  });

  it("does not list a healthy fleet as a problem", async () => {
    vi.mocked(listCollectors).mockResolvedValue([collector("a", "healthy", 3), collector("b", "healthy", 4)]);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));
    expect(within(section("collectors")).queryAllByRole("listitem")).toHaveLength(0);
    expect(section("collectors")).toHaveTextContent("Every registered collector reports a Healthy heartbeat.");
  });
});

describe("Infrastructure inventory", () => {
  it("keeps the authorized inventory counts and their links", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    const inventory = within(section("inventory"));
    expect(inventory.getByText("2318")).toBeInTheDocument();
    expect(inventory.getByText("4800 kW")).toBeInTheDocument();
    expect(inventory.getByText("151 known / 13 unknown")).toBeInTheDocument();
    expect(inventory.getByRole("link", { name: /Racks/ })).toHaveAttribute("href", "/racks");
    expect(inventory.getByRole("link", { name: /Equipment/ })).toHaveAttribute("href", "/equipment");
    expect(inventory.getAllByRole("link", { name: /Overloaded Nodes|Near-Capacity Nodes|Redundancy Degraded|Power Nodes/ }).every((a) => a.getAttribute("href") === "/power")).toBe(true);
    expect(inventory.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Sites and racks", "Rack capacity", "Power capacity"]);
  });

  it("says 'unknown' for an unknown configured capacity instead of 0 kW", async () => {
    vi.mocked(getDashboardSummary).mockResolvedValue({ ...SUMMARY, capacity_summary: { ...SUMMARY.capacity_summary, total_configured_kw: null } } as DashboardSummary);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    expect(within(section("inventory")).getByText("unknown")).toBeInTheDocument();
    expect(section("inventory")).not.toHaveTextContent("0 kW");
  });
});

/** One row per independently loaded section: how to make its source fail, hang, or return nothing. */
const SOURCES = [
  { id: "alarms", what: "active alarms", permission: "alarm:read", user: userWith("dashboard:read", "collector:read"), empty: "No active alarms." },
  { id: "exceptions", what: "infrastructure exceptions", permission: "dashboard:read", user: userWith("alarm:read", "collector:read"), empty: "No capacity or topology exceptions detected." },
  { id: "collectors", what: "collector connectivity", permission: "collector:read", user: userWith("alarm:read", "dashboard:read"), empty: "No collectors are registered." },
  { id: "inventory", what: "the infrastructure inventory summary", permission: "dashboard:read", user: userWith("alarm:read", "collector:read"), empty: "The API returned no inventory summary." },
] as const;

function failSource(id: string, error: unknown) {
  if (id === "alarms") vi.mocked(getOpenAlarms).mockRejectedValue(error);
  if (id === "exceptions") vi.mocked(getDashboardExceptions).mockRejectedValue(error);
  if (id === "collectors") vi.mocked(listCollectors).mockRejectedValue(error);
  if (id === "inventory") vi.mocked(getDashboardSummary).mockRejectedValue(error);
}

describe.each(SOURCES)("$id section states", ({ id, what, permission, user, empty }) => {
  it("is loading while the request is in flight: a status, no records and no zero", async () => {
    if (id === "alarms") vi.mocked(getOpenAlarms).mockReturnValue(never());
    if (id === "exceptions") vi.mocked(getDashboardExceptions).mockReturnValue(never());
    if (id === "collectors") vi.mocked(listCollectors).mockReturnValue(never());
    if (id === "inventory") vi.mocked(getDashboardSummary).mockReturnValue(never());
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf(id)).toBe("loading"));
    const status = within(section(id)).getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent(`Loading ${what}…`);
    expect(section(id)).not.toHaveTextContent(empty);
  });

  it("is empty only after a successful response with no records, and says so", async () => {
    if (id === "inventory") vi.mocked(getDashboardSummary).mockResolvedValue(null as unknown as DashboardSummary);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf(id)).toBe("empty"));
    expect(screen.getByTestId(`${id}-empty`)).toHaveTextContent(empty);
    expect(within(section(id)).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an error, not an empty or zero state, when the request fails, and retries on request", async () => {
    failSource(id, new Error("Upstream timed out"));
    const userEv = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf(id)).toBe("error"));
    const alert = within(section(id)).getByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${what}.`);
    expect(alert).toHaveTextContent("Upstream timed out");
    expect(section(id)).not.toHaveTextContent(empty);
    expect(within(section(id)).queryByTestId(`${id}-empty`)).not.toBeInTheDocument();

    // Recover: the retry button, operated from the keyboard, requests again and the section fills in.
    if (id === "alarms") vi.mocked(getOpenAlarms).mockImplementation((async (s?: string) => (s === "ACTIVE" ? [alarm("a", "x", "2026-01-01T00:00:00Z")] : [])) as typeof getOpenAlarms);
    if (id === "exceptions") vi.mocked(getDashboardExceptions).mockResolvedValue([exception("E", "warning")]);
    if (id === "collectors") vi.mocked(listCollectors).mockResolvedValue([collector("c", "offline", 9)]);
    if (id === "inventory") vi.mocked(getDashboardSummary).mockResolvedValue(SUMMARY);
    const retry = within(section(id)).getByRole("button", { name: "Try again" });
    retry.focus();
    await userEv.keyboard("{Enter}");
    await waitFor(() => expect(stateOf(id)).toBe("ready"));
    expect(within(section(id)).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("is Access restricted, naming the permission, when the backend answers 403: never zero, empty or healthy", async () => {
    failSource(id, new ApiError(403, "Forbidden", "Missing permission", null));
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf(id)).toBe("restricted"));
    const panel = within(section(id));
    expect(panel.getByRole("heading", { level: 3, name: "Access restricted" })).toBeInTheDocument();
    expect(panel.getByText(permission)).toBeInTheDocument();
    expect(section(id)).not.toHaveTextContent(empty);
    expect(panel.queryByRole("alert")).not.toBeInTheDocument();
    expect(panel.queryByRole("listitem")).not.toBeInTheDocument();
    expect(section(id)).not.toHaveTextContent(/Healthy|\b0\b/);
  });

  it(`is Access restricted without any request when the user lacks ${permission}`, async () => {
    renderWithProviders(<DashboardPage />, { user });

    await waitFor(() => expect(stateOf(id)).toBe("restricted"));
    expect(within(section(id)).getByRole("heading", { level: 3, name: "Access restricted" })).toBeInTheDocument();
    expect(within(section(id)).getByText(permission)).toBeInTheDocument();
    expect(section(id)).not.toHaveTextContent(empty);
    const calls = {
      alarms: vi.mocked(getOpenAlarms),
      exceptions: vi.mocked(getDashboardExceptions),
      collectors: vi.mocked(listCollectors),
      inventory: vi.mocked(getDashboardSummary),
    }[id];
    expect(calls).not.toHaveBeenCalled();
  });
});

describe("Authorization", () => {
  it("lets each section stand on its own: one restricted source does not blank or zero the others", async () => {
    vi.mocked(getOpenAlarms).mockRejectedValue(new ApiError(403, "Forbidden", "Missing permission", null));
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("E1", "critical")]);
    vi.mocked(listCollectors).mockResolvedValue([collector("a", "offline", 5)]);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    expect(section("alarms")).not.toHaveTextContent("No active alarms.");
  });

  it("requests only what a dashboard:read-only user may read", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith("dashboard:read") });

    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    expect(getDashboardSummary).toHaveBeenCalled();
    expect(getDashboardExceptions).toHaveBeenCalled();
    expect(getOpenAlarms).not.toHaveBeenCalled();
    expect(listCollectors).not.toHaveBeenCalled();
    expect(stateOf("alarms")).toBe("restricted");
    expect(stateOf("collectors")).toBe("restricted");
  });

  it("treats a power:manage-style or look-alike code as no access", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith("alarm:manage", "dashboard:manage", "collector:manage") });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    for (const id of ["alarms", "exceptions", "collectors", "inventory"]) expect(stateOf(id)).toBe("restricted");
    expect(getOpenAlarms).not.toHaveBeenCalled();
    expect(getDashboardExceptions).not.toHaveBeenCalled();
    expect(listCollectors).not.toHaveBeenCalled();
    expect(getDashboardSummary).not.toHaveBeenCalled();
  });

  it("reads as loading, never restricted or empty, before the profile is known, and requests nothing", () => {
    renderWithProviders(<DashboardPage />, { user: null });

    for (const id of ["alarms", "exceptions", "collectors", "inventory"]) expect(stateOf(id)).toBe("loading");
    expect(getOpenAlarms).not.toHaveBeenCalled();
    expect(getDashboardExceptions).not.toHaveBeenCalled();
    expect(listCollectors).not.toHaveBeenCalled();
    expect(getDashboardSummary).not.toHaveBeenCalled();
  });

  it("gives each Access restricted panel its own heading id so the section labels do not collide", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith() });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    const ids = Array.from(document.querySelectorAll("[data-state=restricted] section h3")).map((h) => h.id);
    expect(ids).toHaveLength(4);
    expect(new Set(ids).size).toBe(4);
  });
});

describe("A paused initial request (offline, no cached data) is never an empty result", () => {
  afterEach(() => onlineManager.setOnline(true));

  it("reads as loading in every section, says it is waiting for the network, and shows no empty or healthy message", async () => {
    onlineManager.setOnline(false);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(document.querySelectorAll("[data-state=loading]")).toHaveLength(4));
    for (const id of ["alarms", "exceptions", "collectors", "inventory"]) {
      expect(stateOf(id)).toBe("loading");
      expect(within(section(id)).getByRole("status")).toHaveTextContent("Waiting for the network to load");
      expect(screen.queryByTestId(`${id}-empty`)).not.toBeInTheDocument();
    }
    expect(document.body).not.toHaveTextContent(/No active alarms|No capacity or topology|No collectors|Every registered collector/);
    expect(getOpenAlarms).not.toHaveBeenCalled();
  });

  it("fills in from the real response once the network returns", async () => {
    mockAll({ active: [alarm("a", "x", "2026-01-01T00:00:00Z")] });
    onlineManager.setOnline(false);
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("alarms")).toBe("loading"));

    onlineManager.setOnline(true);
    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    await waitFor(() => expect(stateOf("exceptions")).toBe("empty"));
  });
});

describe("Acknowledged alarms stay visible when the ACTIVE list is empty", () => {
  const ackAlarms = [alarm("b", "y", "2026-01-01T00:00:00Z", { status: "ACKNOWLEDGED" }), alarm("c", "z", "2026-01-02T00:00:00Z", { status: "ACKNOWLEDGED" })];

  it("shows 'No active alarms.' together with the acknowledged count and the Events link", async () => {
    mockAll({ active: [], acknowledged: ackAlarms });
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("empty"));
    expect(screen.getByTestId("alarms-empty")).toHaveTextContent("No active alarms.");
    const footer = within(section("alarms")).getByTestId("alarms-footer");
    await waitFor(() => expect(footer).toHaveTextContent("2 acknowledged, awaiting clearance."));
    expect(within(footer).getByRole("link", { name: "Open Events →" })).toHaveAttribute("href", "/events");
  });

  it("says the acknowledged count is loading, and that it could not be loaded, instead of showing zero", async () => {
    vi.mocked(getOpenAlarms).mockImplementation((async (status?: string) => (status === "ACTIVE" ? [] : never())) as typeof getOpenAlarms);
    const { unmount } = renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("alarms")).toBe("empty"));
    expect(within(section("alarms")).getByTestId("alarms-footer")).toHaveTextContent("Acknowledged alarms: loading…");
    unmount();

    vi.mocked(getOpenAlarms).mockImplementation((async (status?: string) => {
      if (status === "ACTIVE") return [];
      throw new Error("boom");
    }) as typeof getOpenAlarms);
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(within(section("alarms")).getByTestId("alarms-footer")).toHaveTextContent("The acknowledged count could not be loaded."));
    expect(within(section("alarms")).getByTestId("alarms-footer")).not.toHaveTextContent(/\b0 acknowledged/);
  });

  it("reports a genuinely empty acknowledged list as 0, because that response was successful", async () => {
    mockAll({ active: [], acknowledged: [] });
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(within(section("alarms")).getByTestId("alarms-footer")).toHaveTextContent("0 acknowledged, awaiting clearance."));
  });

  it("keeps the footer out of restricted and error states, and requests nothing for a user without alarm:read", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith("dashboard:read", "collector:read") });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    expect(within(section("alarms")).queryByTestId("alarms-footer")).not.toBeInTheDocument();
    expect(within(section("alarms")).queryByRole("link", { name: "Open Events →" })).not.toBeInTheDocument();
    expect(getOpenAlarms).not.toHaveBeenCalled();
  });

  it("does not show the footer when the ACTIVE request itself failed", async () => {
    vi.mocked(getOpenAlarms).mockRejectedValue(new Error("boom"));
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("error"));
    expect(within(section("alarms")).queryByTestId("alarms-footer")).not.toBeInTheDocument();
  });
});

describe("Exceptions beyond eight stay reachable", () => {
  const many = (critical: number, warning: number) => [
    ...Array.from({ length: warning }, (_, i) => exception(`WARN_${i}`, "warning")),
    ...Array.from({ length: critical }, (_, i) => exception(`CRIT_${i}`, "critical")),
  ];
  const codes = () => within(section("exceptions")).getAllByRole("listitem").map((li) => li.querySelector("span.font-mono")?.textContent);

  it("lists everything at eight or fewer, with no control", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue(many(3, 5));
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(codes()).toHaveLength(8);
    expect(within(section("exceptions")).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("exceptions-truncation")).not.toBeInTheDocument();
  });

  it("with eleven Critical records, shows eight, says three Critical are not shown, and reveals all on request", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue(many(11, 2));
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(codes()).toEqual(Array.from({ length: 8 }, (_, i) => `CRIT_${i}`));
    expect(screen.getByTestId("exceptions-truncation")).toHaveTextContent("Showing 8 of 13, most severe first. 3 more Critical are not shown.");

    const button = within(section("exceptions")).getByRole("button", { name: "Show all 13" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAttribute("aria-controls", within(section("exceptions")).getByRole("list").id);

    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveTextContent("Show fewer");
    const all = codes();
    expect(all).toHaveLength(13);
    // Still ordered critical first, then warning; the previously hidden Critical records are now listed.
    expect(all.slice(0, 11)).toEqual(Array.from({ length: 11 }, (_, i) => `CRIT_${i}`));
    expect(all.slice(11)).toEqual(["WARN_0", "WARN_1"]);
    expect(screen.getByTestId("exceptions-truncation")).toHaveTextContent("Showing all 13, most severe first.");
  });

  it("collapses again with Show fewer, and both directions work from the keyboard", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue(many(10, 0));
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));

    const button = within(section("exceptions")).getByRole("button", { name: "Show all 10" });
    button.focus();
    await user.keyboard("{Enter}");
    expect(codes()).toHaveLength(10);
    expect(button).toHaveFocus();
    await user.keyboard(" ");
    expect(codes()).toHaveLength(8);
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveTextContent("Show all 10");
  });

  it("offers nothing to a user without dashboard:read, and nothing after a 403", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith("alarm:read") });
    await waitFor(() => expect(stateOf("exceptions")).toBe("restricted"));
    expect(within(section("exceptions")).queryByRole("button")).not.toBeInTheDocument();
    expect(getDashboardExceptions).not.toHaveBeenCalled();
  });

  it("shows the one-line singular when exactly one Critical record is beyond the limit", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue(many(9, 0));
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(screen.getByTestId("exceptions-truncation")).toHaveTextContent("1 more Critical is not shown.");
  });
});

describe("Keyboard", () => {
  it("reaches every link and control in reading order, alarms first", async () => {
    mockAll({ active: [alarm("a", "x", "2026-01-01T00:00:00Z", { managed_asset_id: "eq-1" })] });
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("E1", "critical")]);
    vi.mocked(listCollectors).mockResolvedValue([collector("a", "offline", 5)]);
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));

    const order: string[] = [];
    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      order.push(document.activeElement?.textContent?.trim() ?? "");
    }
    expect(order.slice(0, 4)).toEqual(["Equipment →", "Open Events →", "Inspect →", "Open Collectors →"]);
  });
});
