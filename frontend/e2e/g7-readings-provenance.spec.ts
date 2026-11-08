import { expect, test } from "@playwright/test";

/** Browser integration with deterministic API fixtures; PostgreSQL HTTP tests
 * independently exercise the history SQL and G4-R compaction. */
test("equipment history separates contract evidence and preserves legacy display", async ({ page }) => {
  const asset = "00000000-0000-0000-0000-000000000007";
  const at = new Date().toISOString();
  const point = (id: string, value: number, provenance: string | null) => ({
    id, integration_id: asset, managed_asset_id: asset, external_identifier: "sensor",
    metric: "temperature_c", unit: "degC", presentation_unit: "degC", value,
    presentation_value: value, occurred_at: at, received_at: at, series_key: "same-series",
    registry_version: "1", provenance_status: provenance, resolution: "raw",
    mapping_revision_id: provenance === "pinned" ? "00000000-0000-0000-0000-000000000008" : null,
  });
  const points = [point("legacy1", 90, "unverified_registry"), point("legacy2", 91, "unverified_registry"),
    point("pinned1", 20, "pinned"), point("pinned2", 21, "pinned"),
    { ...point("daily", 30, null), resolution: "daily", sample_count: 4 },
    { ...point("old-client-compatible", 40, null), series_key: null }];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/v1/auth/login") body = { access_token: "fixture-token" };
    else if (path === "/api/v1/auth/me") body = { id: asset, email: "g7@example.test", full_name: "G7", role_names: [], permission_codes: [] };
    else if (path === "/api/v1/dashboard/summary") body = null;
    else if (path === "/api/v1/dashboard/exceptions") body = [];
    else if (path === "/api/v1/equipment") body = { items: [{ id: asset, hostname: "G7 fixture", asset_tag: "G7", lifecycle_status: "active", created_at: at, version: 1, placement: null }], next_cursor: null };
    else if (path === `/api/v1/equipment/${asset}`) body = { id: asset, hostname: "G7 fixture", asset_tag: "G7", lifecycle_status: "active", created_at: at, version: 1, placement: null };
    else if (path.endsWith("/ports")) body = { ports: [], power_inlets: [] };
    else if (path.endsWith("/power-summary")) body = null;
    else if (path === "/api/v1/telemetry/latest") body = [points[2]];
    else if (path === "/api/v1/telemetry/history") body = points;
    else if (path === "/api/v1/rooms" || path === "/api/v1/racks" || path === "/api/v1/alarms/history") body = { items: [], next_cursor: null };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/login");
  await page.getByRole("textbox", { name: "Email" }).fill("g7@example.test");
  await page.getByLabel("Password").fill("FixturePassw0rd!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  // Use application links so BrowserRouter owns both transitions and the in-memory token survives.
  await page.getByRole("link", { name: "Equipment" }).first().click();
  await page.getByRole("link", { name: "G7 fixture" }).click();
  await expect(page.getByRole("heading", { name: "G7 fixture" })).toBeVisible();
  const pinned = page.getByRole("region", { name: "Pinned history group" });
  const unverified = page.getByRole("region", { name: "Unverified registry history group" });
  await expect(pinned.getByRole("img", { name: "Trend from 20 to 21 degC" })).toBeVisible();
  await expect(unverified.getByRole("img", { name: "Trend from 90 to 91 degC" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Aggregate provenance pending history group" })).toBeVisible();
  await expect(page.getByText("raw · Pinned").first()).toBeVisible();
  await expect(page.getByText("daily · Aggregate provenance pending")).toBeVisible();
  await expect(page.getByRole("region", { name: "Provenance unavailable history group" })).toBeVisible();
  await expect(page.getByText("Mixed provenance or distinct series: each group is shown separately.")).toBeVisible();
  await expect(page.getByRole("img", { name: /Trend from 20 to 91/ })).toHaveCount(0);
});
