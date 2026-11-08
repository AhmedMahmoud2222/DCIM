import { APIRequestContext, Page, Playwright, expect, test } from "@playwright/test";

import { buildXlsxWorkbook } from "./fixtures/xlsxBuilder";

/** Issue #148: the authorized location-hierarchy administration UI, exercised end to end
 * through the real backend. An Administrator logs in and builds
 * Organization → Country → City → Site → Building → Floor → Room entirely through the
 * cascade, which proves every existing create endpoint is reachable and permission-gated;
 * the same flow then proves the rows are *durably* persisted (authenticated API read-back
 * of every level and its parent id, not just the optimistic UI); a duplicate site code
 * proves the 409 path renders as an actionable message; and the rack bulk-import scenarios
 * prove the site built here is the real prerequisite a rack import resolves by
 * `site_code` — a valid row commits a rack that is read back over the API, while a row
 * with no catalog model stays an error and auto-creates no reference data.
 *
 * The chain built here is exactly the prerequisite a rack bulk import resolves by
 * `site_code` — see backend/app/application/bulk_import/resolvers.py — which is why
 * "Locations" is a first-class navigation destination rather than a deep link. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

const RACK_IMPORT_HEADERS = [
  "asset_tag", "rack_name", "manufacturer", "model_name", "revision_number", "site_code", "building_code",
  "floor_level", "room_code", "x_mm", "y_mm", "rotation_deg", "owner", "notes",
];

/** The bulk-import parser numbers *real spreadsheet* rows — the header occupies row 1, so the
 * first data row a workbook built here produces is row_number 2, not 1
 * (backend/app/application/bulk_import/parsing.py). Asserting on row 1 silently matched
 * nothing at all. */
const FIRST_DATA_ROW_NUMBER = 2;

interface Api {
  api: APIRequestContext;
  headers: { Authorization: string };
}

interface HierarchyNames {
  orgName: string;
  countryName: string;
  cityName: string;
  siteCode: string;
  siteName: string;
  buildingCode: string;
  buildingName: string;
  floorName: string;
  roomCode: string;
  roomName: string;
}

async function login(api: APIRequestContext): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  return ((await resp.json()) as { access_token: string }).access_token;
}

async function authorizedApi(playwright: Playwright): Promise<Api> {
  const api = await playwright.request.newContext({ baseURL: process.env.BASE_URL ?? "http://127.0.0.1:5173" });
  const token = await login(api);
  return { api, headers: { Authorization: `Bearer ${token}` } };
}

async function apiItems<T = Record<string, unknown>>(ctx: Api, path: string): Promise<T[]> {
  const resp = await ctx.api.get(path, { headers: ctx.headers });
  expect(resp.ok(), `GET ${path} failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  return ((await resp.json()) as { items: T[] }).items;
}

/** Every row of a bulk-import job, read over the authenticated API (not the optimistic UI). */
async function jobRows(ctx: Api, jobId: string): Promise<Array<{ row_number: number; status: string }>> {
  return apiItems<{ row_number: number; status: string }>(ctx, `/api/v1/import-jobs/${jobId}/rows?limit=25`);
}

async function loginThroughUi(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}

/** Drives the whole cascade through the browser — every create form and every parent
 * selection — and returns the human-readable names, which the API read-back then resolves
 * to real rows and ids. Client-side navigation throughout: the access token lives only in
 * memory (src/lib/authStore.ts), so a hard navigation would bounce back to /login. */
async function buildHierarchyThroughUi(page: Page): Promise<HierarchyNames> {
  const suffix = Math.random().toString(36).slice(2, 8);
  const names: HierarchyNames = {
    orgName: `E2E Org ${suffix}`,
    countryName: `E2E Country ${suffix}`,
    cityName: `E2E City ${suffix}`,
    siteCode: `E2ESITE-${suffix}`,
    siteName: `E2E Site ${suffix}`,
    buildingCode: `B-${suffix}`,
    buildingName: `E2E Building ${suffix}`,
    floorName: `E2E Floor ${suffix}`,
    roomCode: `R-${suffix}`,
    roomName: `E2E Room ${suffix}`,
  };

  await page.getByRole("link", { name: "Location Hierarchy", exact: true }).click();
  await expect(page).toHaveURL("/locations");
  await expect(page.getByRole("heading", { name: "Location Administration" })).toBeVisible();

  await page.getByLabel("Organization name").fill(names.orgName);
  await page.getByRole("button", { name: "Create organization" }).click();
  await page.getByRole("button", { name: new RegExp(names.orgName) }).click();

  await page.getByLabel("Country name").fill(names.countryName);
  await page.getByRole("button", { name: "Create country" }).click();
  await page.getByRole("button", { name: new RegExp(names.countryName) }).click();

  await page.getByLabel("City name").fill(names.cityName);
  await page.getByRole("button", { name: "Create city" }).click();
  await page.getByRole("button", { name: new RegExp(names.cityName) }).click();

  await page.getByLabel("Site code").fill(names.siteCode);
  await page.getByLabel("Site name").fill(names.siteName);
  await page.getByRole("button", { name: "Create site" }).click();
  await page.getByRole("button", { name: new RegExp(names.siteName) }).click();

  await page.getByLabel("Building code").fill(names.buildingCode);
  await page.getByLabel("Building name").fill(names.buildingName);
  await page.getByRole("button", { name: "Create building" }).click();
  await page.getByRole("button", { name: new RegExp(names.buildingName) }).click();

  await page.getByLabel("Floor name").fill(names.floorName);
  await page.getByLabel("Level number").fill("7");
  await page.getByRole("button", { name: "Create floor" }).click();
  await page.getByRole("button", { name: new RegExp(names.floorName) }).click();

  await page.getByLabel("Room code").fill(names.roomCode);
  await page.getByLabel("Room name").fill(names.roomName);
  await page.getByRole("button", { name: "Create room" }).click();

  const roomButton = page.getByRole("button", { name: new RegExp(names.roomName) });
  await expect(roomButton).toBeVisible();
  await roomButton.click();

  // Selecting creates the optimistic-concurrency edit affordance the API supports (PATCH
  // /rooms/{id} with If-Match); there is deliberately no delete.
  await expect(page.getByLabel("Edit room name")).toBeVisible();

  const path = page.getByRole("navigation", { name: "Location path" });
  await expect(path).toContainText(names.orgName);
  await expect(path).toContainText(names.siteName);
  await expect(path).toContainText(names.floorName);

  return names;
}

interface ResolvedHierarchy {
  org: { id: string; name: string };
  country: { id: string; organization_id: string; name: string };
  city: { id: string; country_id: string; name: string };
  site: { id: string; city_id: string; code: string; name: string };
  building: { id: string; site_id: string; name: string };
  floor: { id: string; building_id: string; name: string; level_number: number };
  room: { id: string; floor_id: string; name: string };
}

interface IdRow { id: string }
interface CountryRow extends IdRow { organization_id: string; name: string }
interface CityRow extends IdRow { country_id: string; name: string }
interface SiteRow extends IdRow { city_id: string; code: string; name: string }
interface BuildingRow extends IdRow { site_id: string; name: string }
interface FloorRow extends IdRow { building_id: string; name: string; level_number: number }
interface RoomRow extends IdRow { floor_id: string; name: string }

function require<T>(value: T | undefined, message: string): T {
  expect(value, message).toBeTruthy();
  return value as T;
}

/** Independent, authenticated read-back of the hierarchy the UI just created: each level
 * is fetched by its parent id and its parent foreign key is asserted, so a UI that only
 * *looked* like it persisted a parent id cannot pass. */
async function resolveHierarchyThroughApi(ctx: Api, names: HierarchyNames): Promise<ResolvedHierarchy> {
  const orgs = await apiItems<{ id: string; name: string }>(ctx, "/api/v1/organizations?limit=200");
  const org = require(orgs.find((o) => o.name === names.orgName), "created organization must be readable");

  const countries = await apiItems<CountryRow>(ctx, `/api/v1/countries?organization_id=${org.id}&limit=200`);
  const country = require(countries.find((c) => c.name === names.countryName), "created country must be readable");
  expect(country.organization_id).toBe(org.id);

  const cities = await apiItems<CityRow>(ctx, `/api/v1/cities?country_id=${country.id}&limit=200`);
  const city = require(cities.find((c) => c.name === names.cityName), "created city must be readable");
  expect(city.country_id).toBe(country.id);

  const sites = await apiItems<SiteRow>(ctx, `/api/v1/sites?city_id=${city.id}&limit=200`);
  const site = require(sites.find((s) => s.code === names.siteCode), "created site must be readable by code");
  expect(site.city_id).toBe(city.id);

  const buildings = await apiItems<BuildingRow>(ctx, `/api/v1/buildings?site_id=${site.id}&limit=200`);
  const building = require(buildings.find((b) => b.name === names.buildingName), "created building must be readable");
  expect(building.site_id).toBe(site.id);

  const floors = await apiItems<FloorRow>(ctx, `/api/v1/floors?building_id=${building.id}&limit=200`);
  const floor = require(floors.find((f) => f.name === names.floorName), "created floor must be readable");
  expect(floor.building_id).toBe(building.id);
  expect(floor.level_number).toBe(7);

  const rooms = await apiItems<RoomRow>(ctx, `/api/v1/rooms?floor_id=${floor.id}&limit=200`);
  const room = require(rooms.find((r) => r.name === names.roomName), "created room must be readable");
  expect(room.floor_id).toBe(floor.id);

  return { org, country, city, site, building, floor, room };
}

/** Seeds the legacy RackModel/RackModelRevision pair the rack validator resolves by
 * (manufacturer, model_name) via resolvers.py's `resolve_rack_model_revision`. */
async function seedRackModel(ctx: Api): Promise<{ manufacturer: string; modelName: string }> {
  const suffix = Math.random().toString(36).slice(2, 8);
  const manufacturer = `E2E Prereq Co ${suffix}`;
  const modelName = `RM-Prereq-${suffix}`;
  const rackModel = await (
    await ctx.api.post("/api/v1/rack-models", { headers: ctx.headers, data: { manufacturer, model_name: modelName } })
  ).json();
  const revision = await ctx.api.post(`/api/v1/rack-models/${rackModel.id}/revisions`, {
    headers: ctx.headers,
    data: { height_u: 10, width_mm: 600, depth_mm: 1000 },
  });
  expect(revision.ok(), `rack model revision failed: ${revision.status()} ${await revision.text()}`).toBeTruthy();
  return { manufacturer, modelName };
}

/** Rack models are never auto-created by a rack import; this scans every page so a
 * reference row cannot hide past the first page. */
async function findRackModel(ctx: Api, manufacturer: string, modelName: string): Promise<unknown> {
  let offset = 0;
  for (;;) {
    const page = await (
      await ctx.api.get(`/api/v1/rack-models?limit=200&offset=${offset}`, { headers: ctx.headers })
    ).json();
    const found = (page.items as Array<{ manufacturer: string; model_name: string }>).find(
      (m) => m.manufacturer === manufacturer && m.model_name === modelName,
    );
    if (found) return found;
    offset += 200;
    if (offset >= page.total) return null;
  }
}

async function openRackImport(page: Page) {
  await page.getByRole("link", { name: "Racks", exact: true }).click();
  await expect(page).toHaveURL("/racks");
  await page.getByRole("button", { name: "Bulk Import" }).click();
  const dialog = page.getByRole("dialog", { name: "Bulk import Rack" });
  await expect(dialog).toBeVisible();
  return dialog;
}

function rackWorkbook(rows: string[][]) {
  return buildXlsxWorkbook("Racks", RACK_IMPORT_HEADERS, rows);
}

test.describe("Location hierarchy administration", () => {
  test("an unauthenticated visitor is redirected to login", async ({ page }) => {
    await page.goto("/locations");

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Location Administration" })).toHaveCount(0);
  });

  test("an Administrator builds the full hierarchy through the cascade and it persists with the right parents", async ({
    page,
    playwright,
  }) => {
    const ctx = await authorizedApi(playwright);
    try {
      await loginThroughUi(page);
      const names = await buildHierarchyThroughUi(page);

      // Durable-state proof: every level is re-read over the authenticated API and its
      // parent foreign key is asserted — the UI only *displayed* a chain until this passes.
      const resolved = await resolveHierarchyThroughApi(ctx, names);
      expect(resolved.room.floor_id).toBe(resolved.floor.id);
    } finally {
      await ctx.api.dispose();
    }
  });

  test("a duplicate site code surfaces an actionable conflict in the UI", async ({ page, playwright }) => {
    const ctx = await authorizedApi(playwright);
    try {
      await loginThroughUi(page);
      const names = await buildHierarchyThroughUi(page);
      const resolved = await resolveHierarchyThroughApi(ctx, names);

      // Re-submitting the same code in the same city must not create a second site: the
      // backend answers 409 and the form shows the "already exists" message.
      await page.getByLabel("Site code").fill(names.siteCode);
      await page.getByLabel("Site name").fill(`${names.siteName} dup`);
      await page.getByRole("button", { name: "Create site" }).click();

      await expect(page.getByText(/A site with these details already exists/)).toBeVisible();

      const sites = await apiItems<SiteRow>(ctx, `/api/v1/sites?city_id=${resolved.city.id}&limit=200`);
      expect(sites.filter((s) => s.code === names.siteCode)).toHaveLength(1);
    } finally {
      await ctx.api.dispose();
    }
  });

  test("a rack import against a site created in the UI commits the rack and its row result", async ({
    page,
    playwright,
  }) => {
    const ctx = await authorizedApi(playwright);
    try {
      const { manufacturer, modelName } = await seedRackModel(ctx);

      await loginThroughUi(page);
      const names = await buildHierarchyThroughUi(page);
      const { site } = await resolveHierarchyThroughApi(ctx, names);

      const suffix = Math.random().toString(36).slice(2, 8);
      const assetTag = `E2E-IMPORT-RACK-${suffix}`;
      const rackName = `E2E Import Rack ${suffix}`;

      const dialog = await openRackImport(page);
      const workbook = rackWorkbook([
        [
          assetTag, rackName, manufacturer, modelName, "", names.siteCode, names.buildingCode, 7, names.roomCode,
          "", "", "", "", "",
        ],
      ]);
      await dialog.getByLabel("Import file").setInputFiles({
        name: "rack-import.xlsx",
        mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        buffer: workbook,
      });

      const [uploadResp] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === "POST" && /\/racks\/import-jobs/.test(r.url())),
        dialog.getByRole("button", { name: "Upload" }).click(),
      ]);
      const job = (await uploadResp.json()) as { id: string };

      const statusBadge = dialog.getByTestId("bulk-import-job-status");
      await expect(statusBadge).toHaveText("validated", { timeout: 15_000 });
      await expect(dialog.getByRole("cell", { name: "valid" })).toBeVisible();

      await dialog.getByRole("button", { name: "Commit import" }).click();
      await expect(statusBadge).toHaveText("committed", { timeout: 15_000 });

      // Row results, asserted against the generic job pipeline rather than the UI table.
      // This workbook has exactly one data row (row_number 2, under the header row); it must
      // reach the terminal "committed" state and there must be no other rows.
      await expect
        .poll(async () => (await jobRows(ctx, job.id)).find((r) => r.row_number === FIRST_DATA_ROW_NUMBER)?.status, {
          timeout: 15_000,
        })
        .toBe("committed");
      const committedRows = await jobRows(ctx, job.id);
      expect(committedRows.map((r) => r.row_number)).toEqual([FIRST_DATA_ROW_NUMBER]);
      expect(committedRows.map((r) => r.status)).toEqual(["committed"]);

      // The committed rack is durable and scoped to the site the UI created.
      const racks = await apiItems<{ asset_tag: string; name: string }>(ctx, `/api/v1/racks?site_id=${site.id}&limit=200`);
      const committed = require(racks.find((r) => r.asset_tag === assetTag), "committed rack must be readable by asset_tag");
      expect(committed.name).toBe(rackName);

      await dialog.getByRole("button", { name: "Close" }).click();
      await expect(page.getByRole("link", { name: rackName })).toBeVisible();
    } finally {
      await ctx.api.dispose();
    }
  });

  test("a rack row with no catalog model stays an error and auto-creates no reference data", async ({
    page,
    playwright,
  }) => {
    const ctx = await authorizedApi(playwright);
    try {
      await loginThroughUi(page);
      const names = await buildHierarchyThroughUi(page);
      const { site } = await resolveHierarchyThroughApi(ctx, names);

      const suffix = Math.random().toString(36).slice(2, 8);
      const assetTag = `E2E-NO-MODEL-${suffix}`;
      const ghostManufacturer = `E2E Ghost Co ${suffix}`;
      const ghostModel = `RM-Ghost-${suffix}`;

      const dialog = await openRackImport(page);
      const workbook = rackWorkbook([
        [
          assetTag, `E2E No Model Rack ${suffix}`, ghostManufacturer, ghostModel, "", names.siteCode,
          names.buildingCode, 7, names.roomCode, "", "", "", "", "",
        ],
      ]);
      await dialog.getByLabel("Import file").setInputFiles({
        name: "rack-no-model.xlsx",
        mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        buffer: workbook,
      });

      const [uploadResp] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === "POST" && /\/racks\/import-jobs/.test(r.url())),
        dialog.getByRole("button", { name: "Upload" }).click(),
      ]);
      const job = (await uploadResp.json()) as { id: string };

      const statusBadge = dialog.getByTestId("bulk-import-job-status");
      await expect(statusBadge).toHaveText("validated", { timeout: 15_000 });

      // The unknown model is its own row-level error (distinct from the missing-site
      // notice), naming the exact field the resolver rejected.
      const row = dialog.getByTestId("bulk-import-row").first();
      await expect(row).toContainText("model_name: No such rack model");
      await expect(row).toContainText("invalid");

      await dialog.getByRole("button", { name: "Commit import" }).click();
      await expect(statusBadge).not.toHaveText("validated", { timeout: 15_000 });

      // Committing must not create the missing reference data, and must not create a rack.
      await expect
        .poll(async () => (await findRackModel(ctx, ghostManufacturer, ghostModel)) === null, { timeout: 15_000 })
        .toBe(true);
      const racks = await apiItems<{ asset_tag: string }>(ctx, `/api/v1/racks?site_id=${site.id}&limit=200`);
      expect(racks.some((r) => r.asset_tag === assetTag)).toBe(false);

      // The job is terminal and the row (row_number 2) is invalid: never committed, and never
      // silently rewritten to a success state.
      await expect
        .poll(async () => (await jobRows(ctx, job.id)).find((r) => r.row_number === FIRST_DATA_ROW_NUMBER)?.status, {
          timeout: 15_000,
        })
        .toBe("invalid");
      expect((await jobRows(ctx, job.id)).map((r) => r.status)).toEqual(["invalid"]);
    } finally {
      await ctx.api.dispose();
    }
  });

  test("a rack import whose site_code is unknown shows an actionable location prerequisite", async ({
    page,
    playwright,
  }) => {
    const ctx = await authorizedApi(playwright);
    try {
      const { manufacturer, modelName } = await seedRackModel(ctx);

      const suffix = Math.random().toString(36).slice(2, 8);
      const missingSiteCode = `NO-SUCH-SITE-${suffix}`;

      await loginThroughUi(page);
      const dialog = await openRackImport(page);
      const workbook = rackWorkbook([
        [
          `E2E-MISSING-SITE-${suffix}`, `E2E Missing Site Rack ${suffix}`, manufacturer, modelName, "", missingSiteCode,
          "A", 1, "R1", "", "", "", "", "",
        ],
      ]);

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
    } finally {
      await ctx.api.dispose();
    }
  });
});
