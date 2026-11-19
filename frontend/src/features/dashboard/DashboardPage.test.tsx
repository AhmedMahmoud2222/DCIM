import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { listCollectors } from "@/features/integrations/api";
import { getDashboardExceptions, getDashboardSummary, getGeography } from "@/features/power/api";
import { AlarmOverview, getAlarmOverview } from "@/features/telemetry/api";
import { ApiError } from "@/lib/apiClient";
import type { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Collector, DashboardExceptionItem, DashboardSummary, Geography } from "@/types";

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
const ALL = userWith("alarm:read", "dashboard:read", "collector:read", "location:read");

const alarm = (id: string, extra: Record<string, unknown> = {}) => ({
  id, rule_id: "rule", integration_id: "integration", managed_asset_id: null, subject_key: `subject-${id}`, status: "ACTIVE", severity: "warning",
  opened_at: "2026-01-01T00:00:00Z", acknowledged_at: null, cleared_at: null, last_value: 1, unit: "1", presentation_value: 1, presentation_unit: "%", ...extra,
});

const overview = (items: unknown[] = [], extra: Partial<AlarmOverview> = {}): AlarmOverview =>
  ({
    scope: { include_acknowledged: false, severity: null },
    counts: { critical: 0, warning: items.length, info: 0, total: items.length },
    items,
    matching_total: items.length,
    limit: 7,
    truncated: false,
    as_of: "2026-11-18T10:00:00Z",
    ...extra,
  }) as AlarmOverview;

const exception = (code: string, severity: string, extra: Partial<DashboardExceptionItem> = {}): DashboardExceptionItem =>
  ({ code, severity, object_type: "power_node", object_id: code, message: `${code} message`, ...extra }) as DashboardExceptionItem;

const collector = (name: string, health: string, seconds: number | null): Collector =>
  ({ id: `c-${name}`, name, collector_type: "edge", site_id: null, status: "active", version_string: "1", health, last_heartbeat_at: null, seconds_since_heartbeat: seconds }) as unknown as Collector;

const SUMMARY = {
  site_summary: { sites: 3, buildings: 5, floors: 12, rooms: 28, racks: 412, equipment: 2318, power_nodes: 164 },
  rack_summary: { total_racks: 412, occupied_racks: 338, available_racks: 74 },
  capacity_summary: { total_configured_kw: 4800, nodes_with_known_capacity: 151, nodes_with_unknown_capacity: 13 },
  power_summary: { overloaded_nodes: 2, near_capacity_nodes: 9, redundancy_degraded_equipment: 6 },
} as unknown as DashboardSummary;

const EMPTY_GEOGRAPHY: Geography = {
  coordinates: "included",
  totals: { countries: 0, cities: 0, sites: 0, sites_with_coordinates: 0, sites_without_coordinates: 0 },
  countries: [],
  cities: [],
  sites: [],
  sites_truncated: false,
  as_of: "2026-11-18T10:00:00Z",
};
const GEOGRAPHY: Geography = {
  coordinates: "included",
  totals: { countries: 1, cities: 1, sites: 1, sites_with_coordinates: 1, sites_without_coordinates: 0 },
  countries: [{ id: "ae", name: "UAE", iso_code: "AE", city_count: 1, site_count: 1 }],
  cities: [{ id: "dxb", country_id: "ae", name: "Dubai", site_count: 1 }],
  sites: [{ id: "s1", code: "DXB1", name: "Dubai One", city_id: "dxb", latitude: 25.2, longitude: 55.3 }],
  sites_truncated: false,
  as_of: "2026-11-18T10:00:00Z",
};

const never = () => new Promise<never>(() => undefined);
const section = (id: string) => document.querySelector(`[data-section="${id}"]`) as HTMLElement;
const stateOf = (id: string) => section(id).getAttribute("data-state");
const IDS = ["alarms", "geography", "exceptions", "collectors", "inventory"] as const;

function mockAll() {
  vi.mocked(getAlarmOverview).mockResolvedValue(overview());
  vi.mocked(getGeography).mockResolvedValue(EMPTY_GEOGRAPHY);
  vi.mocked(getDashboardExceptions).mockResolvedValue([]);
  vi.mocked(getDashboardSummary).mockResolvedValue(SUMMARY);
  vi.mocked(listCollectors).mockResolvedValue([]);
}

beforeEach(() => {
  vi.resetAllMocks();
  mockAll();
});

describe("Dashboard hierarchy", () => {
  it("has one page heading and lists the sections in priority order", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Operations overview");
    expect(Array.from(document.querySelectorAll("[data-section]")).map((el) => el.getAttribute("data-section"))).toEqual([...IDS]);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "Active alarms",
      "Geographic coverage",
      "Events & exceptions",
      "Collector connectivity",
      "Inventory and capacity",
    ]);
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
  });

  it("puts alarms and geographic coverage side by side on large screens and in one column below", () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    const row = section("alarms").parentElement!;
    expect(row.className).toContain("lg:grid-cols-2");
    expect(section("geography").parentElement).toBe(row);
    expect(row.className).not.toMatch(/(^|\s)grid-cols-[2-9]/);
  });

  it("shows when data was last received and refreshes every readable section on request", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    await waitFor(() => expect(screen.getByTestId("dashboard-freshness")).toHaveTextContent(/Last data received/));

    const before = vi.mocked(getDashboardSummary).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(vi.mocked(getDashboardSummary).mock.calls.length).toBeGreaterThan(before));
    expect(vi.mocked(getAlarmOverview).mock.calls.length).toBeGreaterThan(1);
    expect(vi.mocked(getGeography).mock.calls.length).toBeGreaterThan(1);
  });

  it("does not refresh a source the user may not read", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: userWith("dashboard:read") });
    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(getAlarmOverview).not.toHaveBeenCalled();
    expect(getGeography).not.toHaveBeenCalled();
  });
});

describe("Stale data after a failed refresh", () => {
  it("keeps the earlier records on screen, says they may be out of date, and recovers on retry", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("RACK_OVER_CAPACITY", "critical")]);
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));

    vi.mocked(getDashboardExceptions).mockRejectedValue(new Error("Upstream timed out"));
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(stateOf("exceptions")).toBe("stale"));
    expect(within(section("exceptions")).getByText("RACK_OVER_CAPACITY")).toBeInTheDocument();
    expect(screen.getByTestId("exceptions-stale")).toHaveTextContent(/Could not refresh infrastructure exceptions\. Showing data loaded at .*; it may be out of date\./);
    expect(within(section("exceptions")).queryByRole("alert")).not.toBeInTheDocument();

    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("FEED_IMBALANCE", "warning")]);
    await user.click(within(screen.getByTestId("exceptions-stale")).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(within(section("exceptions")).getByText("FEED_IMBALANCE")).toBeInTheDocument();
  });

  it("turns a 403 on refresh into Access restricted and stops showing the earlier records", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("RACK_OVER_CAPACITY", "critical")]);
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));

    vi.mocked(getDashboardExceptions).mockRejectedValue(new ApiError(403, "Forbidden", "Missing permission", null));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(stateOf("exceptions")).toBe("restricted"));
    expect(within(section("exceptions")).queryByText("RACK_OVER_CAPACITY")).not.toBeInTheDocument();
  });
});

describe("Events & exceptions preview", () => {
  const many = [
    exception("NO_POWER_MODELED", "info", { object_type: "equipment", object_id: "eq-7" }),
    exception("NEW_BACKEND_CODE", "fatal"),
    exception("FEED_IMBALANCE", "warning"),
    ...Array.from({ length: 4 }, (_, i) => exception(`CRIT_${i}`, "critical")),
  ];
  const codes = () => within(section("exceptions")).getAllByRole("listitem").map((li) => li.querySelector("span.font-mono")?.textContent);

  it("lists the three most severe, counts every severity from the full list, and says how many more", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue(many);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(codes()).toEqual(["CRIT_0", "CRIT_1", "CRIT_2"]);
    expect(screen.getByTestId("exceptions-more")).toHaveTextContent("+4 more, including 1 Critical not shown here.");
    const heading = screen.getByRole("heading", { level: 2, name: "Events & exceptions" }).parentElement!;
    for (const label of ["Critical", "Warning", "Info", "Unknown severity"]) expect(within(heading).getByText(label)).toBeInTheDocument();
    expect(within(heading).getByText("4")).toBeInTheDocument();
    expect(within(section("exceptions")).getByRole("link", { name: "Open all in Events → Infrastructure" })).toHaveAttribute("href", "/events?view=infrastructure");
  });

  it("keeps code, severity and message on each row and the Inspect drill-down", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue([
      exception("RACK_OVER_CAPACITY", "critical", { message: "Rack R1 is over capacity" }),
      exception("REDUNDANCY_DEGRADED", "warning", { object_type: "equipment", object_id: "eq-9" }),
    ]);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    const rows = within(section("exceptions")).getAllByRole("listitem");
    expect(within(rows[0]).getByText("Critical")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Rack R1 is over capacity")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: "Inspect RACK_OVER_CAPACITY" })).toHaveAttribute("href", "/power");
    expect(within(rows[1]).getByRole("link", { name: "Inspect REDUNDANCY_DEGRADED" })).toHaveAttribute("href", "/equipment/eq-9");
    expect(screen.queryByTestId("exceptions-more")).not.toBeInTheDocument();
  });

  it("never shows an alarm workflow status on an exception", async () => {
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("RACK_OVER_CAPACITY", "critical")]);
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("exceptions")).toBe("ready"));
    expect(section("exceptions")).not.toHaveTextContent(/ACTIVE|ACKNOWLEDGED|CLEARED/);
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
    expect(within(section("collectors")).queryByText("zeta")).not.toBeInTheDocument();
    expect(section("collectors")).toHaveTextContent("Other classification 1");
  });

  it("never presents connectivity as measurement freshness", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("collectors")).toBe("ready"));
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

describe("Inventory and capacity", () => {
  it("keeps the authorized inventory counts and their links in a compact layout", async () => {
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("inventory")).toBe("ready"));
    const inventory = within(section("inventory"));
    expect(inventory.getByText("2318")).toBeInTheDocument();
    expect(inventory.getByText("4800 kW")).toBeInTheDocument();
    expect(inventory.getByText("151 known / 13 unknown")).toBeInTheDocument();
    expect(inventory.getByRole("link", { name: /Racks/ })).toHaveAttribute("href", "/racks");
    expect(inventory.getByRole("link", { name: /Equipment/ })).toHaveAttribute("href", "/equipment");
    expect(inventory.getAllByRole("link", { name: /Overloaded Nodes|Near-Capacity Nodes|Redundancy Degraded|Power Nodes/ }).every((a) => a.getAttribute("href") === "/power")).toBe(true);
    expect(inventory.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Inventory", "Rack capacity", "Power capacity"]);
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
  { id: "alarms", what: "active alarms", permission: "alarm:read", user: userWith("dashboard:read", "collector:read", "location:read"), empty: "No active alarms." },
  { id: "geography", what: "geographic coverage", permission: "location:read", user: userWith("alarm:read", "dashboard:read", "collector:read"), empty: "No authorized country, city or site is recorded." },
  { id: "exceptions", what: "infrastructure exceptions", permission: "dashboard:read", user: userWith("alarm:read", "collector:read", "location:read"), empty: "No capacity or topology exceptions detected." },
  { id: "collectors", what: "collector connectivity", permission: "collector:read", user: userWith("alarm:read", "dashboard:read", "location:read"), empty: "No collectors are registered." },
  { id: "inventory", what: "the infrastructure inventory summary", permission: "dashboard:read", user: userWith("alarm:read", "collector:read", "location:read"), empty: "The API returned no inventory summary." },
] as const;

/** The mocked API function behind each dashboard section. An explicit switch, not an object lookup by a runtime key. */
function sourceFn(id: (typeof IDS)[number]) {
  switch (id) {
    case "alarms": return vi.mocked(getAlarmOverview);
    case "geography": return vi.mocked(getGeography);
    case "exceptions": return vi.mocked(getDashboardExceptions);
    case "collectors": return vi.mocked(listCollectors);
    case "inventory": return vi.mocked(getDashboardSummary);
  }
}

function recover(id: string) {
  if (id === "alarms") vi.mocked(getAlarmOverview).mockResolvedValue(overview([alarm("a")]));
  if (id === "geography") vi.mocked(getGeography).mockResolvedValue(GEOGRAPHY);
  if (id === "exceptions") vi.mocked(getDashboardExceptions).mockResolvedValue([exception("E", "warning")]);
  if (id === "collectors") vi.mocked(listCollectors).mockResolvedValue([collector("c", "offline", 9)]);
  if (id === "inventory") vi.mocked(getDashboardSummary).mockResolvedValue(SUMMARY);
}

describe.each(SOURCES)("$id section states", ({ id, what, permission, user, empty }) => {
  it("is loading while the request is in flight: a status, no records and no zero", async () => {
    (sourceFn(id) as unknown as { mockReturnValue: (v: unknown) => void }).mockReturnValue(never());
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

  it("shows an error, not an empty or zero state, when the first request fails, and retries from the keyboard", async () => {
    (sourceFn(id) as unknown as { mockRejectedValue: (v: unknown) => void }).mockRejectedValue(new Error("Upstream timed out"));
    const userEv = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf(id)).toBe("error"));
    const alert = within(section(id)).getByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${what}.`);
    expect(alert).toHaveTextContent("Upstream timed out");
    expect(section(id)).not.toHaveTextContent(empty);

    recover(id);
    within(section(id)).getByRole("button", { name: "Try again" }).focus();
    await userEv.keyboard("{Enter}");
    await waitFor(() => expect(stateOf(id)).toBe("ready"));
    expect(within(section(id)).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("is Access restricted, naming the permission, when the backend answers 403: never zero, empty or healthy", async () => {
    (sourceFn(id) as unknown as { mockRejectedValue: (v: unknown) => void }).mockRejectedValue(new ApiError(403, "Forbidden", "Missing permission", null));
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
    expect(sourceFn(id)).not.toHaveBeenCalled();
  });
});

describe("Authorization", () => {
  it("lets each section stand on its own: one restricted source does not blank or zero the others", async () => {
    vi.mocked(getAlarmOverview).mockRejectedValue(new ApiError(403, "Forbidden", "Missing permission", null));
    vi.mocked(getDashboardExceptions).mockResolvedValue([exception("E1", "critical")]);
    vi.mocked(listCollectors).mockResolvedValue([collector("a", "offline", 5)]);
    vi.mocked(getGeography).mockResolvedValue(GEOGRAPHY);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    for (const id of ["geography", "exceptions", "collectors", "inventory"]) await waitFor(() => expect(stateOf(id)).toBe("ready"));
  });

  it("treats look-alike or manage codes as no access and requests nothing", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith("alarm:manage", "dashboard:manage", "collector:manage", "location:manage") });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    for (const id of IDS) expect(stateOf(id)).toBe("restricted");
    for (const id of IDS) expect(sourceFn(id)).not.toHaveBeenCalled();
  });

  it("reads as loading, never restricted or empty, before the profile is known, and requests nothing", () => {
    renderWithProviders(<DashboardPage />, { user: null });

    for (const id of IDS) expect(stateOf(id)).toBe("loading");
    for (const id of IDS) expect(sourceFn(id)).not.toHaveBeenCalled();
  });

  it("gives each Access restricted panel its own heading id and keeps only the sections as regions", async () => {
    renderWithProviders(<DashboardPage />, { user: userWith() });

    await waitFor(() => expect(stateOf("alarms")).toBe("restricted"));
    const ids = Array.from(document.querySelectorAll("[data-state=restricted] h3")).map((h) => h.id);
    expect(ids).toHaveLength(5);
    expect(new Set(ids).size).toBe(5);
    expect(document.querySelectorAll("[data-state=restricted] section")).toHaveLength(0);
    expect(screen.getAllByRole("region").map((r) => r.getAttribute("data-section"))).toEqual([...IDS]);
  });
});

describe("A paused initial request (offline, no cached data) is never an empty result", () => {
  afterEach(() => onlineManager.setOnline(true));

  it("reads as loading in every section and says it is waiting for the network", async () => {
    onlineManager.setOnline(false);
    renderWithProviders(<DashboardPage />, { user: ALL });

    await waitFor(() => expect(document.querySelectorAll("[data-state=loading]")).toHaveLength(5));
    for (const id of IDS) {
      expect(within(section(id)).getByRole("status")).toHaveTextContent("Waiting for the network to load");
      expect(screen.queryByTestId(`${id}-empty`)).not.toBeInTheDocument();
    }
    expect(getAlarmOverview).not.toHaveBeenCalled();
  });

  it("fills in from the real response once the network returns", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(overview([alarm("a")]));
    onlineManager.setOnline(false);
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("alarms")).toBe("loading"));

    onlineManager.setOnline(true);
    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));
    await waitFor(() => expect(stateOf("exceptions")).toBe("empty"));
  });
});

describe("Keyboard", () => {
  it("starts with Refresh, then the severity counters, before any other control", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(overview([alarm("a")]));
    const user = userEvent.setup();
    renderWithProviders(<DashboardPage />, { user: ALL });
    await waitFor(() => expect(stateOf("alarms")).toBe("ready"));

    const order: string[] = [];
    for (let i = 0; i < 4; i += 1) {
      await user.tab();
      order.push(document.activeElement?.textContent?.trim() ?? "");
    }
    expect(order[0]).toBe("Refresh");
    expect(order.slice(1, 4).map((t) => t.replace(/\d+$/, ""))).toEqual(["Critical", "Warning", "Info"]);
  });
});
