import { APIRequestContext, expect, test } from "@playwright/test";

/** Issue #102 E2E: the operator workflow in the existing power UI. Seeds a dual-fed server behind two
 * UPSs and two breakers over the API, then drives the browser: read capacity and data quality, trip a
 * breaker and see the redundancy change, read the forecast status, queue a report on the `reports`
 * Celery queue, and download it once it is ready. The load is a nameplate estimate because the suite
 * has no telemetry collector; the page must say so. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

async function seed(api: APIRequestContext) {
  const login = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(login.ok()).toBeTruthy();
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const sfx = Math.random().toString(36).slice(2, 8);
  const post = async (path: string, data: unknown, extra: Record<string, string> = {}) => {
    const r = await api.post(`/api/v1${path}`, { headers: { ...headers, ...extra }, data });
    expect(r.ok(), `${path}: ${r.status()} ${await r.text()}`).toBeTruthy();
    return r.json();
  };
  const org = await post("/organizations", { name: `E2E-102 Org ${sfx}` });
  const country = await post("/countries", { organization_id: org.id, name: "Testland", iso_code: "TL" });
  const city = await post("/cities", { country_id: country.id, name: "Testville" });
  const siteName = `E2E-102 Site ${sfx}`;
  const site = await post("/sites", { city_id: city.id, code: `E${sfx}`.slice(0, 10), name: siteName });
  const building = await post("/buildings", { site_id: site.id, code: "A", name: "B" });
  const floor = await post("/floors", { building_id: building.id, name: "F", level_number: 1 });
  const room = await post("/rooms", { floor_id: floor.id, code: `R${sfx}`.slice(0, 10), name: "Room" });

  const model = await post("/equipment-models", { manufacturer: "Acme", model_name: `EM-${sfx}` });
  const revision = await post(`/equipment-models/${model.id}/revisions`, {});
  const equipment = await post("/equipment", { asset_tag: `SRV-${sfx}`, model_revision_id: revision.id, hostname: `srv-${sfx}` });

  const breakers: Record<string, { id: string }> = {};
  for (const side of ["A", "B"]) {
    const ups = await post("/power/upses", { asset_tag: `UPS-${side}-${sfx}`, name: `UPS-${side}`, room_id: room.id, capacity_kva: 50 });
    await api.put(`/api/v1/power/nodes/${ups.id}/capacity`, { headers, data: { rated_capacity_kw: 20 } });
    const brk = await post("/power/protection-devices", {
      housing_asset_id: ups.managed_asset_id, site_id: site.id, label: `BRK-${side}`, rating_a: 32, voltage_v: 230,
      poles: 1, phase_config: "single",
    });
    breakers[side] = brk;
    const feed = await post("/power/equipment-feeds", { equipment_asset_id: equipment.id, label: `Feed ${side}` });
    await api.put(`/api/v1/power/nodes/${feed.id}/capacity`, { headers, data: { rated_capacity_kw: 4 } });
    await post("/power/connections", { source_node_id: ups.id, target_node_id: brk.id, connection_type: "feed", feed_label: side });
    await post("/power/connections", { source_node_id: brk.id, target_node_id: feed.id, connection_type: "feed", feed_label: side });
  }
  return { siteName };
}

test("operator reads capacity, trips a breaker, reads the forecast and downloads a report", async ({ page, request }) => {
  const { siteName } = await seed(request);

  await page.goto("/login");
  await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL("/");
  await page.getByRole("link", { name: "Power Topology", exact: true }).click();
  await page.getByRole("link", { name: "Open capacity analytics" }).click();
  await expect(page.getByRole("heading", { name: "Power capacity and forecast" })).toBeVisible();
  await page.getByLabel("Site").selectOption({ label: siteName });

  // Dual-fed demand is counted once (4 kW, the larger feed), split over both UPSs, and labelled as an estimate.
  await expect(page.getByTestId("site-load")).toHaveText("4.00 kW");
  const nodes = page.getByRole("table", { name: "Power nodes" });
  await expect(nodes.getByRole("row", { name: /UPS-A/ })).toContainText("2.00 kW");
  await expect(nodes.getByRole("row", { name: /UPS-A/ })).toContainText("Estimated");
  const redundancy = page.getByRole("table", { name: "Equipment redundancy" });
  await expect(redundancy.getByRole("row", { name: /SRV-/ })).toContainText("normal dual feed");

  // Trip breaker A: the survivor carries the full load and the warning appears.
  await page.getByRole("tab", { name: "Protection" }).click();
  await page.getByLabel("State for BRK-A").selectOption("tripped");
  await expect(page.getByTestId("state-BRK-A")).toHaveText("tripped");
  await page.getByRole("tab", { name: "Capacity" }).click();
  await expect(redundancy.getByRole("row", { name: /SRV-/ })).toContainText("one feed failed");
  await expect(page.getByText(/redundancy lost/)).toBeVisible();
  await expect(nodes.getByRole("row", { name: /UPS-B/ })).toContainText("4.00 kW");
  await expect(page.getByTestId("site-load")).toHaveText("4.00 kW");

  // No history exists yet, so the forecast must decline and say why.
  await page.getByRole("tab", { name: "History and forecast" }).click();
  await expect(page.getByTestId("forecast-status")).toHaveText("No data");
  await expect(page.getByText(/Reason: no utilization snapshots/)).toBeVisible();

  // Report: queued on the reports queue, picked up by the worker, then downloaded.
  await page.getByRole("tab", { name: "Reports" }).click();
  await page.getByRole("button", { name: "Generate report" }).click();
  const download = page.getByRole("button", { name: "Download CSV" }).first();
  await expect(download).toBeVisible({ timeout: 60_000 });
  const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
  expect(file.suggestedFilename()).toMatch(/^power-report-.*\.csv$/);
});
