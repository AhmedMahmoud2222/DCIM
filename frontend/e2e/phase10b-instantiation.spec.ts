import { APIRequestContext, expect, test } from "@playwright/test";

/** Phase 10B E2E: catalog instantiation → rack elevation faceplate → connectivity
 * overlay, end to end through the real browser UI. Catalog/location scaffolding (the
 * manufacturer, published equipment revision with ports/PSU/graphic/markers, room, and
 * rack) is seeded directly over the API — the same prerequisite-building role
 * `tests/api/_phase2_helpers.py` plays for the backend's own HTTP tests — so this suite
 * exercises the browser only for what Phase 10B actually adds: the instantiate flow, the
 * rack elevation's faceplate overlay, and the marker detail panel's connectivity state. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

// A minimal valid 1x1 transparent PNG — small enough to embed inline, real enough for
// the backend's Pillow-based image validation (app/application/catalog_designer_service.py)
// to accept and report real width/height for.
const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

interface Seed {
  modelName: string;
  revisionId: string;
  roomId: string;
  rackId: string;
  rackName: string;
}

async function login(api: APIRequestContext): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  const body = await resp.json();
  return body.access_token as string;
}

async function seedPublishedEquipmentModel(api: APIRequestContext, token: string): Promise<Seed> {
  const headers = { Authorization: `Bearer ${token}` };
  const suffix = Math.random().toString(36).slice(2, 8);

  const manufacturer = await (
    await api.post("/api/v1/catalog/manufacturers", { headers, data: { name: `E2E Acme ${suffix}` } })
  ).json();

  const modelName = `E2E Server ${suffix}`;
  const model = await (
    await api.post("/api/v1/catalog/models", {
      headers, data: { manufacturer_id: manufacturer.id, category: "equipment", model_name: modelName },
    })
  ).json();

  let revision = await (await api.post(`/api/v1/catalog/models/${model.id}/revisions`, { headers })).json();

  const fieldsResp = await api.patch(`/api/v1/catalog/revisions/${revision.id}`, {
    headers: { ...headers, "If-Match": String(revision.version) },
    data: {
      dimension_unit: "mm", width_value: 440, height_value: 88.9, depth_value: 600, weight_unit: "kg", weight_value: 10,
      rack_unit_height: 2, supported_placement_types: ["rack_mounted"],
    },
  });
  revision = await fieldsResp.json();

  const port1Resp = await api.post(`/api/v1/catalog/revisions/${revision.id}/network-ports`, {
    headers: { ...headers, "If-Match": String(revision.version) },
    data: {
      stable_key: "eth0", display_name: "eth0", media_type: "copper", supported_speeds_mbps: [1000], connector_type: "rj45",
      side: "front", sort_order: 0,
    },
  });
  const port1 = await port1Resp.json();

  const port2Resp = await api.post(`/api/v1/catalog/revisions/${revision.id}/network-ports`, {
    headers: { ...headers, "If-Match": String(port1.revision_version) },
    data: {
      stable_key: "eth1", display_name: "eth1", media_type: "copper", supported_speeds_mbps: [1000], connector_type: "rj45",
      side: "front", sort_order: 1,
    },
  });
  const port2 = await port2Resp.json();

  const psuResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/power-supplies`, {
    headers: { ...headers, "If-Match": String(port2.revision_version) },
    data: { stable_key: "psu1", label: "PSU 1", quantity: 1, connector_type: "C14" },
  });
  const psu = await psuResp.json();

  const graphicResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/front`, {
    headers: { ...headers, "If-Match": String(psu.revision_version) },
    multipart: { file: { name: "front.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG } },
  });
  expect(graphicResp.ok(), `graphic upload failed: ${graphicResp.status()} ${await graphicResp.text()}`).toBeTruthy();
  const graphic = await graphicResp.json();

  const marker1Resp = await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/${graphic.id}/markers`, {
    headers: { ...headers, "If-Match": String(graphic.revision_version) },
    data: { marker_type: "network_port", network_port_template_id: port1.id, marker_x: 0.2, marker_y: 0.5, label: "eth0" },
  });
  const marker1 = await marker1Resp.json();

  await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/${graphic.id}/markers`, {
    headers: { ...headers, "If-Match": String(marker1.revision_version) },
    data: { marker_type: "network_port", network_port_template_id: port2.id, marker_x: 0.4, marker_y: 0.5, label: "eth1" },
  });

  const publishResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/publish`, { headers });
  expect(publishResp.ok(), `publish failed: ${publishResp.status()} ${await publishResp.text()}`).toBeTruthy();
  const published = await publishResp.json();

  // Location chain (mirrors tests/api/_phase2_helpers.py's create_room).
  const org = await (await api.post("/api/v1/organizations", { headers, data: { name: `E2E Org ${suffix}` } })).json();
  const country = await (
    await api.post("/api/v1/countries", { headers, data: { organization_id: org.id, name: "E2E Land", iso_code: "EL" } })
  ).json();
  const city = await (await api.post("/api/v1/cities", { headers, data: { country_id: country.id, name: "E2E City" } })).json();
  const site = await (
    await api.post("/api/v1/sites", { headers, data: { city_id: city.id, code: `E2E-${suffix}`, name: "E2E Site" } })
  ).json();
  const building = await (
    await api.post("/api/v1/buildings", { headers, data: { site_id: site.id, code: "A", name: "Building A" } })
  ).json();
  const floor = await (
    await api.post("/api/v1/floors", { headers, data: { building_id: building.id, name: "Floor 1", level_number: 1 } })
  ).json();
  const room = await (
    await api.post("/api/v1/rooms", { headers, data: { floor_id: floor.id, code: `R-${suffix}`, name: "E2E Room" } })
  ).json();

  const rackModel = await (
    await api.post("/api/v1/rack-models", { headers, data: { manufacturer: "E2E Rack Co", model_name: `RM-${suffix}` } })
  ).json();
  const rackRevision = await (
    await api.post(`/api/v1/rack-models/${rackModel.id}/revisions`, { headers, data: { height_u: 10, width_mm: 600, depth_mm: 1000 } })
  ).json();
  const rackName = `E2E Rack ${suffix}`;
  const rack = await (
    await api.post("/api/v1/racks", {
      headers, data: { asset_tag: `RACK-E2E-${suffix}`, model_revision_id: rackRevision.id, name: rackName, room_id: room.id },
    })
  ).json();

  return { modelName, revisionId: published.id, roomId: room.id, rackId: rack.id, rackName };
}

test.describe("Phase 10B: catalog instantiation and rack elevation", () => {
  let seed: Seed;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: process.env.BASE_URL ?? "http://127.0.0.1:5173" });
    const token = await login(api);
    seed = await seedPublishedEquipmentModel(api, token);
    await api.dispose();
  });

  test("instantiate a 2U server into a rack, then verify ports and the faceplate overlay", async ({ page }) => {
    await page.goto("/login");
    // LoginPage.tsx's <label> elements aren't programmatically associated with their
    // inputs (no htmlFor/id), so getByLabel can't find them — target by input type
    // instead, matching the markup as it actually is.
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // --- 1. Select the published catalog model and instantiate a 2U server in a rack.
    // Client-side navigation throughout (nav links / in-app buttons), never page.goto or
    // page.reload after this point: the access token lives only in memory
    // (src/lib/authStore.ts) and a real page load has no bootstrap-session-from-cookie
    // step, so a hard navigation would silently bounce back to /login.
    await page.getByRole("link", { name: "Equipment", exact: true }).click();
    await page.getByRole("link", { name: "Instantiate from catalog" }).click();
    await expect(page).toHaveURL("/equipment/instantiate");
    await page.getByTestId("instantiate-model-select").selectOption({ label: `${seed.modelName} ` });
    await page.getByTestId("instantiate-revision-select").selectOption({ index: 1 });
    const assetTag = `E2E-SRV-${Math.random().toString(36).slice(2, 8)}`;
    await page.getByTestId("instantiate-asset-tag").fill(assetTag);
    await page.getByTestId("instantiate-wants-placement").check();
    await page.getByTestId("instantiate-room-select").selectOption(seed.roomId);
    await page.getByTestId("instantiate-rack-select").selectOption(seed.rackId);
    await page.getByTestId("instantiate-u-start").fill("1");
    await page.getByTestId("instantiate-u-end").fill("3");
    await page.getByTestId("instantiate-side-select").selectOption("front");
    await page.getByTestId("instantiate-submit").click();

    await expect(page).toHaveURL(/\/equipment\/[0-9a-f-]+$/, { timeout: 10_000 });

    // --- 2. Verify automatic generation of matching port instances.
    await expect(page.getByRole("heading", { name: "Ports & cabling" })).toBeVisible();
    await expect(page.getByTestId("equipment-port-row")).toHaveCount(2);
    const eth0Row = page.getByTestId("equipment-port-row").filter({ hasText: "eth0" });
    await expect(eth0Row).toContainText("eth0");
    await expect(eth0Row).toContainText("Unassigned");

    // A second instantiated equipment stands in for a patch panel with a real port ID to
    // cable to — created over the API purely as setup, same as the room/rack/model
    // scaffolding above.
    const api = await page.request;
    const token = await login(api);
    const headers = { Authorization: `Bearer ${token}` };
    const patchPanel = await (
      await api.post("/api/v1/equipment/instantiate", {
        headers, data: { asset_tag: `E2E-PP-${Math.random().toString(36).slice(2, 8)}`, catalog_model_revision_id: seed.revisionId },
      })
    ).json();

    // Cable eth0 through the real Ports & cabling UI, right here on the equipment detail
    // page — the actual cabling-mutation surface Phase 10B adds. Its success handler
    // invalidates the same ["equipment", id, "ports"] query the rack elevation's
    // faceplate overlay reads below, via the shared QueryClient (App.tsx), so the marker
    // there reflects it without a page reload — never used past this point, since a hard
    // navigation would drop the in-memory access token (src/lib/authStore.ts) with no
    // session-restore-on-load to recover it.
    await eth0Row.getByRole("button", { name: "Connect" }).click();
    await eth0Row.getByPlaceholder("Target EquipmentPort ID").fill(patchPanel.ports[0].id);
    await eth0Row.getByPlaceholder("Cable ID (optional)").fill("E2E-CBL-1");
    await eth0Row.getByRole("button", { name: "Save" }).click();
    await expect(eth0Row).toContainText("E2E-CBL-1");

    // --- 3. View the rack elevation with the faceplate image and click a marker to
    // verify connectivity state.
    await page.getByRole("link", { name: "Racks", exact: true }).click();
    await page.getByRole("link", { name: seed.rackName }).click();
    await expect(page).toHaveURL(`/racks/${seed.rackId}`);
    const overlay = page.getByTestId("faceplate-overlay").first();
    await expect(overlay).toBeVisible({ timeout: 10_000 });
    await expect(overlay.locator("img")).toBeVisible();

    const markers = page.getByTestId("faceplate-marker");
    await expect(markers).toHaveCount(2);
    await page.getByRole("button", { name: /^Port eth0:/ }).click();
    await expect(page.getByTestId("marker-detail-panel")).toBeVisible();
    await expect(page.getByTestId("marker-detail-status")).toHaveText("connected");
    await expect(page.getByTestId("marker-detail-panel")).toContainText("E2E-CBL-1");
  });
});
