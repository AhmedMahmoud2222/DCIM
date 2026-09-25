import { APIRequestContext, expect, test } from "@playwright/test";

/** Phase 10C E2E: live telemetry overlays on the rack elevation, and the "what-if"
 * failure-impact simulator, end to end through the real browser UI. Catalog/location/
 * power scaffolding is seeded directly over the API (this repo's established pattern —
 * see phase10b-instantiation.spec.ts's own docstring) so this suite exercises the
 * browser only for what Phase 10C actually adds: the live link/power status rendered on
 * faceplate markers, and the ImpactAnalysisModal's blast-radius simulation with its
 * rack-elevation highlighting. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

interface Seed {
  revisionId: string;
  roomId: string;
  rackId: string;
  rackName: string;
  networkPortTemplateId: string;
  powerSupplyTemplateId: string;
}

async function login(api: APIRequestContext): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  const body = await resp.json();
  return body.access_token as string;
}

async function seedPublishedModelAndRack(api: APIRequestContext, token: string): Promise<Seed> {
  const headers = { Authorization: `Bearer ${token}` };
  const suffix = Math.random().toString(36).slice(2, 8);

  const manufacturer = await (
    await api.post("/api/v1/catalog/manufacturers", { headers, data: { name: `E2E-10C Acme ${suffix}` } })
  ).json();
  const model = await (
    await api.post("/api/v1/catalog/models", {
      headers, data: { manufacturer_id: manufacturer.id, category: "equipment", model_name: `E2E-10C Server ${suffix}` },
    })
  ).json();

  let revision = await (await api.post(`/api/v1/catalog/models/${model.id}/revisions`, { headers })).json();
  const fieldsResp = await api.patch(`/api/v1/catalog/revisions/${revision.id}`, {
    headers: { ...headers, "If-Match": String(revision.version) },
    data: {
      dimension_unit: "mm", width_value: 440, height_value: 44.45, depth_value: 600, weight_unit: "kg", weight_value: 8,
      rack_unit_height: 1, supported_placement_types: ["rack_mounted"],
    },
  });
  revision = await fieldsResp.json();

  const portResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/network-ports`, {
    headers: { ...headers, "If-Match": String(revision.version) },
    data: {
      stable_key: "eth0", display_name: "eth0", media_type: "copper", supported_speeds_mbps: [1000], connector_type: "rj45",
      side: "front", sort_order: 0,
    },
  });
  const port = await portResp.json();

  const psuResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/power-supplies`, {
    headers: { ...headers, "If-Match": String(port.revision_version) },
    data: { stable_key: "psu1", label: "PSU 1", quantity: 1, connector_type: "C14" },
  });
  const psu = await psuResp.json();

  const graphicResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/front`, {
    headers: { ...headers, "If-Match": String(psu.revision_version) },
    multipart: { file: { name: "front.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG } },
  });
  const graphic = await graphicResp.json();

  const portMarkerResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/${graphic.id}/markers`, {
    headers: { ...headers, "If-Match": String(graphic.revision_version) },
    data: { marker_type: "network_port", network_port_template_id: port.id, marker_x: 0.2, marker_y: 0.5, label: "eth0" },
  });
  const portMarker = await portMarkerResp.json();

  await api.post(`/api/v1/catalog/revisions/${revision.id}/graphics/${graphic.id}/markers`, {
    headers: { ...headers, "If-Match": String(portMarker.revision_version) },
    data: { marker_type: "power_supply", power_supply_template_id: psu.id, marker_x: 0.8, marker_y: 0.5, label: "PSU 1" },
  });

  const publishResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/publish`, { headers });
  const published = await publishResp.json();

  const org = await (await api.post("/api/v1/organizations", { headers, data: { name: `E2E-10C Org ${suffix}` } })).json();
  const country = await (
    await api.post("/api/v1/countries", { headers, data: { organization_id: org.id, name: "E2E Land", iso_code: "EL" } })
  ).json();
  const city = await (await api.post("/api/v1/cities", { headers, data: { country_id: country.id, name: "E2E City" } })).json();
  const site = await (
    await api.post("/api/v1/sites", { headers, data: { city_id: city.id, code: `E2E10C-${suffix}`, name: "E2E Site" } })
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
    await api.post("/api/v1/rack-models", { headers, data: { manufacturer: "E2E-10C Rack Co", model_name: `RM-${suffix}` } })
  ).json();
  const rackRevision = await (
    await api.post(`/api/v1/rack-models/${rackModel.id}/revisions`, { headers, data: { height_u: 10, width_mm: 600, depth_mm: 1000 } })
  ).json();
  const rackName = `E2E-10C Rack ${suffix}`;
  const rack = await (
    await api.post("/api/v1/racks", {
      headers, data: { asset_tag: `RACK-E2E10C-${suffix}`, model_revision_id: rackRevision.id, name: rackName, room_id: room.id },
    })
  ).json();

  return {
    revisionId: published.id, roomId: room.id, rackId: rack.id, rackName,
    networkPortTemplateId: port.id, powerSupplyTemplateId: psu.id,
  };
}

test.describe("Phase 10C: live telemetry overlays and failure-impact simulation", () => {
  let seed: Seed;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: process.env.BASE_URL ?? "http://127.0.0.1:5173" });
    const token = await login(api);
    seed = await seedPublishedModelAndRack(api, token);
    await api.dispose();
  });

  test("shows live telemetry on rack elevation markers, then simulates a PDU outlet trip", async ({ page }) => {
    const api = page.request;
    const token = await login(api);
    const headers = { Authorization: `Bearer ${token}` };

    // --- 1. Instantiate a single-corded server into the rack over the API (Phase 10B's
    // own instantiate flow is exercised end to end by phase10b-instantiation.spec.ts;
    // this suite's browser coverage is the telemetry overlay and impact simulator).
    const server = await (
      await api.post("/api/v1/equipment/instantiate", {
        headers,
        data: {
          asset_tag: `E2E10C-SRV-${Math.random().toString(36).slice(2, 8)}`, catalog_model_revision_id: seed.revisionId,
          hostname: "e2e10c-srv-1", placement_type: "rack_mounted", room_id: seed.roomId, rack_id: seed.rackId,
          u_start: 1, u_end: 2, side: "front",
        },
      })
    ).json();
    const networkPort = server.ports[0];
    const powerInlet = server.power_inlets[0];

    // --- 2. Bind the network port to a telemetry source and ingest a healthy reading.
    const binding = await (
      await api.post("/api/v1/telemetry/bindings", {
        headers,
        data: {
          equipment_id: server.id, target_type: "network_port", equipment_port_id: networkPort.id,
          protocol: "snmp", external_ref: "1.3.6.1.2.1.2.2.1.8.1", label: "eth0 link",
        },
      })
    ).json();
    await api.post("/api/v1/telemetry/port-status/ingest", {
      headers,
      data: {
        binding_id: binding.id, sampled_at: new Date().toISOString(),
        payload: { link_state: "UP", bandwidth_util_pct: 12, error_rate_pct: 0 },
      },
    });

    // --- 3. Wire up power: a PDU outlet feeding the server's single power inlet.
    const pdu = await (
      await api.post("/api/v1/power/pdus", { headers, data: { asset_tag: `E2E10C-PDU-${Math.random().toString(36).slice(2, 8)}`, name: "PDU-1" } })
    ).json();
    const outlet = await (
      await api.post("/api/v1/power/pdu-outlets", { headers, data: { pdu_asset_id: pdu.managed_asset_id, outlet_number: 1 } })
    ).json();
    await api.post("/api/v1/power/connections", {
      headers, data: { source_node_id: outlet.id, target_node_id: powerInlet.power_node_id, feed_label: "single" },
    });

    // --- 4. Log in through the browser and open the rack elevation.
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    await page.getByRole("link", { name: "Racks", exact: true }).click();
    await page.getByRole("link", { name: seed.rackName }).click();
    await expect(page).toHaveURL(`/racks/${seed.rackId}`);

    const overlay = page.getByTestId("faceplate-overlay").first();
    await expect(overlay).toBeVisible({ timeout: 10_000 });

    // The network port marker's live telemetry ring reflects the ingested UP reading.
    const portMarker = page.locator('[data-testid="faceplate-marker"][aria-label^="Port eth0"]').first();
    await expect
      .poll(async () => portMarker.getAttribute("data-telemetry-status"), { timeout: 10_000 })
      .toBe("UP");
    await expect(page.getByTestId("telemetry-alert-halo")).toHaveCount(0);

    // --- 5. Simulate the PDU outlet tripping from the marker's own detail panel.
    await page.locator('[data-testid="faceplate-marker"][aria-label^="Power PSU 1"]').first().click();
    await expect(page.getByTestId("marker-detail-panel")).toBeVisible();
    await page.getByTestId("simulate-failure-button").click();

    const modal = page.getByTestId("impact-analysis-modal");
    await expect(modal).toBeVisible();
    await expect(modal.getByTestId("impact-item")).toHaveCount(1, { timeout: 10_000 });
    await expect(modal.getByTestId("impact-item")).toContainText("power loss");
    await expect(modal.getByTestId("impact-item")).toContainText("e2e10c-srv-1");

    // The rack elevation highlights the affected slot in red while the simulation result
    // is current.
    await expect(page.getByTestId("impact-highlight").first()).toHaveAttribute("data-impact-severity", "direct");

    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });
});
