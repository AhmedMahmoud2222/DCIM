import { APIRequestContext, expect, test } from "@playwright/test";

import { buildXlsxWorkbook } from "./fixtures/xlsxBuilder";

/** Bulk import E2E: the rack XLSX import pipeline end to end through the real browser UI
 * — download the template, upload a small valid workbook, watch it validate, commit it,
 * and download the results report. Location/rack-model scaffolding referenced by the
 * uploaded row is seeded directly over the API, the same prerequisite-building role
 * phase10b-instantiation.spec.ts's `seedPublishedEquipmentModel` plays. This is the
 * first spec using `page.setInputFiles` with an in-memory buffer — there's no checked-in
 * .xlsx fixture in the repo and no xlsx-writing library in this frontend's dependencies
 * (see package.json), so `./fixtures/xlsxBuilder.ts` builds the tiny workbook by hand;
 * see its module docstring for why that's a valid approach here. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

interface Seed {
  suffix: string;
  siteCode: string;
  buildingCode: string;
  floorLevel: number;
  roomCode: string;
  manufacturer: string;
  modelName: string;
}

async function login(api: APIRequestContext): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  const body = await resp.json();
  return body.access_token as string;
}

// Mirrors phase10b-instantiation.spec.ts's location-chain seeding (Org → Country → City
// → Site → Building → Floor → Room) plus a legacy RackModel/RackModelRevision — the same
// pair the rack import validator resolves by (manufacturer, model_name) via
// app/application/bulk_import/resolvers.py's `resolve_rack_model_revision`.
async function seedRackImportPrerequisites(api: APIRequestContext, token: string): Promise<Seed> {
  const headers = { Authorization: `Bearer ${token}` };
  const suffix = Math.random().toString(36).slice(2, 8);

  const org = await (await api.post("/api/v1/organizations", { headers, data: { name: `E2E Import Org ${suffix}` } })).json();
  const country = await (
    await api.post("/api/v1/countries", { headers, data: { organization_id: org.id, name: "E2E Import Land", iso_code: "EI" } })
  ).json();
  const city = await (await api.post("/api/v1/cities", { headers, data: { country_id: country.id, name: "E2E Import City" } })).json();
  const siteCode = `E2EI-${suffix}`;
  const site = await (
    await api.post("/api/v1/sites", { headers, data: { city_id: city.id, code: siteCode, name: "E2E Import Site" } })
  ).json();
  const buildingCode = "A";
  const building = await (
    await api.post("/api/v1/buildings", { headers, data: { site_id: site.id, code: buildingCode, name: "Building A" } })
  ).json();
  const floorLevel = 1;
  const floor = await (
    await api.post("/api/v1/floors", { headers, data: { building_id: building.id, name: "Floor 1", level_number: floorLevel } })
  ).json();
  const roomCode = `R-${suffix}`;
  await api.post("/api/v1/rooms", { headers, data: { floor_id: floor.id, code: roomCode, name: "E2E Import Room" } });

  const manufacturer = `E2E Import Co ${suffix}`;
  const modelName = `RM-Import-${suffix}`;
  const rackModel = await (
    await api.post("/api/v1/rack-models", { headers, data: { manufacturer, model_name: modelName } })
  ).json();
  await api.post(`/api/v1/rack-models/${rackModel.id}/revisions`, {
    headers, data: { height_u: 10, width_mm: 600, depth_mm: 1000 },
  });

  return { suffix, siteCode, buildingCode, floorLevel, roomCode, manufacturer, modelName };
}

test.describe("Bulk import: racks", () => {
  let seed: Seed;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: process.env.BASE_URL ?? "http://127.0.0.1:5173" });
    const token = await login(api);
    seed = await seedRackImportPrerequisites(api, token);
    await api.dispose();
  });

  test("download the template, upload a valid row, validate, commit, and download the report", async ({ page }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // Client-side navigation throughout, same discipline as phase10b-instantiation.spec.ts:
    // the access token lives only in memory (src/lib/authStore.ts), so a hard navigation
    // would silently bounce back to /login.
    await page.getByRole("link", { name: "Racks", exact: true }).click();
    await expect(page).toHaveURL("/racks");

    await page.getByRole("button", { name: "Bulk Import" }).click();
    const dialog = page.getByRole("dialog", { name: "Bulk import Rack" });
    await expect(dialog).toBeVisible();

    const [templateDownload] = await Promise.all([
      page.waitForEvent("download"),
      dialog.getByRole("button", { name: "Download template" }).click(),
    ]);
    expect(templateDownload.suggestedFilename()).toBe("rack-import-template.xlsx");

    const assetTag = `E2E-IMPORT-RACK-${seed.suffix}`;
    const rackName = `E2E Import Rack ${seed.suffix}`;
    const workbook = buildXlsxWorkbook(
      "Racks",
      [
        "asset_tag", "rack_name", "manufacturer", "model_name", "revision_number", "site_code", "building_code",
        "floor_level", "room_code", "x_mm", "y_mm", "rotation_deg", "owner", "notes",
      ],
      [
        [
          assetTag, rackName, seed.manufacturer, seed.modelName, "", seed.siteCode, seed.buildingCode, seed.floorLevel,
          seed.roomCode, "", "", "", "", "",
        ],
      ],
    );

    await dialog.getByLabel("Import file").setInputFiles({
      name: "rack-import.xlsx",
      mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: workbook,
    });
    await expect(dialog.getByRole("radio", { name: "Create only" })).toBeChecked();
    await dialog.getByRole("button", { name: "Upload" }).click();

    const statusBadge = dialog.getByTestId("bulk-import-job-status");
    await expect(statusBadge).toHaveText("validated", { timeout: 15_000 });
    await expect(dialog.getByRole("cell", { name: "valid" })).toBeVisible();

    await dialog.getByRole("button", { name: "Commit import" }).click();
    await expect(statusBadge).toHaveText("committed", { timeout: 15_000 });

    const [reportDownload] = await Promise.all([
      page.waitForEvent("download"),
      dialog.getByRole("button", { name: "Download results report" }).click(),
    ]);
    expect(reportDownload.suggestedFilename()).toMatch(/^bulk-import-report-.+\.xlsx$/);

    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).not.toBeVisible();

    await expect(page.getByRole("link", { name: rackName })).toBeVisible();
  });
});
