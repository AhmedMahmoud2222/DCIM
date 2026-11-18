import { expect, test, type Page } from "@playwright/test";

// Issue #365 Control Room dashboard. Every API call is mocked, so this spec needs no backend.
const USER = {
  id: "1",
  email: "ops@example.com",
  full_name: "Ops",
  role_names: ["Operator"],
  permission_codes: ["alarm:read", "dashboard:read", "collector:read", "location:read"],
};

const alarm = (id: string, severity: string, status = "ACTIVE") => ({
  id,
  rule_id: "rule",
  integration_id: "integration",
  managed_asset_id: id === "a0" ? "eq-1" : null,
  subject_key: `rack-${id}.temperature`,
  status,
  severity,
  opened_at: "2026-11-18T09:00:00Z",
  acknowledged_at: status === "ACKNOWLEDGED" ? "2026-11-18T09:30:00Z" : null,
  cleared_at: null,
  last_value: 31,
  unit: "degC",
  presentation_value: 31,
  presentation_unit: "degC",
});

const ALARMS = [
  ...Array.from({ length: 4 }, (_, i) => alarm(`c${i}`, "critical")),
  ...Array.from({ length: 6 }, (_, i) => alarm(`w${i}`, "warning")),
  alarm("i0", "info"),
  alarm("k0", "warning", "ACKNOWLEDGED"),
];

function overview(url: URL) {
  const severity = url.searchParams.get("severity");
  const includeAck = url.searchParams.get("include_acknowledged") === "true";
  const inScope = ALARMS.filter((a) => includeAck || a.status === "ACTIVE");
  const matching = inScope.filter((a) => !severity || a.severity === severity);
  const count = (level: string) => inScope.filter((a) => a.severity === level).length;
  return {
    scope: { include_acknowledged: includeAck, severity },
    counts: { critical: count("critical"), warning: count("warning"), info: count("info"), total: inScope.length },
    items: matching.slice(0, 7),
    matching_total: matching.length,
    limit: 7,
    truncated: matching.length > 7,
    as_of: "2026-11-18T10:00:00Z",
  };
}

const GEOGRAPHY = {
  coordinates: "included",
  totals: { countries: 2, cities: 3, sites: 4, sites_with_coordinates: 3, sites_without_coordinates: 1 },
  countries: [
    { id: "ae", name: "UAE", iso_code: "AE", city_count: 2, site_count: 3 },
    { id: "uk", name: "United Kingdom", iso_code: "GB", city_count: 1, site_count: 1 },
  ],
  cities: [
    { id: "dxb", country_id: "ae", name: "Dubai", site_count: 2 },
    { id: "auh", country_id: "ae", name: "Abu Dhabi", site_count: 1 },
    { id: "lon", country_id: "uk", name: "London", site_count: 1 },
  ],
  sites: [
    { id: "s1", code: "DXB1", name: "Dubai One", city_id: "dxb", latitude: 25.2, longitude: 55.3 },
    { id: "s2", code: "DXB2", name: "Dubai Two", city_id: "dxb", latitude: null, longitude: null },
    { id: "s3", code: "AUH1", name: "Abu Dhabi One", city_id: "auh", latitude: 24.45, longitude: 54.38 },
    { id: "s4", code: "LON1", name: "London One", city_id: "lon", latitude: 51.5, longitude: -0.12 },
  ],
  sites_truncated: false,
  as_of: "2026-11-18T10:00:00Z",
};

const EXCEPTIONS = [
  { code: "NODE_OVERLOADED", severity: "critical", object_type: "power_node", object_id: "pn-1", message: "PDU A over capacity" },
  { code: "NODE_OVERLOADED", severity: "critical", object_type: "power_node", object_id: "pn-2", message: "PDU B over capacity" },
  { code: "REDUNDANCY_DEGRADED", severity: "warning", object_type: "equipment", object_id: "eq-9", message: "Single feed" },
  { code: "NODE_OVERLOADED", severity: "critical", object_type: "power_node", object_id: "pn-3", message: "PDU C over capacity" },
  { code: "POWER_PATH_MISSING", severity: "info", object_type: "equipment", object_id: "eq-3", message: "No power path" },
];

const SUMMARY = {
  site_summary: { sites: 4, buildings: 5, floors: 12, rooms: 28, racks: 412, equipment: 2318, power_nodes: 164 },
  rack_summary: { total_racks: 412, occupied_racks: 338, available_racks: 74 },
  capacity_summary: { total_configured_kw: 4800, nodes_with_known_capacity: 151, nodes_with_unknown_capacity: 13 },
  power_summary: { overloaded_nodes: 3, near_capacity_nodes: 9, redundancy_degraded_equipment: 6 },
};

async function mockApi(page: Page) {
  const overviewRequests: URL[] = [];
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: USER });
    if (path.endsWith("/auth/refresh")) return route.fulfill({ json: { access_token: "t" } });
    if (path.endsWith("/alarms/overview")) {
      overviewRequests.push(url);
      return route.fulfill({ json: overview(url) });
    }
    if (path.endsWith("/dashboard/geography")) return route.fulfill({ json: GEOGRAPHY });
    if (path.endsWith("/dashboard/exceptions")) return route.fulfill({ json: EXCEPTIONS });
    if (path.endsWith("/dashboard/summary")) return route.fulfill({ json: SUMMARY });
    if (path.endsWith("/alarms/history")) return route.fulfill({ json: { items: [], next_cursor: null } });
    return route.fulfill({ json: [] });
  });
  return overviewRequests;
}

const alarmsPanel = (page: Page) => page.locator('[data-section="alarms"]');
/** Counter buttons are named "<Severity> <count>". Literal patterns, not a RegExp built from a string. */
function counter(page: Page, name: "Critical" | "Warning" | "Info") {
  const pattern = name === "Critical" ? /^Critical\s*\d+$/ : name === "Warning" ? /^Warning\s*\d+$/ : /^Info\s*\d+$/;
  return alarmsPanel(page).getByRole("button", { name: pattern });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "horizontal overflow").toBeLessThanOrEqual(0);
}

test.describe("Control Room dashboard", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("severity counters and selector are one control and show server counts", async ({ page }) => {
    const requests = await mockApi(page);
    await page.goto("/");
    await expect(alarmsPanel(page)).toHaveAttribute("data-state", "ready");
    await expect(counter(page, "Critical")).toContainText("4");
    await expect(counter(page, "Warning")).toContainText("6");
    await expect(alarmsPanel(page).getByRole("listitem")).toHaveCount(7);
    await expect(alarmsPanel(page).getByTestId("alarms-truncation")).toHaveText("Showing latest 7 of 11.");

    await counter(page, "Critical").click();
    await expect(counter(page, "Critical")).toHaveAttribute("aria-pressed", "true");
    await expect(alarmsPanel(page).getByRole("radio", { name: "Critical" })).toBeChecked();
    await expect(alarmsPanel(page).getByRole("listitem")).toHaveCount(4);
    expect(requests.at(-1)?.searchParams.get("severity")).toBe("critical");

    await counter(page, "Critical").click();
    await expect(alarmsPanel(page).getByRole("radio", { name: "All" })).toBeChecked();
    await expect(alarmsPanel(page).getByRole("listitem")).toHaveCount(7);
  });

  test("the acknowledged switch is off by default and changes the request on its own", async ({ page }) => {
    const requests = await mockApi(page);
    await page.goto("/");
    const toggle = alarmsPanel(page).getByRole("switch", { name: "Include acknowledged" });
    await expect(toggle).not.toBeChecked();
    expect(requests[0].searchParams.get("include_acknowledged")).toBe("false");

    // The radio input is visually hidden inside its segmented label; a pointer user clicks the label.
    await alarmsPanel(page).locator("fieldset label", { hasText: /^Warning$/ }).click();
    await expect(alarmsPanel(page).getByRole("radio", { name: "Warning" })).toBeChecked();
    await toggle.check();
    await expect(alarmsPanel(page).getByTestId("alarms-scope")).toContainText("12 active or acknowledged in total");
    const last = requests.at(-1)!;
    expect(last.searchParams.get("include_acknowledged")).toBe("true");
    expect(last.searchParams.get("severity")).toBe("warning");
    await expect(alarmsPanel(page).getByText("ACKNOWLEDGED", { exact: true })).toBeVisible();
  });

  test("the exceptions preview drills into Events → Infrastructure", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    const preview = page.locator('[data-section="exceptions"]');
    await expect(preview.getByRole("listitem")).toHaveCount(3);
    await expect(preview.getByTestId("exceptions-more")).toHaveText("+2 more.");
    await preview.getByRole("link", { name: "Open all in Events → Infrastructure" }).click();

    await expect(page).toHaveURL(/\/events\?view=infrastructure$/);
    await expect(page.getByRole("tab", { name: "Infrastructure" })).toHaveAttribute("aria-selected", "true");
    const table = page.getByRole("table", { name: "Infrastructure exceptions" });
    await expect(table.getByRole("row")).toHaveCount(6);
    await expect(table.getByRole("link", { name: /Inspect REDUNDANCY_DEGRADED/ })).toHaveAttribute("href", "/equipment/eq-9");
    await expect(page.getByRole("button", { name: "Export current results CSV" })).toBeDisabled();

    await page.getByRole("tab", { name: "History" }).click();
    await expect(page).toHaveURL(/\/events\?view=history$/);
  });

  test("the alarm row links to Events and to its equipment", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    await alarmsPanel(page).getByRole("link", { name: "Open rack-c0.temperature in Events" }).click();
    await expect(page).toHaveURL(/\/events$/);
    await expect(page.getByRole("tab", { name: /Active/ })).toHaveAttribute("aria-selected", "true");
  });

  test("works from the keyboard: counters, map pan and zoom, cluster activation", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    await expect(alarmsPanel(page)).toHaveAttribute("data-state", "ready");

    await counter(page, "Warning").focus();
    await page.keyboard.press("Enter");
    await expect(counter(page, "Warning")).toHaveAttribute("aria-pressed", "true");

    const svg = page.locator('[data-section="geography"] svg');
    await page.getByRole("group", { name: /Site map/ }).focus();
    await page.keyboard.press("+");
    await expect(svg).toHaveAttribute("viewBox", "90 45 180 90");
    await page.keyboard.press("ArrowRight");
    await expect(svg).not.toHaveAttribute("viewBox", "90 45 180 90");
    await page.getByRole("button", { name: "Reset view" }).click();

    await page.getByRole("button", { name: /Cluster of 2 sites/ }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("map-status")).toContainText("Zoom 2×");
  });

  test("the map has an always-rendered list with a sites-without-coordinates group", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    const geography = page.locator('[data-section="geography"]');
    await expect(geography).toHaveAttribute("data-state", "ready");
    const group = page.getByRole("region", { name: /Sites without coordinates/ });
    await expect(group.getByRole("link", { name: "Dubai Two (DXB2)" })).toHaveAttribute("href", "/sites/s2");

    const list = page.getByTestId("geography-list");
    await list.getByRole("button", { name: "UAE" }).click();
    await list.getByRole("button", { name: "Dubai", exact: true }).click();
    await list.getByRole("button", { name: "Dubai One (DXB1)" }).click();
    await expect(page.getByTestId("selected-site").getByRole("link", { name: "Open site DXB1 →" })).toHaveAttribute("href", "/sites/s1");
  });
});

for (const width of [1440, 820, 390]) {
  test.describe(`at ${width}px wide`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("the dashboard and the Infrastructure tab do not scroll sideways", async ({ page }) => {
      await mockApi(page);
      await page.goto("/");
      await expect(page.locator('[data-section="geography"]')).toHaveAttribute("data-state", "ready");
      await expect(page.locator('[data-section="inventory"]')).toHaveAttribute("data-state", "ready");
      await expectNoHorizontalOverflow(page);
      const alarms = (await alarmsPanel(page).boundingBox())!;
      const geography = (await page.locator('[data-section="geography"]').boundingBox())!;
      if (width >= 1024) expect(Math.abs(alarms.y - geography.y), "side by side").toBeLessThan(2);
      else expect(geography.y, "stacked").toBeGreaterThan(alarms.y + alarms.height - 1);

      await page.goto("/events?view=infrastructure");
      await expect(page.getByRole("table", { name: "Infrastructure exceptions" })).toBeVisible();
      await expectNoHorizontalOverflow(page);
    });
  });
}
