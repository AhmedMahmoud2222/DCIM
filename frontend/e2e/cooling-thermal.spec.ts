import { APIRequestContext, Page, expect, test } from "@playwright/test";
import { createHmac, randomUUID } from "node:crypto";

/** Issue #105 E2E, against the real backend, the real Celery worker and the real sandboxed DXF parser:
 *
 *   create/configure cooling assets -> place sensors -> ingest deterministic environmental telemetry through the
 *   signed collector endpoint -> define aisle/containment -> render temperature/humidity maps -> verify measured vs
 *   interpolated -> verify stale/missing -> verify airflow provenance -> verify capacity/headroom -> verify 2D/3D.
 *
 * The room's calibrated floor plan is produced by the same DXF import used in the #104 suite (driven through the API
 * so this spec stays about cooling). No external BMS hardware or SaaS is involved; every value is deterministic. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

const lwpoly = (handle: string, layer: string, pts: [number, number][]) =>
  `0\nLWPOLYLINE\n5\n${handle}\n8\n${layer}\n90\n${pts.length}\n70\n1\n${pts.map(([x, y]) => `10\n${x}\n20\n${y}\n`).join("")}`;

function hallDxf(): Buffer {
  const entities = lwpoly("100", "WALLS", [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]);
  return Buffer.from(`0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n${entities}0\nENDSEC\n0\nEOF\n`);
}

type Api = { post: (path: string, data: unknown, extra?: Record<string, string>) => Promise<any>; get: (path: string) => Promise<any>; patch: (path: string, data: unknown, version: number) => Promise<any>; put: (path: string, data: unknown) => Promise<any>; headers: Record<string, string> };

async function connect(request: APIRequestContext): Promise<Api> {
  const login = await request.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(login.ok()).toBeTruthy();
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const check = async (r: Awaited<ReturnType<APIRequestContext["post"]>>, what: string) => {
    expect(r.ok(), `${what}: ${r.status()} ${await r.text()}`).toBeTruthy();
    return r.status() === 204 ? null : r.json();
  };
  return {
    headers,
    post: async (path, data, extra = {}) => check(await request.post(`/api/v1${path}`, { headers: { ...headers, ...extra }, data }), `POST ${path}`),
    get: async (path) => check(await request.get(`/api/v1${path}`, { headers }), `GET ${path}`),
    patch: async (path, data, version) => check(await request.patch(`/api/v1${path}`, { headers: { ...headers, "If-Match": String(version) }, data }), `PATCH ${path}`),
    put: async (path, data) => check(await request.put(`/api/v1${path}`, { headers, data }), `PUT ${path}`),
  };
}

async function calibratedRoom(request: APIRequestContext, api: Api, sfx: string) {
  const org = await api.post("/organizations", { name: `E2E-105 Org ${sfx}` });
  const country = await api.post("/countries", { organization_id: org.id, name: "Testland", iso_code: "TL" });
  const city = await api.post("/cities", { country_id: country.id, name: "Testville" });
  const site = await api.post("/sites", { city_id: city.id, code: `C${sfx}`.slice(0, 10), name: `E2E-105 Site ${sfx}` });
  const building = await api.post("/buildings", { site_id: site.id, code: "A", name: "B" });
  const floor = await api.post("/floors", { building_id: building.id, name: "F", level_number: 1 });
  const roomName = `E2E-105 Hall ${sfx}`;
  const room = await api.post("/rooms", { floor_id: floor.id, code: `H${sfx}`.slice(0, 10), name: roomName });

  const plan = await api.post("/floor-plans", { room_id: room.id });
  const upload = await request.post(`/api/v1/floor-plans/${plan.id}/upload`, {
    headers: api.headers, multipart: { file: { name: "hall.dxf", mimeType: "application/octet-stream", buffer: hallDxf() } },
  });
  expect(upload.ok(), await upload.text()).toBeTruthy();
  const job = await upload.json();
  await expect.poll(async () => (await api.get(`/floor-plans/import-jobs/${job.id}`)).status, { timeout: 60_000 }).toBe("parsed");
  const fresh = async () => api.get(`/floor-plans/${plan.id}`);
  await api.post(`/floor-plans/${plan.id}/calibration`, { method: "declared_units", job_id: job.id }, { "If-Match": String((await fresh()).version) });
  const candidates = (await api.get(`/floor-plans/import-jobs/${job.id}/candidates?limit=200`)).items;
  // a lone rectangle is ambiguous to the classifier: the operator classifies it as the room outline while accepting it
  const outline = candidates[0];
  expect(outline, "the DXF must yield a candidate for the room outline").toBeTruthy();
  await api.post(`/floor-plans/import-jobs/${job.id}/candidates/${outline.id}/accept`, { object_type: "room_outline" }, { "If-Match": String(outline.version) });
  await api.post(`/floor-plans/${plan.id}/activate`, {}, { "If-Match": String((await fresh()).version) });
  return { site, room, roomName };
}

/** Signed telemetry through the real collector endpoint (HMAC-SHA256 over `id.timestamp.nonce.` + raw body). */
async function collectorFor(request: APIRequestContext, api: Api, sfx: string) {
  const collector = await api.post("/collectors", { name: `e2e-105-${sfx}`, collector_type: "central" });
  await api.post(`/collectors/${collector.id}/capabilities`, { protocol_codes: ["icmp"] });
  const integration = await api.post("/integrations", { name: `e2e-105-int-${sfx}`, integration_type: "icmp", target_host: "127.0.0.1", poll_interval_seconds: 60 });
  await api.post(`/collectors/${collector.id}/assignments`, { integration_id: integration.id });
  let mappingCount = 0;
  return {
    map: async (assetId: string, metric: string, unit: string) => {
      mappingCount += 1;
      const source = `src-${mappingCount}`;
      await api.post("/telemetry/mappings", { integration_id: integration.id, managed_asset_id: assetId, source_identifier: source, canonical_metric: metric, unit, scale: 1 });
      return source;
    },
    send: async (readings: { source: string; value: number; ageSeconds: number }[]) => {
      const records = readings.map((r) => ({
        dedup_key: randomUUID(), integration_id: integration.id, external_identifier: r.source, source_identifier: r.source,
        occurred_at: new Date(Date.now() - r.ageSeconds * 1000).toISOString(), value: r.value,
      }));
      const raw = JSON.stringify({ records });
      const ts = String(Math.floor(Date.now() / 1000));
      const nonce = randomUUID().replace(/-/g, "");
      const signature = createHmac("sha256", collector.secret).update(`${collector.id}.${ts}.${nonce}.`).update(raw).digest("hex");
      const response = await request.post(`/api/v1/collectors/${collector.id}/telemetry`, {
        headers: { "Content-Type": "application/json", "X-Collector-Id": collector.id, "X-Collector-Timestamp": ts, "X-Collector-Nonce": nonce, "X-Collector-Signature": signature },
        data: Buffer.from(raw),
      });
      expect(response.ok(), await response.text()).toBeTruthy();
      expect((await response.json()).results.map((r: any) => r.status)).toEqual(readings.map(() => "accepted"));
    },
  };
}

async function login(page: Page) {
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}

const shot = async (page: Page, name: string) => {
  if (process.env.E2E_SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.E2E_SCREENSHOT_DIR}/${name}.png`, fullPage: true });
};

test("cooling assets -> sensors -> telemetry -> aisles -> maps -> measured vs interpolated -> stale/missing -> airflow -> capacity -> 2D/3D", async ({ page, request }) => {
  test.setTimeout(150_000); // seeds a calibrated plan through the real import worker, then walks the whole workflow
  const api = await connect(request);
  const sfx = Math.random().toString(36).slice(2, 8);
  const { site, room, roomName } = await calibratedRoom(request, api, sfx);

  // ---------------------------------------------------------------- an IT load the capacity view can use (a dual-fed server, as in #104)
  const rackModel = await api.post("/rack-models", { manufacturer: "Acme", model_name: `RM-${sfx}` });
  const rackRevision = await api.post(`/rack-models/${rackModel.id}/revisions`, { height_u: 42, width_mm: 600, depth_mm: 1000 });
  const rack = await api.post("/racks", { asset_tag: `RKA-${sfx}`, model_revision_id: rackRevision.id, name: "E2E Rack A", room_id: room.id, x_mm: 2500, y_mm: 1500, rotation_deg: 0 });
  const eqModel = await api.post("/equipment-models", { manufacturer: "Acme", model_name: `EM-${sfx}` });
  const eqRevision = await api.post(`/equipment-models/${eqModel.id}/revisions`, {});
  const server = await api.post("/equipment", { asset_tag: `SRV-${sfx}`, model_revision_id: eqRevision.id, hostname: `srv-${sfx}` });
  await api.post(`/equipment/${server.id}/move`, { placement_type: "rack_mounted", room_id: room.id, rack_id: rack.id, u_start: 1, u_end: 3, side: "front" });
  const ups = await api.post("/power/upses", { asset_tag: `UPS-${sfx}`, name: "UPS", room_id: room.id, capacity_kva: 50 });
  const feed = await api.post("/power/equipment-feeds", { equipment_asset_id: server.id, label: "Feed A" });
  await api.post("/power/connections", { source_node_id: ups.id, target_node_id: feed.id, connection_type: "feed", feed_label: "single" });

  // ---------------------------------------------------------------- cooling assets: group, two CRAHs, a served zone, aisles with containment
  const group = await api.post("/cooling/groups", { site_id: site.id, name: `Row-A ${sfx}` });
  const makeUnit = async (name: string, extra: Record<string, unknown>, x: number, y: number) => {
    const unit = await api.post("/cooling/units", { unit_kind: "crah", asset_tag: `CRAH-${name}-${sfx}`, site_id: site.id, name, cooling_group_id: group.id, operating_status: "online", ...extra });
    for (const status of ["installed", "active"]) await api.post(`/managed-assets/${unit.id}/lifecycle-transition`, { to_status: status });
    await api.put(`/cooling/units/${unit.id}/placement`, { room_id: room.id, x_mm: x, y_mm: y, rotation_deg: 0 });
    return unit;
  };
  const crahA = await makeUnit("CRAH-A", { rated_cooling_capacity_kw: 40, configured_cooling_capacity_kw: 30, airflow_capacity_m3_s: 4.5, supply_direction_deg: 90 }, 500, 500);
  const crahB = await makeUnit("CRAH-B", { rated_cooling_capacity_kw: 40, airflow_capacity_m3_s: 3 }, 5500, 500); // no configured direction
  const served = await api.post("/cooling/zones", { room_id: room.id, name: "Whole hall", zone_kind: "served_zone" });
  await api.post("/cooling/relations", { cooling_unit_id: crahA.id, thermal_zone_id: served.id, relation_kind: "serves", semantics: "authoritative" });
  await api.post("/cooling/relations", { cooling_unit_id: crahB.id, thermal_zone_id: served.id, relation_kind: "serves", semantics: "authoritative" });
  const hot = await api.post("/cooling/zones", { room_id: room.id, name: "Hot aisle 1", zone_kind: "hot_aisle", containment: "contained", geometry_type: "rect", x_mm: 3300, y_mm: 800, width_mm: 1000, height_mm: 2400 });
  await api.post(`/cooling/zones/${hot.id}/containment-elements`, { element_kind: "boundary", x1_mm: 3300, y1_mm: 800, x2_mm: 3300, y2_mm: 3200 }, { "If-Match": "1" });
  await api.post(`/cooling/zones/${hot.id}/containment-elements`, { element_kind: "opening", x1_mm: 3300, y1_mm: 1400, x2_mm: 3300, y2_mm: 1800 }, { "If-Match": "2" });
  await api.post("/cooling/zones", { room_id: room.id, name: "Cold aisle 1", zone_kind: "cold_aisle", geometry_type: "rect", x_mm: 1200, y_mm: 800, width_mm: 1000, height_mm: 2400 });

  // ---------------------------------------------------------------- sensors: 3 fresh, 1 stale, 1 never reporting; one airflow sensor
  const collector = await collectorFor(request, api, sfx);
  const makeSensor = async (name: string, x: number, y: number, extra: Record<string, unknown> = {}) => {
    const sensor = await api.post("/cooling/sensors", { asset_tag: `SN-${name}-${sfx}`, site_id: site.id, name, sensor_kind: "temperature", measurement_role: "ambient", ...extra });
    for (const status of ["installed", "active"]) await api.post(`/managed-assets/${sensor.id}/lifecycle-transition`, { to_status: status });
    await api.put(`/cooling/sensors/${sensor.id}/placement`, { room_id: room.id, placement_type: "floor_standing", x_mm: x, y_mm: y });
    return sensor;
  };
  const t1 = await makeSensor("T1", 1000, 1000);
  const t2 = await makeSensor("T2", 5000, 1000);
  const t3 = await makeSensor("T3", 1000, 3000);
  const t4 = await makeSensor("T4-stale", 5000, 3000);
  const t5 = await makeSensor("T5-silent", 3000, 3800);
  const flow = await makeSensor("Flow1", 3000, 2000, { sensor_kind: "airflow", flow_direction_deg: 180 });
  const readings = [
    { source: await collector.map(t1.id, "temperature_c", "degC"), value: 20, ageSeconds: 10 },
    { source: await collector.map(t2.id, "temperature_c", "degC"), value: 30, ageSeconds: 12 },
    { source: await collector.map(t3.id, "temperature_c", "degC"), value: 24, ageSeconds: 8 },
    { source: await collector.map(t4.id, "temperature_c", "degC"), value: 27, ageSeconds: 3600 }, // an hour old at a 60 s poll interval
    { source: await collector.map(flow.id, "airflow_m3_s", "m3/h"), value: 4500, ageSeconds: 5 }, // 1.25 m3/s after conversion
    { source: await collector.map(server.id, "power_kw", "kW"), value: 3, ageSeconds: 5 },
  ];
  await collector.map(t5.id, "temperature_c", "degC"); // mapped but never reports => missing
  await collector.send(readings);

  // ---------------------------------------------------------------- API contract of the same data the browser is about to render
  const map = await api.get(`/cooling/rooms/${room.id}/heat-map`);
  expect(map.state).toBe("partial");
  expect(map.grid.value_provenance).toBe("interpolated");
  expect(map.sensors.map((s: any) => [s.name, s.state, s.value_provenance]).sort()).toEqual([
    ["T1", "measured_fresh", "measured"], ["T2", "measured_fresh", "measured"], ["T3", "measured_fresh", "measured"],
    ["T4-stale", "measured_stale", "measured"], ["T5-silent", "missing", "none"],
  ]);
  let stored = 0;
  for (const asset of [t1, t2, t3, t4, t5, flow, server]) stored += (await api.get(`/telemetry/latest?managed_asset_id=${asset.id}&limit=100`)).length;
  expect(stored).toBe(readings.length); // exactly what the collector sent: building the map wrote nothing back into telemetry

  // ---------------------------------------------------------------- the browser: Cooling & environment mode of the calibrated 2D plan
  await login(page);
  await page.getByRole("link", { name: "Floor Plans", exact: true }).click();
  await page.getByRole("link", { name: roomName }).click();
  const verify = page.getByTestId("verify-2d");
  await expect(verify.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "validated", { timeout: 20_000 });
  await verify.getByRole("button", { name: "Cooling & environment" }).click();
  const panel = verify.getByTestId("cooling-panel");

  // measured vs interpolated, stale vs missing, and a map that is never called healthy while sensors are not contributing
  await expect(panel.getByTestId("map-state-badge")).toContainText("Temperature map: Partial", { timeout: 20_000 });
  await expect(panel.getByTestId("count-fresh")).toHaveText("3");
  await expect(panel.getByTestId("count-stale")).toHaveText("1");
  await expect(panel.getByTestId("count-missing")).toHaveText("1");
  await expect(panel.getByTestId("map-reasons")).toContainText("stale, missing, invalid or unlocated");
  await expect(panel.getByTestId("heat-cells")).toHaveAttribute("data-provenance", "interpolated");
  expect(await panel.locator('[data-testid="heat-cells"] rect[data-cell-index]').count()).toBeGreaterThan(20);
  expect(await panel.locator('[data-testid="heat-cells"] rect[data-provenance="interpolated"]').count()).toBe(await panel.locator('[data-testid="heat-cells"] rect[data-cell-index]').count());
  const sensorLayer = panel.getByTestId("sensor-layer");
  await expect(sensorLayer.locator('[data-state="measured_fresh"]')).toHaveCount(3);
  await expect(sensorLayer.locator('[data-state="measured_stale"]')).toHaveCount(1);
  await expect(sensorLayer.locator('[data-state="missing"]')).toHaveCount(1); // a placed sensor that never reported is drawn as missing, not omitted
  await expect(panel.getByTestId("sensor-table").locator('tr[data-sensor-state="missing"]')).toHaveCount(1); // and listed in the text table
  await expect(panel.getByTestId("map-disclaimer")).toContainText("not validated CFD");
  await expect(panel.getByTestId("legend-interpolated")).toContainText("not measured");
  await expect(panel.getByTestId("legend-range")).toContainText("degC");

  await panel.locator('[data-sensor-id][data-state="measured_stale"]').click();
  await expect(panel.getByTestId("detail-state")).toContainText("Measured, stale");
  await expect(panel.getByTestId("detail-value")).toContainText("(stale: not current)");
  await expect(panel.getByTestId("detail-used")).toContainText("No (stale)");
  await panel.locator('[data-sensor-id][data-state="measured_fresh"]').first().click();
  await expect(panel.getByTestId("detail-provenance")).toHaveText("Measured");
  await expect(panel.getByTestId("detail-used")).toContainText("Yes, contributes");

  // a cell, inspected with the keyboard alone, is labelled as an estimate
  await panel.getByTestId("heat-cells").focus();
  await page.keyboard.press("ArrowRight");
  await expect(panel.getByTestId("detail-provenance")).toHaveText("Interpolated (not measured)");
  await expect(panel.getByTestId("detail-value")).toContainText("≈");
  await shot(page, "cooling-2d-partial-temperature");

  // aisle / containment geometry rendered from persisted zone objects
  const zones = panel.getByTestId("zone-layer");
  await expect(zones.locator('[data-zone-kind="hot_aisle"]')).toHaveAttribute("data-containment", "contained");
  await expect(zones).toContainText("Hot aisle · contained");
  await expect(zones).toContainText("Cold aisle");
  await expect(zones.locator('[data-containment-element="boundary"]')).toHaveCount(1);
  await expect(zones.locator('[data-containment-element="opening"]')).toHaveCount(1);

  // airflow provenance: configured design arrow for CRAH-A, measured arrow for the sensor, nothing invented for CRAH-B
  const airflow = panel.getByTestId("airflow-layer");
  await expect(airflow.locator("[data-airflow-id]")).toHaveCount(2);
  await expect(airflow.locator(`[data-airflow-id="${crahA.id}"]`)).toHaveAttribute("data-provenance", "configured");
  await expect(airflow.locator(`[data-airflow-id="${flow.id}"]`)).toHaveAttribute("data-provenance", "measured");
  await expect(airflow.locator(`[data-airflow-id="${crahB.id}"]`)).toHaveCount(0);
  await expect(airflow).toContainText("configured design 4.5 m³/s");
  await expect(airflow).toContainText("measured 4500 m3/h"); // 1.25 m3/s, shown in the registry's presentation unit
  await expect(panel.getByTestId("airflow-note")).toContainText("1 measured, 1 configured design, 0 modelled");

  // capacity and headroom: 30 kW derated CRAH-A + 40 kW CRAH-B, 3 kW measured IT load, N+1 verified
  const capacityRow = panel.getByTestId("capacity-table").locator("tbody tr").first();
  await expect(capacityRow).toContainText("Whole hall");
  await expect(capacityRow).toContainText("2 / 2");
  await expect(capacityRow).toContainText("80 kW"); // installed rated
  await expect(capacityRow).toContainText("70 kW"); // available (configured 30 + 40)
  await expect(capacityRow.getByTestId("load-cell")).toContainText("3 kW (measured)");
  await expect(capacityRow.getByTestId("headroom-cell")).toContainText("67 kW");
  await expect(capacityRow).toHaveAttribute("data-redundancy", "redundant");
  await expect(panel.getByTestId("capacity-assumption")).toContainText("No COP, PUE or efficiency factor");

  // exceptions: derived stale / missing items, with their source
  const list = panel.getByTestId("exception-list");
  await expect(list.locator('[data-exception-type="stale_sensor"]')).toContainText("T4-stale");
  await expect(list.locator('[data-exception-type="missing_sensor_data"]')).toContainText("T5-silent");
  await expect(list.locator('[data-exception-type="stale_sensor"]')).toHaveAttribute("data-source", "derived");

  // an operator takes CRAH-A out of service: capacity, redundancy and exceptions follow after the 2D view refetches
  await api.patch(`/cooling/units/${crahA.id}`, { operating_status: "fault" }, 1);
  await verify.getByRole("button", { name: "Layout" }).click();
  await expect(verify.getByTestId("cooling-panel")).toHaveCount(0);
  await verify.getByRole("button", { name: "Cooling & environment" }).click(); // remounting refetches: live data is never served from an old cache entry
  const after = verify.getByTestId("cooling-panel");
  const afterRow = after.getByTestId("capacity-table").locator("tbody tr").first();
  await expect(afterRow).toContainText("1 / 2", { timeout: 20_000 });
  await expect(afterRow).toContainText("40 kW");
  await expect(afterRow).toHaveAttribute("data-redundancy", "degraded");
  await expect(after.getByTestId("exception-list").locator('[data-exception-type="cooling_unit_unavailable"]')).toContainText("CRAH-A");

  // humidity: no humidity sensors exist, so nothing is estimated
  await after.getByRole("button", { name: "Humidity" }).click();
  await expect(after.getByTestId("map-state-badge")).toContainText("Humidity map: Unavailable", { timeout: 20_000 });
  await expect(after.getByTestId("heat-cells")).toHaveCount(0);
  await expect(after.getByTestId("sensors-empty")).toContainText("No humidity sensor is placed in this room.");
  await after.getByRole("button", { name: "Temperature" }).click();

  // ---------------------------------------------------------------- 3D twin: same room, same scale, thermal layer on the floor plane
  await page.getByTestId("verify-2d").getByRole("link", { name: "Open 3D twin" }).click();
  await expect(page).toHaveURL(new RegExp(`/floor-plans/3d-layout\\?room=${room.id}`));
  await page.getByLabel("Thermal layer").selectOption("temperature_c");
  const thermal3d = page.getByTestId("thermal-3d");
  await expect(thermal3d).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("thermal-3d-state")).toContainText("Partial", { timeout: 20_000 });
  expect(await thermal3d.locator('g[data-provenance="measured"]').count()).toBe(5);
  expect(await thermal3d.locator('rect[data-provenance="interpolated"]').count()).toBeGreaterThan(20);
  await expect(thermal3d.locator('[data-zone-kind="hot_aisle"]')).toHaveAttribute("data-containment", "contained");
  await expect(thermal3d.locator("[data-unit-id]")).toHaveCount(2);
  await expect(page.getByTestId("thermal-3d-sensors")).toContainText("T4-stale: Measured, stale");
  await expect(page.getByTestId("thermal-3d-sensors")).toContainText("T5-silent: Missing, no value");
  await expect(page.getByTestId(`cuboid-${rack.id}`)).toHaveAttribute("data-x-mm", "2500");
  await shot(page, "cooling-3d-thermal");
});

test("a site-restricted user cannot reach any thermal view or cooling configuration", async ({ request }) => {
  const api = await connect(request);
  const sfx = Math.random().toString(36).slice(2, 8);
  const { site, room } = await calibratedRoom(request, api, sfx);
  const group = await api.post("/groups", { name: `e2e-105-restricted-${sfx}` });
  await api.put(`/groups/${group.id}/permissions`, { allow: ["cooling:read", "cooling:manage", "spatial:read", "telemetry:read", "rack:read"], deny: [] });
  await api.put(`/groups/${group.id}/site-access`, { sites: [{ site_id: site.id, rack_scope: "all", rack_ids: [] }] });
  const email = `e2e-105-${sfx}@example.com`;
  const password = "Restricted-Passw0rd!";
  await api.post("/users", { email, full_name: "Restricted", password, group_ids: [group.id] });
  const token = (await (await request.post("/api/v1/auth/login", { data: { email, password } })).json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  for (const path of ["layout", "environment", "heat-map", "airflow", "capacity", "exceptions"]) {
    const response = await request.get(`/api/v1/cooling/rooms/${room.id}/${path}`, { headers });
    expect(response.status(), path).toBe(403);
  }
  expect((await request.get("/api/v1/cooling/units", { headers })).status()).toBe(403);
  expect((await request.post("/api/v1/cooling/zones", { headers, data: { room_id: room.id, name: "x", zone_kind: "served_zone" } })).status()).toBe(403);
});
