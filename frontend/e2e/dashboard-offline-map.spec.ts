import { createHash } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

// Issue #365 offline basemap. Every API call is mocked, so this spec needs no backend. The basemap itself is the
// real bundled Natural Earth file; overrides are served with page.route at the same-origin /map-data/ paths.
const USER = {
  id: "1",
  email: "ops@example.com",
  full_name: "Ops",
  role_names: ["Operator"],
  permission_codes: ["alarm:read", "dashboard:read", "collector:read", "location:read"],
};

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

const WITHHELD = {
  ...GEOGRAPHY,
  coordinates: "withheld",
  totals: { ...GEOGRAPHY.totals, sites_with_coordinates: null, sites_without_coordinates: null },
  sites: GEOGRAPHY.sites.map((site) => ({ ...site, latitude: null, longitude: null })),
};

const WITHHELD_MESSAGE = "Precise site coordinates are withheld for your access level. Countries with authorized sites are highlighted.";

async function mockApi(page: Page, geography: unknown = GEOGRAPHY) {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) return route.fulfill({ json: USER });
    if (path.endsWith("/auth/refresh")) return route.fulfill({ json: { access_token: "t" } });
    if (path.endsWith("/dashboard/geography")) return route.fulfill({ json: geography });
    return route.fulfill({ json: [] });
  });
}

/** A tiny valid custom basemap: three boxes, one of them the UAE. */
const box = (w: number, s: number, e: number, n: number) => [w, s, e, s, e, n, w, n].map((d) => Math.round(d * 100));
const CUSTOM = {
  format: "dcim-basemap",
  schema_version: 1,
  dataset_id: "test-borders",
  version: "9.9.9-test",
  projection: "equirectangular",
  precision: 100,
  countries: [
    { iso2: "AE", name: "United Arab Emirates", polygons: [[box(51.5, 22.6, 56.4, 26.1)]] },
    { iso2: "GB", name: "United Kingdom", polygons: [[box(-8, 50, 2, 59)]] },
    { iso2: "BR", name: "Brazil", polygons: [[box(-74, -34, -35, 5)]] },
  ],
};
const CUSTOM_BODY = JSON.stringify(CUSTOM);

function manifest(overrides: Record<string, unknown> = {}) {
  return {
    format: "dcim-basemap-manifest",
    schema_version: 1,
    dataset_id: CUSTOM.dataset_id,
    version: CUSTOM.version,
    file: "basemap-test.json",
    bytes: new TextEncoder().encode(CUSTOM_BODY).length,
    sha256: createHash("sha256").update(CUSTOM_BODY).digest("hex"),
    created_at: "2026-11-18T00:00:00Z",
    source: { name: "Test", url: "https://example.invalid/", license: "Public domain", source_sha256: "0".repeat(64) },
    ...overrides,
  };
}

async function serveOverride(page: Page, manifestBody: unknown) {
  await page.route("**/map-data/manifest.json", (route) => route.fulfill({ json: manifestBody }));
  await page.route("**/map-data/basemap-test.json", (route) => route.fulfill({ contentType: "application/json", body: CUSTOM_BODY }));
}

const geography = (page: Page) => page.locator('[data-section="geography"]');
const countryPaths = (page: Page) => geography(page).locator('[data-testid="basemap-country"]');
const caption = (page: Page) => page.getByTestId("basemap-caption");

test.describe("offline basemap", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a full dashboard load makes no request outside the app's own origin and draws the bundled countries", async ({ page, context, baseURL }) => {
    const appOrigin = new URL(baseURL ?? "http://localhost").origin;
    const external: string[] = [];
    const seen: string[] = [];
    // Context-level: anything not answered by the page's API mock lands here; foreign origins are aborted.
    await context.route("**/*", (route) => {
      const url = route.request().url();
      if (new URL(url).origin !== appOrigin) {
        external.push(url);
        return route.abort();
      }
      return route.fallback();
    });
    page.on("request", (request) => seen.push(request.url()));
    await mockApi(page);
    await page.goto("/");
    await expect(geography(page)).toHaveAttribute("data-state", "ready");
    await expect(caption(page)).toHaveText("Basemap: Natural Earth 50m 5.1.2-r1 (bundled)");
    expect(await countryPaths(page).count()).toBeGreaterThan(150);
    await expect(geography(page).locator('[data-testid="basemap-country"][data-highlighted="true"]')).toHaveCount(2);
    await expect(geography(page).locator('[data-iso2="AE"]')).toHaveAttribute("data-highlighted", "true");
    await expect(geography(page).locator('[data-iso2="GB"]')).toHaveAttribute("data-highlighted", "true");
    await expect(geography(page).locator('[data-iso2="SA"]')).not.toHaveAttribute("data-highlighted", "true");
    await expect(page.getByTestId("basemap-note")).toHaveCount(0);
    await page.waitForLoadState("networkidle");

    expect(external, "external requests").toEqual([]);
    const foreign = seen.filter((url) => !url.startsWith("data:") && !url.startsWith("blob:") && new URL(url).origin !== appOrigin);
    expect(foreign, "requests to another origin").toEqual([]);
    expect(seen.some((url) => url === `${appOrigin}/map-data/manifest.json`)).toBe(true);
  });

  test("a valid custom basemap is used and captioned as custom", async ({ page }) => {
    await mockApi(page);
    await serveOverride(page, manifest());
    await page.goto("/");
    await expect(caption(page)).toHaveText("Basemap: test-borders 9.9.9-test (custom)");
    await expect(countryPaths(page)).toHaveCount(3);
    await expect(geography(page).locator('[data-iso2="AE"]')).toHaveAttribute("data-highlighted", "true");
    await expect(page.getByTestId("basemap-note")).toHaveCount(0);
  });

  test("a custom basemap with a wrong checksum falls back to the bundled one with a visible note", async ({ page }) => {
    await mockApi(page);
    await serveOverride(page, manifest({ sha256: "0".repeat(64) }));
    await page.goto("/");
    await expect(caption(page)).toHaveText("Basemap: Natural Earth 50m 5.1.2-r1 (bundled)");
    await expect(page.getByTestId("basemap-note")).toHaveText("Basemap notice: Custom basemap 9.9.9-test was rejected (checksum mismatch); using bundled basemap 5.1.2-r1.");
    await expect(page.getByTestId("basemap-note")).toBeVisible();
    expect(await countryPaths(page).count()).toBeGreaterThan(150);
  });

  test("a manifest with an incompatible schema_version falls back to the bundled basemap", async ({ page }) => {
    await mockApi(page);
    await serveOverride(page, manifest({ schema_version: 2 }));
    await page.goto("/");
    await expect(caption(page)).toHaveText("Basemap: Natural Earth 50m 5.1.2-r1 (bundled)");
    await expect(page.getByTestId("basemap-note")).toContainText("incompatible schema");
  });

  test("a missing manifest (404) silently uses the bundled basemap", async ({ page }) => {
    await mockApi(page);
    await page.route("**/map-data/manifest.json", (route) => route.fulfill({ status: 404, body: "not found" }));
    await page.goto("/");
    await expect(caption(page)).toHaveText("Basemap: Natural Earth 50m 5.1.2-r1 (bundled)");
    await expect(page.getByTestId("basemap-note")).toHaveCount(0);
  });

  test("withheld coordinates show the message, no markers, and the full list", async ({ page }) => {
    await mockApi(page, WITHHELD);
    await page.goto("/");
    await expect(geography(page)).toHaveAttribute("data-state", "ready");
    await expect(page.getByTestId("coordinates-withheld")).toHaveText(WITHHELD_MESSAGE);
    await expect(caption(page)).toContainText("(bundled)");
    await expect(page.getByTestId("map-site")).toHaveCount(0);
    await expect(page.getByTestId("map-cluster")).toHaveCount(0);
    await expect(page.getByTestId("sites-without-coordinates")).toHaveCount(0);
    await expect(geography(page).locator('[data-highlighted="true"]')).toHaveCount(2);

    const list = page.getByTestId("geography-list");
    await list.getByRole("button", { name: "UAE" }).click();
    await expect(page.getByTestId("map-status")).toContainText("Zoom 32×");
    await list.getByRole("button", { name: "Dubai", exact: true }).click();
    await expect(list.getByRole("button", { name: "Dubai Two (DXB2)" })).toBeVisible();
    await expect(list.getByRole("button", { name: "Dubai One (DXB1)" })).toBeVisible();
  });

  test("the map and the country list work from the keyboard", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    await expect(caption(page)).toContainText("(bundled)");
    const svg = geography(page).locator("svg").first();

    const map = page.getByRole("group", { name: /Site map/ });
    await map.focus();
    await expect(map).toBeFocused();
    await page.keyboard.press("+");
    await expect(svg).toHaveAttribute("viewBox", "90 45 180 90");
    await page.keyboard.press("ArrowRight");
    await expect(svg).not.toHaveAttribute("viewBox", "90 45 180 90");
    await page.keyboard.press("-");
    await expect(svg).toHaveAttribute("viewBox", "0 0 360 180");

    const list = page.getByTestId("geography-list");
    await list.getByRole("button", { name: "United Kingdom" }).focus();
    await page.keyboard.press("Enter");
    await expect(list.getByRole("button", { name: "United Kingdom" })).toHaveAttribute("aria-expanded", "true");
    await expect(svg).not.toHaveAttribute("viewBox", "0 0 360 180");
    await page.keyboard.press("Tab");
    await expect(list.getByRole("button", { name: "London", exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(list.getByRole("button", { name: "London One (LON1)" })).toBeVisible();
  });
});

for (const width of [1440, 820, 390]) {
  test.describe(`offline basemap at ${width}px wide`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("the dashboard with the map does not scroll sideways in either coordinates mode", async ({ page }) => {
      for (const data of [GEOGRAPHY, WITHHELD]) {
        await mockApi(page, data);
        await page.goto("/");
        await expect(caption(page)).toContainText("(bundled)");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow, "horizontal overflow").toBeLessThanOrEqual(0);
        await page.unrouteAll({ behavior: "ignoreErrors" });
      }
    });
  });
}
