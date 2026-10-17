import { APIRequestContext, expect, test } from "@playwright/test";

import { buildXlsxWorkbook } from "./fixtures/xlsxBuilder";

/** Issue #148: the authorized location-hierarchy administration UI, exercised end to end
 * through the real backend. An Administrator logs in and builds
 * Organization → Country → City → Site → Building → Floor → Room entirely through the
 * cascade, which proves every existing create endpoint is reachable and permission-gated;
 * a second scenario pins the unauthenticated redirect; a third proves the rack bulk-import
 * "missing site" prerequisite is actionable and links back to location administration.
 *
 * The chain built here is exactly the prerequisite a rack bulk import resolves by
 * `site_code` — see backend/app/application/bulk_import/resolvers.py — which is why
 * "Locations" is a first-class navigation destination rather than a deep link. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

async function login(api: APIRequestContext): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  return ((await resp.json()) as { access_token: string }).access_token;
}

// A valid rack model, so a rack import row whose *only* problem is an unknown site_code
// exercises the exact missing-site path the UI is expected to make actionable.
async function seedRackModel(api: APIRequestContext, token: string): Promise<{ manufacturer: string; modelName: string }> {
  const headers = { Authorization: `Bearer ${token}` };
  const suffix = Math.random().toString(36).slice(2, 8);
  const manufacturer = `E2E Prereq Co ${suffix}`;
  const modelName = `RM-Prereq-${suffix}`;
  const rackModel = await (
    await api.post("/api/v1/rack-models", { headers, data: { manufacturer, model_name: modelName } })
  ).json();
  const revision = await api.post(`/api/v1/rack-models/${rackModel.id}/revisions`, {
    headers,
    data: { height_u: 10, width_mm: 600, depth_mm: 1000 },
  });
  expect(revision.ok(), `rack model revision failed: ${revision.status()} ${await revision.text()}`).toBeTruthy();
  return { manufacturer, modelName };
}

test.describe("Location hierarchy administration", () => {
  test("an unauthenticated visitor is redirected to login", async ({ page }) => {
    await page.goto("/locations");

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Location Administration" })).toHaveCount(0);
  });

  test("an Administrator builds the full hierarchy through the cascade", async ({ page }) => {
    const suffix = Math.random().toString(36).slice(2, 8);

    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    await page.getByRole("link", { name: "Locations", exact: true }).click();
    await expect(page).toHaveURL("/locations");
    await expect(page.getByRole("heading", { name: "Location Administration" })).toBeVisible();

    const orgName = `E2E Org ${suffix}`;
    await page.getByLabel("Organization name").fill(orgName);
    await page.getByRole("button", { name: "Create organization" }).click();
    await page.getByRole("button", { name: new RegExp(orgName) }).click();

    const countryName = `E2E Country ${suffix}`;
    await page.getByLabel("Country name").fill(countryName);
    await page.getByRole("button", { name: "Create country" }).click();
    await page.getByRole("button", { name: new RegExp(countryName) }).click();

    const cityName = `E2E City ${suffix}`;
    await page.getByLabel("City name").fill(cityName);
    await page.getByRole("button", { name: "Create city" }).click();
    await page.getByRole("button", { name: new RegExp(cityName) }).click();

    const siteCode = `E2ESITE-${suffix}`;
    const siteName = `E2E Site ${suffix}`;
    await page.getByLabel("Site code").fill(siteCode);
    await page.getByLabel("Site name").fill(siteName);
    await page.getByRole("button", { name: "Create site" }).click();
    await page.getByRole("button", { name: new RegExp(siteName) }).click();

    const buildingName = `E2E Building ${suffix}`;
    await page.getByLabel("Building code").fill(`B-${suffix}`);
    await page.getByLabel("Building name").fill(buildingName);
    await page.getByRole("button", { name: "Create building" }).click();
    await page.getByRole("button", { name: new RegExp(buildingName) }).click();

    const floorName = `E2E Floor ${suffix}`;
    await page.getByLabel("Floor name").fill(floorName);
    await page.getByLabel("Level number").fill("7");
    await page.getByRole("button", { name: "Create floor" }).click();
    await page.getByRole("button", { name: new RegExp(floorName) }).click();

    const roomName = `E2E Room ${suffix}`;
    await page.getByLabel("Room code").fill(`R-${suffix}`);
    await page.getByLabel("Room name").fill(roomName);
    await page.getByRole("button", { name: "Create room" }).click();

    const roomButton = page.getByRole("button", { name: new RegExp(roomName) });
    await expect(roomButton).toBeVisible();
    await roomButton.click();

    // Selecting creates the optimistic-concurrency edit affordance the API supports (PATCH
    // /rooms/{id} with If-Match); there is deliberately no delete.
    await expect(page.getByLabel("Edit room name")).toBeVisible();

    const path = page.getByRole("navigation", { name: "Location path" });
    await expect(path).toContainText(orgName);
    await expect(path).toContainText(siteName);
    await expect(path).toContainText(floorName);
  });

  test("a rack import whose site_code is unknown shows an actionable location prerequisite", async ({
    page,
    playwright,
  }) => {
    const api = await playwright.request.newContext({ baseURL: process.env.BASE_URL ?? "http://127.0.0.1:5173" });
    const token = await login(api);
    const { manufacturer, modelName } = await seedRackModel(api, token);
    await api.dispose();

    const suffix = Math.random().toString(36).slice(2, 8);
    const missingSiteCode = `NO-SUCH-SITE-${suffix}`;

    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    await page.getByRole("link", { name: "Racks", exact: true }).click();
    await page.getByRole("button", { name: "Bulk Import" }).click();
    const dialog = page.getByRole("dialog", { name: "Bulk import Rack" });
    await expect(dialog).toBeVisible();

    const workbook = buildXlsxWorkbook(
      "Racks",
      [
        "asset_tag", "rack_name", "manufacturer", "model_name", "revision_number", "site_code", "building_code",
        "floor_level", "room_code", "x_mm", "y_mm", "rotation_deg", "owner", "notes",
      ],
      [
        [
          `E2E-MISSING-SITE-${suffix}`, `E2E Missing Site Rack ${suffix}`, manufacturer, modelName, "", missingSiteCode,
          "A", 1, "R1", "", "", "", "", "",
        ],
      ],
    );

    await dialog.getByLabel("Import file").setInputFiles({
      name: "rack-missing-site.xlsx",
      mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: workbook,
    });
    await dialog.getByRole("button", { name: "Upload" }).click();

    const notice = dialog.getByTestId("missing-site-prerequisite");
    await expect(notice).toBeVisible({ timeout: 15_000 });
    await expect(notice).toContainText(missingSiteCode);

    // The navigation affordance is the whole point: it must route to the hierarchy admin.
    const link = notice.getByRole("link", { name: /Location Administration/ });
    await expect(link).toHaveAttribute("href", "/locations");
  });
});
