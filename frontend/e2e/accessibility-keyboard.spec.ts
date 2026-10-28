import { expect, test, type Page } from "@playwright/test";

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

test("login labels and skip link work with a keyboard", async ({ page }) => {
  await page.goto("/login");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("textbox", { name: "Email" })).toBeFocused();
  await page.getByRole("textbox", { name: "Email" }).fill(ADMIN_EMAIL);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  const skip = page.getByRole("link", { name: "Skip to main content" });
  await page.keyboard.press("Tab");
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // Measure the rendered Administration group label against its opaque sidebar background.
  const adminLabel = page.getByRole("button", { name: "Administration", exact: true });
  await expect(adminLabel).toBeVisible();
  const contrast = await adminLabel.evaluate((element) => {
    // Tailwind 4 colors compute as oklch(), not rgb(), so resolve any CSS color to sRGB through a canvas pixel.
    const channels = (color: string) => {
      const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
      context.fillStyle = "#000";
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
    };
    const luminance = (color: string) => {
      const [r, g, b] = channels(color).map((value) => {
        const srgb = value / 255;
        return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const foreground = luminance(getComputedStyle(element).color);
    const background = luminance(getComputedStyle(element.closest("aside")!).backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);

  // At 400% zoom on a 1280px-wide viewport, the CSS viewport is approximately 320px.
  await page.setViewportSize({ width: 320, height: 640 });
  const mainBounds = await page.locator("#main-content").boundingBox();
  expect(mainBounds).not.toBeNull();
  expect(mainBounds!.x).toBe(0);
  expect(mainBounds!.width).toBeGreaterThanOrEqual(320);
  expect(mainBounds!.x + mainBounds!.width).toBeLessThanOrEqual(320);
});

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "Email" }).fill(ADMIN_EMAIL);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}

/** Reads the focus ring of the focused element and whether its colour is the named CSS token. */
async function focusRing(page: Page, token: string) {
  return page.evaluate((tokenName) => {
    const element = document.activeElement as HTMLElement;
    const style = getComputedStyle(element);
    // Tailwind 4 colours compute as oklch(), so compare through a canvas pixel in sRGB.
    const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    const toRgb = (color: string) => {
      canvas.fillStyle = "#000";
      canvas.fillStyle = color;
      canvas.fillRect(0, 0, 1, 1);
      return Array.from(canvas.getImageData(0, 0, 1, 1).data.slice(0, 3)).join(",");
    };
    const tokenValue = getComputedStyle(document.documentElement).getPropertyValue(tokenName).trim();
    return {
      inSidebar: Boolean(element.closest("#app-sidebar")),
      outlineStyle: style.outlineStyle,
      outlineWidth: parseFloat(style.outlineWidth),
      matchesToken: toRgb(style.outlineColor) === toRgb(tokenValue),
    };
  }, token);
}

test("shared focus ring is visible on menu links and on form inputs that remove the browser outline", async ({ page }) => {
  await signIn(page);

  // Skip link first, then the first menu links in order.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Tab");
  for (let step = 0; step < 4; step += 1) {
    const ring = await focusRing(page, "--color-focus-on-sidebar");
    expect(ring.inSidebar).toBe(true);
    expect(ring.outlineStyle).toBe("solid");
    expect(ring.outlineWidth).toBeGreaterThanOrEqual(2);
    expect(ring.matchesToken).toBe(true);
    await page.keyboard.press("Tab");
  }

  // The New Equipment inputs use `focus:outline-hidden`; the shared rule must still draw a ring.
  await page.getByRole("link", { name: "Equipment", exact: true }).click();
  await page.getByRole("button", { name: "New Equipment" }).click();
  await page.getByPlaceholder("Asset tag (e.g. SRV-0142)").focus();
  const inputRing = await focusRing(page, "--color-focus");
  expect(inputRing.inSidebar).toBe(false);
  expect(inputRing.outlineStyle).toBe("solid");
  expect(inputRing.outlineWidth).toBeGreaterThanOrEqual(2);
  expect(inputRing.matchesToken).toBe(true);
});

test("design tokens can be replaced by a theme attribute without touching components", async ({ page }) => {
  await page.goto("/login");
  await page.addStyleTag({ content: ':root[data-theme="probe"] { --color-focus: #ff0000; }' });
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "probe"));
  await page.getByRole("textbox", { name: "Email" }).focus();
  const outlineColor = await page.evaluate(() => getComputedStyle(document.activeElement as HTMLElement).outlineColor);
  expect(outlineColor).toBe("rgb(255, 0, 0)");
});

test.describe("mobile navigation", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  const insideSidebar = (page: Page) => page.evaluate(() => Boolean(document.activeElement?.closest("#app-sidebar")));
  const noSidewaysScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

  test("menu drawer is keyboard accessible, trapped while open, and has 44px touch targets", async ({ page }) => {
    await signIn(page);

    // Content comes first; the menu is behind a button, not stacked above the page.
    const menuButton = page.getByRole("button", { name: "Open main menu" });
    await expect(menuButton).toBeVisible();
    await expect(menuButton).toHaveAttribute("aria-expanded", "false");
    const buttonBox = (await menuButton.boundingBox())!;
    expect(buttonBox.width).toBeGreaterThanOrEqual(44);
    expect(buttonBox.height).toBeGreaterThanOrEqual(44);
    const signOutBox = (await page.getByRole("button", { name: "Sign out" }).boundingBox())!;
    expect(signOutBox.height).toBeGreaterThanOrEqual(44);
    const mainBox = (await page.locator("#main-content").boundingBox())!;
    expect(mainBox.y).toBeLessThan(200);
    expect(await noSidewaysScroll(page)).toBe(true);

    // Closed: inert, and the keyboard never lands inside the hidden menu.
    await expect(page.locator("#app-sidebar")).toHaveAttribute("inert", "");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    for (let step = 0; step < 6; step += 1) {
      await page.keyboard.press("Tab");
      expect(await insideSidebar(page)).toBe(false);
    }

    // Open: a dialog, focus on Close, links at least 44px tall.
    await menuButton.click();
    const dialog = page.getByRole("dialog", { name: "Main menu" });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("button", { name: "Close menu" })).toBeFocused();
    const linkBox = (await dialog.getByRole("link", { name: "Racks", exact: true }).boundingBox())!;
    expect(linkBox.height).toBeGreaterThanOrEqual(44);
    expect(await noSidewaysScroll(page)).toBe(true);

    // Tab and Shift+Tab never leave the dialog, however many times they are pressed.
    for (let step = 0; step < 30; step += 1) {
      await page.keyboard.press("Tab");
      expect(await insideSidebar(page)).toBe(true);
    }
    for (let step = 0; step < 6; step += 1) {
      await page.keyboard.press("Shift+Tab");
      expect(await insideSidebar(page)).toBe(true);
    }

    // Escape closes it, hands focus back to the menu button, and makes it inert again.
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(menuButton).toBeFocused();
    await expect(page.locator("#app-sidebar")).toHaveAttribute("inert", "");

    // Choosing a link navigates and closes the drawer.
    await menuButton.click();
    await page.getByRole("dialog", { name: "Main menu" }).getByRole("link", { name: "Equipment", exact: true }).click();
    await expect(page).toHaveURL("/equipment");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(await noSidewaysScroll(page)).toBe(true);
  });

  test("reduced motion removes the drawer slide", async ({ page }) => {
    await signIn(page);
    const duration = () => page.locator("#app-sidebar").evaluate((element) => parseFloat(getComputedStyle(element).transitionDuration));

    await page.emulateMedia({ reducedMotion: "no-preference" });
    expect(await duration()).toBeCloseTo(0.15, 2);

    await page.emulateMedia({ reducedMotion: "reduce" });
    expect(await duration()).toBeLessThan(0.001);
  });
});

test("navigation groups are keyboard disclosure buttons that keep the current page's group open", async ({ page }) => {
  await signIn(page);
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const group of ["Monitoring", "Infrastructure", "Network", "Data Sources", "Administration"]) {
    await expect(nav.getByRole("button", { name: group, exact: true })).toHaveAttribute("aria-expanded", "true");
  }

  const monitoring = nav.getByRole("button", { name: "Monitoring", exact: true });
  await monitoring.focus();
  await page.keyboard.press("Enter");
  await expect(monitoring).toHaveAttribute("aria-expanded", "false");
  await expect(nav.getByRole("link", { name: "Events", exact: true })).toHaveCount(0);

  // Arriving on a page inside the collapsed group (a client-side navigation, so the shell keeps its
  // state) opens it again and marks the page.
  await page.evaluate(() => {
    window.history.pushState({}, "", "/events");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(monitoring).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "Events", exact: true })).toHaveAttribute("aria-current", "page");
});

test("dashboard sections come in priority order, settle out of loading, and fit desktop and phone widths", async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole("heading", { level: 1, name: "Operations overview" })).toBeVisible();

  const ids = ["alarms", "exceptions", "collectors", "inventory"];
  await expect(page.locator("[data-section]")).toHaveCount(4);
  expect(await page.locator("[data-section]").evaluateAll((els) => els.map((el) => el.getAttribute("data-section")))).toEqual(ids);

  // Every section ends in a deliberate state. An unfinished load or a failed request is not acceptable here,
  // and "restricted" means the signed-in user lacks that permission, not that the data is zero.
  for (const id of ids) {
    await expect(page.locator(`[data-section="${id}"]`)).toHaveAttribute("data-state", /^(ready|empty|restricted)$/, { timeout: 15_000 });
  }

  const noSideways = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

  // Desktop: alarms and exceptions share a row, alarms first and wider.
  await page.setViewportSize({ width: 1280, height: 800 });
  const alarms = (await page.locator('[data-section="alarms"]').boundingBox())!;
  const exceptions = (await page.locator('[data-section="exceptions"]').boundingBox())!;
  expect(alarms.x).toBeLessThan(exceptions.x);
  expect(Math.abs(alarms.y - exceptions.y)).toBeLessThan(2);
  expect(alarms.width).toBeGreaterThan(exceptions.width);
  expect(await noSideways()).toBe(true);

  // Phone: one column in the same order, and the page itself never scrolls sideways.
  await page.setViewportSize({ width: 390, height: 844 });
  const phoneAlarms = (await page.locator('[data-section="alarms"]').boundingBox())!;
  const phoneExceptions = (await page.locator('[data-section="exceptions"]').boundingBox())!;
  expect(phoneExceptions.y).toBeGreaterThanOrEqual(phoneAlarms.y + phoneAlarms.height - 1);
  expect(await noSideways()).toBe(true);
});

test("each page names itself in the document title", async ({ page }) => {
  await signIn(page);
  await expect(page).toHaveTitle("Dashboard · DCIM Platform");
  await page.evaluate(() => {
    window.history.pushState({}, "", "/events");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page).toHaveTitle("Events · DCIM Platform");
});

// ---------------------------------------------------------------------------------------------------
// Reflow with populated content (WCAG 1.4.4 resize text, 1.4.10 reflow)
//
// The measurements below only mean something once the page holds its real, populated content, so the
// API responses for these routes are served from fixtures by browser-level routes (the sign-in itself
// still goes to the real backend), each delayed so the loading layout exists first. A route is measured
// only after its own content has appeared; "networkidle" is not used because it resolves immediately
// after a client-side (pushState) navigation inside a document that was already idle.
// ---------------------------------------------------------------------------------------------------

const LONG = "datacenter-east-region-one-building-seven-hall-b";
const FIXTURE_ME = {
  id: "fixture-user",
  email: "ops.lead@example.test",
  full_name: "Sample Operator With A Fairly Long Display Name",
  role_names: ["Administrator"],
  permission_codes: ["dashboard:read", "alarm:read", "collector:read", "rack:read", "equipment:read", "cable:read", "discovery:read", "integration:read", "location:read", "power:read"],
};
const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
const FIXTURE_EQUIPMENT = Array.from({ length: 12 }, (_, index) => ({
  id: `eq-${index}`,
  asset_tag: `SRV-${String(1000 + index)}`,
  lifecycle_status: ["active", "decommissioned", "maintenance", "installed"][index % 4],
  hostname: `app-cache-batch-primary-${String(index + 1).padStart(2, "0")}.${LONG}.example.test`,
  version: 1,
  placement:
    index % 5 === 4
      ? null
      : { placement_type: "rack_mounted", room_id: "room-1", rack_id: `rack-${index % 3}`, u_start: 10 + index, u_end: 12 + index, side: "front" },
}));
const FIXTURE_RACKS = Array.from({ length: 3 }, (_, index) => ({
  id: `rack-${index}`,
  name: `Row-${index + 1}-Rack-${LONG}`,
  asset_tag: `RK-${100 + index}`,
  lifecycle_status: "active",
  version: 1,
  placement: { room_id: "room-1", x_mm: 1000 * (index + 1), y_mm: 2000 },
}));
const FIXTURE_DEVICES = Array.from({ length: 6 }, (_, index) => ({
  id: `dev-${index}`,
  external_identifier: `00:11:22:33:44:${String(50 + index)}-${LONG}-uplink-port-${index}`,
  status: ["new", "reconciled", "ignored"][index % 3],
  matched_managed_asset_id: index % 3 === 1 ? `asset-${LONG}-${index}` : null,
}));
const FIXTURE_DIFFS = Array.from({ length: 2 }, (_, index) => ({
  id: `diff-${index}`,
  discovered_device_id: `dev-${index}`,
  diff_type: `unmatched_device_${LONG}`,
  status: "pending",
  field_name: null,
  discovered_value: null,
  authoritative_value: null,
}));
const FIXTURE_ALARMS = Array.from({ length: 4 }, (_, index) => ({
  id: `alarm-${index}`,
  rule_id: "rule-1",
  integration_id: "integration-1",
  managed_asset_id: `eq-${index}`,
  subject_key: `PDU-${LONG}-${index} · branch current`,
  status: index === 3 ? "ACKNOWLEDGED" : "ACTIVE",
  opened_at: minutesAgo(10 + index),
  acknowledged_at: index === 3 ? minutesAgo(2) : null,
  cleared_at: null,
  last_value: 100 + index,
  unit: "%",
  presentation_value: 100 + index,
  presentation_unit: "%",
}));
const FIXTURE_EXCEPTIONS = [
  { code: "POWER_NODE_OVERLOADED_ON_SECONDARY_FEED", severity: "critical", object_type: "power_node", object_id: "p1", message: `PDU-${LONG} load 104% of configured capacity` },
  { code: "REDUNDANCY_DEGRADED", severity: "warning", object_type: "equipment", object_id: "e1", message: "SRV-0142 has a single active feed" },
];
const FIXTURE_COLLECTORS = [
  { id: "c1", name: `edge-${LONG}`, collector_type: "edge", site_id: null, status: "active", version_string: "1", health: "offline", last_heartbeat_at: null, seconds_since_heartbeat: 5700 },
  { id: "c2", name: "central-01", collector_type: "central", site_id: null, status: "active", version_string: "1", health: "healthy", last_heartbeat_at: null, seconds_since_heartbeat: 12 },
];
const FIXTURE_SUMMARY = {
  site_summary: { sites: 3, buildings: 5, floors: 12, rooms: 28, racks: 412, equipment: 2318, power_nodes: 164 },
  rack_summary: { total_racks: 412, occupied_racks: 338, available_racks: 74 },
  capacity_summary: { total_configured_kw: 4800, nodes_with_known_capacity: 151, nodes_with_unknown_capacity: 13 },
  power_summary: { overloaded_nodes: 2, near_capacity_nodes: 9, redundancy_degraded_equipment: 6 },
};
const FIXTURE_INTEGRATIONS = Array.from({ length: 3 }, (_, index) => ({
  id: `int-${index}`,
  name: `core-snmp-${LONG}-${index}`,
  integration_type: "snmp",
  target_host: `10.20.30.${index}.${LONG}.example.test`,
  enabled: index !== 1,
  last_poll_at: minutesAgo(5),
  last_success_at: minutesAgo(6),
  consecutive_failures: index,
  has_credential: true,
  version: 1,
}));
const asPage = (items: unknown[]) => ({ items, total: items.length, limit: 200, offset: 0 });

/** What each fixture-served GET returns; anything else is passed through to the real backend. */
function fixtureFor(pathname: string, search: URLSearchParams): unknown {
  switch (pathname) {
    case "/auth/me": return FIXTURE_ME;
    case "/equipment": return asPage(FIXTURE_EQUIPMENT);
    case "/racks": return asPage(FIXTURE_RACKS);
    case "/rooms": return asPage([{ id: "room-1", name: `Room-${LONG}`, code: "R1", room_type: "data_hall" }]);
    case "/discovery/devices": return FIXTURE_DEVICES;
    case "/discovery/reconciliation": return FIXTURE_DIFFS;
    case "/alarms": return FIXTURE_ALARMS.filter((alarm) => !search.get("status") || alarm.status === search.get("status"));
    case "/dashboard/exceptions": return FIXTURE_EXCEPTIONS;
    case "/dashboard/summary": return FIXTURE_SUMMARY;
    case "/collectors": return FIXTURE_COLLECTORS;
    case "/integrations": return FIXTURE_INTEGRATIONS;
    default: return undefined;
  }
}

const RESPONSE_DELAY_MS = 1200;

/** Serves the fixtures after a delay, so the loading layout is on screen before the content. Returns the
 * number of fixture responses delivered, to prove the populated content was what got measured. */
async function serveFixtures(page: Page) {
  const delivered: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname.replace("/api/v1", "");
    const body = request.method() === "GET" ? fixtureFor(pathname, url.searchParams) : undefined;
    if (body === undefined) return route.fallback();
    if (pathname !== "/auth/me") await new Promise((resolve) => setTimeout(resolve, RESPONSE_DELAY_MS));
    delivered.push(pathname);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  return delivered;
}

const navigateTo = (page: Page, route: string) =>
  page.evaluate((target) => {
    window.history.pushState({}, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, route);

/** For each route: a heading, and the route's own populated content (never just a placeholder). */
interface RouteReadiness {
  route: string;
  heading: string;
  /** The fixture-served request this route depends on, when it has one. */
  served?: string;
  /** The loading layout this route shows first, when the fixture delays the response. */
  loading?: (page: Page) => Promise<void>;
  /** Resolves only when the populated content is on screen. */
  populated: (page: Page, check?: typeof expect) => Promise<void>;
  /** A scrolling region that must hold wider-than-viewport populated content at 320 px. */
  wideRegion?: string;
}

const rowsOf = (page: Page, tableName: string) => page.getByRole("table", { name: tableName }).locator("tbody tr");
const loadingStatus = (page: Page) => page.locator("[role=status][aria-busy=true]").first();
const waitForSettled = async (page: Page) => {
  await expect(page.locator("[role=status][aria-busy=true]")).toHaveCount(0);
};

const ROUTES: RouteReadiness[] = [
  {
    route: "/",
    heading: "Operations overview",
    served: "/dashboard/summary",
    populated: async (page, check = expect) => {
      for (const id of ["alarms", "exceptions", "collectors", "inventory"]) {
        await check(page.locator(`[data-section="${id}"]`)).toHaveAttribute("data-state", "ready");
      }
      await check(page.locator('[data-section="alarms"] li')).toHaveCount(3);
      await check(page.locator('[data-section="exceptions"] li')).toHaveCount(FIXTURE_EXCEPTIONS.length);
    },
  },
  {
    route: "/events",
    heading: "Events",
    served: "/alarms",
    loading: async (page) => { await expect(loadingStatus(page)).toBeVisible(); },
    populated: async (page, check = expect) => { await check(rowsOf(page, "Events")).toHaveCount(FIXTURE_ALARMS.length); },
    wideRegion: "Events table",
  },
  {
    route: "/equipment",
    heading: "Equipment",
    served: "/equipment",
    loading: async (page) => { await expect(loadingStatus(page)).toBeVisible(); await expect(page.getByRole("table", { name: "Equipment" })).toHaveCount(0); },
    populated: async (page, check = expect) => {
      await check(rowsOf(page, "Equipment")).toHaveCount(FIXTURE_EQUIPMENT.length);
      await check(page.getByRole("link", { name: FIXTURE_EQUIPMENT[0].hostname })).toBeVisible();
    },
    wideRegion: "Equipment table",
  },
  {
    route: "/racks",
    heading: "Racks",
    // Its racks request was already answered while the Equipment page loaded and is cached, so there is no
    // loading layout to assert here; the populated content is still awaited.
    served: "/racks",
    populated: async (page, check = expect) => { await check(rowsOf(page, "Racks")).toHaveCount(FIXTURE_RACKS.length); },
    wideRegion: "Racks table",
  },
  {
    route: "/integrations",
    heading: "Integrations",
    served: "/integrations",
    loading: async (page) => { await expect(loadingStatus(page)).toBeVisible(); },
    populated: async (page, check = expect) => { await check(rowsOf(page, "Integrations")).toHaveCount(FIXTURE_INTEGRATIONS.length); },
    wideRegion: "Integrations table",
  },
  {
    route: "/discovery",
    heading: "Discovery & Reconciliation",
    served: "/discovery/devices",
    loading: async (page) => { await expect(loadingStatus(page)).toBeVisible(); await expect(page.getByRole("table", { name: "Discovered devices" })).toHaveCount(0); },
    populated: async (page, check = expect) => {
      await check(rowsOf(page, "Discovered devices")).toHaveCount(FIXTURE_DEVICES.length);
      // The pending reconciliation cards carry the long device identifiers too.
      await check(page.getByText(FIXTURE_DEVICES[0].external_identifier).first()).toBeVisible();
      await check(page.getByPlaceholder("Existing ManagedAsset ID to link")).toHaveCount(FIXTURE_DIFFS.length);
    },
    wideRegion: "Discovered devices table",
  },
  {
    // Uses the real backend: no fixture, so wait for a deliberate settled state rather than a populated one.
    route: "/cables",
    heading: "Cables",
    populated: async (page, check = expect) => {
      await waitForSettled(page);
      await check(page.getByRole("table", { name: "Cables" }).or(page.getByText("No cables recorded.")).or(page.getByRole("alert")).or(page.getByRole("heading", { name: "Access restricted" })).first()).toBeVisible();
    },
  },
];

async function openPopulated(page: Page, entry: RouteReadiness) {
  await navigateTo(page, entry.route);
  await expect(page.getByRole("heading", { level: 1, name: entry.heading })).toBeVisible();
  // The loading layout exists first (the fixture response is delayed), then the populated content replaces it.
  if (entry.loading) await entry.loading(page);
  await entry.populated(page);
  await waitForSettled(page);
  // Wait, then verify at this instant: nothing below may measure a loading layout. A 1 ms timeout makes these
  // a one-shot check that fails if the content is not already on screen (in Playwright a timeout of 0 means
  // "wait forever"), so a missing wait cannot hide behind a later one.
  const now = expect.configure({ timeout: 1 });
  await entry.populated(page, now);
  await now(loadingStatus(page)).toHaveCount(0);
}

const pageScrollsSideways = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);

/** A table or region that is wider than the viewport must scroll inside its own region, not widen the page. */
async function regionOverflow(page: Page, regionName: string) {
  const region = page.getByRole("region", { name: regionName });
  return region.evaluate((element) => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }));
}

test("populated pages reflow at 320 px (400% zoom) once their own content has loaded", async ({ page }) => {
  const delivered = await serveFixtures(page);
  await signIn(page);
  await page.setViewportSize({ width: 320, height: 640 });

  for (const entry of ROUTES) {
    await openPopulated(page, entry);
    if (entry.served) expect(delivered, `${entry.route}: the fixture response was delivered before measuring`).toContain(entry.served);
    if (entry.wideRegion) {
      // Populated content is wider than the 320 px window and scrolls inside its own region.
      const { scrollWidth, clientWidth } = await regionOverflow(page, entry.wideRegion);
      expect(scrollWidth, `${entry.route} populated region is wider than its box`).toBeGreaterThan(clientWidth);
    }
    expect(await pageScrollsSideways(page), `${entry.route} at 320 px`).toBe(false);
    expect(await spillingText(page), `${entry.route}: text spilling out of its own box at 320 px`).toEqual([]);
  }
});

// ---------------------------------------------------------------------------------------------------
// Text-only resize. Setting the root font size scales only rem-based text; shared components also use fixed
// pixel sizes (table headers 11px, status badges 10px, labels 12px). Browsers' own "zoom text only" scales
// those too, so this check does the same: every element's computed font size is read first, then each is set to
// twice that value, which scales pixel-sized and relative text alike. It runs only after a route is populated.
// ---------------------------------------------------------------------------------------------------

/** Removes any earlier scaling, so every pass starts from the page's own font sizes. */
const unscaleText = (page: Page) =>
  page.evaluate(() => {
    for (const element of Array.from(document.body.querySelectorAll<HTMLElement>("[data-text-scale]"))) {
      element.style.removeProperty("font-size");
      delete element.dataset.textScale;
    }
  });

/** Multiplies the computed font size of every element by `factor`, whether the page declared it in pixels,
 * rem or em. All sizes are read first and only then written: writing a parent's size changes what its
 * children compute, so reading and writing in one loop would scale nested text more than once. */
const scaleText = async (page: Page, factor: number) => {
  await unscaleText(page);
  return page.evaluate((scale) => {
    const elements = Array.from(document.body.querySelectorAll<HTMLElement>("*"));
    const sizes = elements.map((element) => parseFloat(getComputedStyle(element).fontSize));
    let scaled = 0;
    elements.forEach((element, index) => {
      const size = sizes[index];
      if (!Number.isFinite(size) || size === 0) return;
      element.dataset.textScale = String(size);
      element.style.setProperty("font-size", `${size * scale}px`, "important");
      scaled += 1;
    });
    return scaled;
  }, factor);
};

const computedSize = (page: Page, selector: string) =>
  page.locator(selector).first().evaluate((element) => parseFloat(getComputedStyle(element).fontSize));

/** Interactive and heading elements must stay inside the window horizontally, unless they sit in a scroll region. */
const unreachableControls = (page: Page) =>
  page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const found: string[] = [];
    for (const element of Array.from(document.querySelectorAll<HTMLElement>("h1, h2, button, a[href], input, select, textarea, [role=tab]"))) {
      if (element.closest("[role=region][tabindex]") || element.closest("[inert]")) continue;
      const box = element.getBoundingClientRect();
      if (box.width === 0 || box.height === 0) continue;
      if (box.left < -1 || box.right > width + 1) found.push(`${element.tagName.toLowerCase()} "${(element.textContent || element.getAttribute("aria-label") || "").trim().slice(0, 40)}" ${Math.round(box.left)}..${Math.round(box.right)}`);
    }
    return found;
  });

/** Headings, controls and labels whose text spills out of their own box (and is neither clipped by the box nor
 * ellipsised on purpose), which overlaps whatever is next to them. Scroll regions are excluded: their content
 * is meant to scroll. */
const spillingText = (page: Page) =>
  page.evaluate(() => {
    const found: string[] = [];
    for (const element of Array.from(document.querySelectorAll<HTMLElement>("h1, h2, h3, button, [role=tab], label, th, dt, dd"))) {
      if (element.closest("[role=region][tabindex]") || element.closest("[inert]")) continue;
      const style = getComputedStyle(element);
      if (style.textOverflow === "ellipsis" || style.overflowX !== "visible") continue;
      if (element.scrollWidth > element.clientWidth + 1) found.push(`${element.tagName.toLowerCase()} "${(element.textContent || element.getAttribute("aria-label") || "").trim().slice(0, 40)}" ${element.scrollWidth}>${element.clientWidth}`);
    }
    return found;
  });

test("text resize to 200% (fixed-pixel text included) keeps content and controls reachable without sideways page scroll", async ({ page }) => {
  await serveFixtures(page);
  await signIn(page);
  await page.setViewportSize({ width: 1280, height: 800 });

  // Representative fixed-pixel and relative text, sampled before and after scaling on the pages that hold them.
  const samples: Record<string, string[]> = {
    "/equipment": ['table[aria-label="Equipment"] thead th', 'table[aria-label="Equipment"] tbody td span.rounded-sm', "h1"],
    "/": ['[data-section="inventory"] p.text-xs', '[data-section="alarms"] h2', 'main span.rounded-sm'],
  };

  for (const entry of ROUTES) {
    await openPopulated(page, entry);
    await unscaleText(page);
    const before: Record<string, number> = {};
    for (const selector of samples[entry.route] ?? []) before[selector] = await computedSize(page, selector);

    const scaled = await scaleText(page, 2);
    expect(scaled, `${entry.route}: elements scaled`).toBeGreaterThan(20);
    for (const [selector, size] of Object.entries(before)) {
      expect(await computedSize(page, selector), `${entry.route}: ${selector} doubled from ${size}px`).toBeCloseTo(size * 2, 1);
    }

    expect(await pageScrollsSideways(page), `${entry.route} at 200% text`).toBe(false);
    expect(await unreachableControls(page), `${entry.route}: controls outside the window at 200% text`).toEqual([]);
    expect(await spillingText(page), `${entry.route}: text spilling out of its own box at 200% text`).toEqual([]);
  }

});

test("a fixed-pixel label is not scaled by the root font size alone, which is why the check scales every element", async ({ page }) => {
  await serveFixtures(page);
  await signIn(page);
  await navigateTo(page, "/equipment");
  await expect(rowsOf(page, "Equipment")).toHaveCount(FIXTURE_EQUIPMENT.length);

  const header = 'table[aria-label="Equipment"] thead th';
  const before = await computedSize(page, header);
  await page.addStyleTag({ content: "html { font-size: 200% !important; }" });
  expect(await computedSize(page, header), "root font size does not touch a 11px header").toBe(before);
  await scaleText(page, 2);
  expect(await computedSize(page, header)).toBeCloseTo(before * 2, 1);
});

test("signing out from /events leaves the sign-in title, and the next sign-in is titled again", async ({ page }) => {
  await signIn(page);
  await navigateTo(page, "/events");
  await expect(page).toHaveTitle("Events · DCIM Platform");

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page).toHaveTitle("Sign in · DCIM Platform");

  await page.getByRole("textbox", { name: "Email" }).fill(ADMIN_EMAIL);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  await expect(page).toHaveTitle("Dashboard · DCIM Platform");
});
