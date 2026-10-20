import { APIRequestContext, Page, expect, test } from "@playwright/test";
import { crc32 } from "node:zlib";

/** Issue #104 E2E: import -> classify -> correct -> calibrate -> accept -> 2D verify -> 3D verify -> overlay verify,
 * against the real backend, the real Celery worker and the real sandboxed DXF parser. The DXF is generated here (no
 * CAD software, no binary fixtures). Rack A and Rack B already exist in the room at the positions the drawing shows. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

function lwpoly(handle: string, layer: string, pts: [number, number][]): string {
  const verts = pts.map(([x, y]) => `10\n${x}\n20\n${y}\n`).join("");
  return `0\nLWPOLYLINE\n5\n${handle}\n8\n${layer}\n90\n${pts.length}\n70\n1\n${verts}`;
}
const rect = (h: string, layer: string, x: number, y: number, w: number, d: number) =>
  lwpoly(h, layer, [[x, y], [x + w, y], [x + w, y + d], [x, y + d]]);
const label = (h: string, x: number, y: number, text: string) => `0\nTEXT\n5\n${h}\n8\nTEXT\n10\n${x}\n20\n${y}\n40\n100\n1\n${text}\n`;

function rackRowDxf(): Buffer {
  let entities = rect("100", "WALLS", 0, 0, 6000, 4000);
  for (let i = 0; i < 4; i += 1) {
    const x = 1000 + i * 700;
    entities += rect(`2${i}0`, "RACKS", x, 1000, 600, 1000) + label(`3${i}0`, x + 300, 1500, `RACK-0${i + 1}`);
  }
  const text = `0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n${entities}0\nENDSEC\n0\nEOF\n`;
  return Buffer.from(text);
}

/** Minimal STORED zip writer: enough to hand the browser a hostile .vsdx without a zip library. */
function storedZip(files: Record<string, string>): Buffer {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of Object.entries(files)) {
    const nameBuf = Buffer.from(name);
    const data = Buffer.from(content);
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    locals.push(local, nameBuf, data);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBuf);
    offset += 30 + nameBuf.length + data.length;
  }
  const centralBuf = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralBuf, end]);
}

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
  const org = await post("/organizations", { name: `E2E-104 Org ${sfx}` });
  const country = await post("/countries", { organization_id: org.id, name: "Testland", iso_code: "TL" });
  const city = await post("/cities", { country_id: country.id, name: "Testville" });
  const site = await post("/sites", { city_id: city.id, code: `T${sfx}`.slice(0, 10), name: `E2E-104 Site ${sfx}` });
  const building = await post("/buildings", { site_id: site.id, code: "A", name: "B" });
  const floor = await post("/floors", { building_id: building.id, name: "F", level_number: 1 });
  const roomName = `E2E-104 Hall ${sfx}`;
  const room = await post("/rooms", { floor_id: floor.id, code: `H${sfx}`.slice(0, 10), name: roomName });

  const rackModel = await post("/rack-models", { manufacturer: "Acme", model_name: `RM-${sfx}` });
  const rackRevision = await post(`/rack-models/${rackModel.id}/revisions`, { height_u: 42, width_mm: 600, depth_mm: 1000 });
  const rackA = await post("/racks", { asset_tag: `RKA-${sfx}`, model_revision_id: rackRevision.id, name: "E2E Rack A", room_id: room.id, x_mm: 1000, y_mm: 2000, rotation_deg: 0 });
  const rackB = await post("/racks", { asset_tag: `RKB-${sfx}`, model_revision_id: rackRevision.id, name: "E2E Rack B", room_id: room.id, x_mm: 1700, y_mm: 2000, rotation_deg: 0 });

  // a dual-fed server mounted in Rack A, behind two UPSs and two breakers (the power overlay's data)
  const eqModel = await post("/equipment-models", { manufacturer: "Acme", model_name: `EM-${sfx}` });
  const eqRevision = await post(`/equipment-models/${eqModel.id}/revisions`, {});
  const server = await post("/equipment", { asset_tag: `SRV-${sfx}`, model_revision_id: eqRevision.id, hostname: `srv-${sfx}` });
  const moved = await api.post(`/api/v1/equipment/${server.id}/move`, {
    headers, data: { placement_type: "rack_mounted", room_id: room.id, rack_id: rackA.id, u_start: 1, u_end: 3, side: "front" },
  });
  expect(moved.ok(), await moved.text()).toBeTruthy();
  const breakers: Record<string, { id: string }> = {};
  for (const side of ["A", "B"]) {
    const ups = await post("/power/upses", { asset_tag: `UPS-${side}-${sfx}`, name: `UPS-${side}`, room_id: room.id, capacity_kva: 50 });
    const brk = await post("/power/protection-devices", {
      housing_asset_id: ups.managed_asset_id, site_id: site.id, label: `BRK-${side}-${sfx}`, rating_a: 32, voltage_v: 230, poles: 1, phase_config: "single",
    });
    breakers[side] = brk;
    const feed = await post("/power/equipment-feeds", { equipment_asset_id: server.id, label: `Feed ${side}` });
    await post("/power/connections", { source_node_id: ups.id, target_node_id: brk.id, connection_type: "feed", feed_label: side });
    await post("/power/connections", { source_node_id: brk.id, target_node_id: feed.id, connection_type: "feed", feed_label: side });
  }
  return { headers, roomId: room.id as string, roomName, rackA, rackB, breakers, rackRevisionId: rackRevision.id as string, sfx };
}

async function login(page: Page) {
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}

/** Optional evidence screenshots: set E2E_SCREENSHOT_DIR to keep them. */
const shot = async (page: Page, name: string) => {
  if (process.env.E2E_SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.E2E_SCREENSHOT_DIR}/${name}.png`, fullPage: true });
};

const ratio = async (locator: ReturnType<Page["getByTestId"]>, a: string, b: string) =>
  Number(await locator.getAttribute(a)) / Number(await locator.getAttribute(b));

test("import -> classify -> correct -> calibrate -> accept -> 2D verify -> 3D verify -> overlay verify", async ({ page, request }) => {
  const world = await seed(request);
  await login(page);

  // ---------------------------------------------------------------- open the room and upload a DXF
  await page.getByRole("link", { name: "Floor Plans", exact: true }).click();
  await page.getByRole("link", { name: world.roomName }).click();
  await expect(page.getByRole("heading", { name: /Spatial digital twin/ })).toBeVisible();
  await page.getByRole("button", { name: "New draft" }).click();
  await page.getByLabel("Upload floor plan file").setInputFiles({ name: "hall.dxf", mimeType: "application/octet-stream", buffer: rackRowDxf() });

  // ---------------------------------------------------------------- parse + classify (real Celery worker, sandboxed parser)
  await expect(page.getByTestId("job-status")).toHaveText("parsed", { timeout: 60_000 });
  await expect(page.getByTestId("racks-detected")).toHaveText("4");
  await expect(page.getByTestId("diagnostics-panel")).toContainText("dxf_ascii_subset");
  await expect(page.getByTestId("diagnostics-panel")).toContainText("Source units");
  const list = page.getByTestId("candidate-list");
  await expect(list.getByRole("button", { name: /RACK-01/ })).toBeVisible();
  await expect(list.getByRole("button", { name: /RACK-04/ })).toBeVisible();

  // nothing is acceptable before calibration
  await list.getByRole("button", { name: /RACK-01/ }).click();
  await expect(page.getByRole("button", { name: "Accept as authoritative geometry" })).toBeDisabled();
  await expect(page.getByText("Calibrate the drawing first")).toBeVisible();
  await expect(page.getByTestId("import-review-canvas")).toHaveAttribute("data-calibrated", "false");

  // ---------------------------------------------------------------- calibrate (declared units) and see the error bound
  await page.getByRole("button", { name: "Set calibration" }).click();
  await expect(page.getByTestId("notice")).toContainText("Calibration recorded (high confidence, error ≤ ±0.5 mm)");
  await expect(page.getByTestId("current-calibration")).toContainText("1 mm = 1 mm");
  await expect(page.getByTestId("import-review-canvas")).toHaveAttribute("data-calibrated", "true");
  // the rack that exists at that position is matched by geometry, not by label
  await expect(list.getByRole("button", { name: /RACK-01/ })).toContainText("matches a rack");
  await expect(list.getByRole("button", { name: /RACK-02/ })).toContainText("matches a rack");
  await expect(list.getByRole("button", { name: /RACK-03/ })).toContainText("no matching rack");

  // ---------------------------------------------------------------- correct RACK-02: +50 mm, staged only
  await list.getByRole("button", { name: /RACK-02/ }).click();
  await expect(page.getByTestId("canonical-note")).toContainText("1700, 2000 mm, 600 × 1000 mm");
  const left = page.getByLabel("Left X (mm)");
  await left.fill("1750");
  await page.getByRole("button", { name: "Stage correction" }).click();
  await expect(page.getByTestId("canonical-note")).toContainText("1750, 2000 mm");
  await expect(list.getByRole("button", { name: /RACK-02/ })).toContainText("edited");
  await expect(page.getByRole("button", { name: "Undo last correction" })).toBeEnabled();
  expect((await (await request.get(`/api/v1/racks/${world.rackB.id}`, { headers: world.headers })).json()).placement.x_mm).toBe(1700);

  // ---------------------------------------------------------------- accept: boundary, linked Rack A, drawn-only Rack B
  await list.getByRole("button", { name: /room_outline/ }).click();
  await page.getByRole("button", { name: "Accept as authoritative geometry" }).click();
  await expect(page.getByTestId("notice")).toContainText("Accepted");
  await expect(page.getByTestId("calibration-panel")).toContainText("can no longer be changed on this revision");

  await list.getByRole("button", { name: /RACK-01/ }).click();
  await expect(page.getByLabel("Matched rack")).toHaveValue(world.rackA.id);
  await page.getByRole("radio", { name: /Link the shape to the asset's existing placement/ }).check();
  await page.getByRole("button", { name: "Accept as authoritative geometry" }).click();
  await expect(list.getByRole("button", { name: /RACK-01/ })).toHaveCount(0);

  await list.getByRole("button", { name: /RACK-02/ }).click();
  await page.getByRole("button", { name: "Accept as authoritative geometry" }).click();
  await expect(list.getByRole("button", { name: /RACK-02/ })).toHaveCount(0);
  const rackA = await (await request.get(`/api/v1/racks/${world.rackA.id}`, { headers: world.headers })).json();
  expect(rackA.placement.x_mm).toBe(1000); // linking never moves the asset
  expect(rackA.placement.spatial_object_id).not.toBeNull();
  const rackB = await (await request.get(`/api/v1/racks/${world.rackB.id}`, { headers: world.headers })).json();
  expect(rackB.placement.x_mm).toBe(1700); // the corrected drawn shape (1750) did not move Rack B
  expect(rackB.placement.spatial_object_id).toBeNull();
  await list.getByRole("button", { name: /RACK-03/ }).click();
  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(list.getByRole("button", { name: /RACK-03/ })).toHaveCount(0);

  // ---------------------------------------------------------------- activate and verify the scaled 2D plan
  await page.getByRole("button", { name: "Activate" }).first().click();
  const verify = page.getByTestId("verify-2d");
  await expect(verify.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "validated", { timeout: 15_000 });
  await expect(verify.getByTestId("calibration-summary")).toContainText("error ≤ ±0.5 mm");
  await expect(verify.getByTestId("scale-source")).toContainText("approved room boundary");
  const rackAShape = verify.getByTestId(`rack-${world.rackA.id}`);
  await expect(rackAShape).toBeVisible();
  expect(await ratio(rackAShape, "data-width-px", "data-depth-px")).toBeCloseTo(0.6, 1);
  await expect(verify.getByTestId("room-origin")).toContainText("room origin (0, 0)");
  await expect(verify.getByTestId("scale-bar")).toBeVisible();
  const linesWithGrid = await verify.getByTestId("scene-chrome").locator("line").count();
  await verify.getByRole("checkbox", { name: "Engineering grid" }).uncheck();
  expect(await verify.getByTestId("scene-chrome").locator("line").count()).toBeLessThan(linesWithGrid);
  await verify.getByRole("checkbox", { name: "Engineering grid" }).check();
  await expect(verify.locator('[data-object-type="rack"]')).toHaveCount(2); // the two accepted drawn shapes

  await shot(page, "2d-validated");

  // ---------------------------------------------------------------- overlays on the 2D plan
  await verify.getByLabel("Operational overlay").selectOption("power");
  await expect(verify.getByRole("link", { name: /Rack E2E Rack A.*power: Normal/ })).toBeVisible({ timeout: 15_000 });
  await expect(verify.getByRole("link", { name: /Rack E2E Rack B.*power: No data/ })).toBeVisible();
  const trip = await request.post(`/api/v1/power/protection-devices/${world.breakers.A.id}/state`, {
    headers: { ...world.headers, "If-Match": "1" }, data: { state: "tripped" },
  });
  expect(trip.ok(), await trip.text()).toBeTruthy();
  await verify.getByLabel("Operational overlay").selectOption("network");
  await verify.getByLabel("Operational overlay").selectOption("power");
  await expect(verify.getByRole("link", { name: /Rack E2E Rack A.*power: Warning/ })).toBeVisible({ timeout: 15_000 });
  await verify.getByLabel("Operational overlay").selectOption("environment");
  await expect(verify.getByRole("link", { name: /Rack E2E Rack A.*environment: No data/ })).toBeVisible();
  await shot(page, "2d-power-overlay");
  await verify.getByLabel("Operational overlay").selectOption("none");

  // ---------------------------------------------------------------- the 3D twin shows the same identities at real scale
  await page.getByRole("link", { name: "Open 3D twin" }).click();
  await expect(page).toHaveURL(new RegExp(`/floor-plans/3d-layout\\?room=${world.roomId}`));
  const cuboidA = page.getByTestId(`cuboid-${world.rackA.id}`);
  await expect(cuboidA).toBeVisible();
  await expect(page.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "validated");
  await expect(cuboidA).toHaveAttribute("data-x-mm", "1000");
  await expect(cuboidA).toHaveAttribute("data-y-mm", "2000");
  expect(await ratio(cuboidA, "data-width-px", "data-depth-px")).toBeCloseTo(0.6, 1);
  expect(await ratio(cuboidA, "data-height-px", "data-depth-px")).toBeCloseTo(1.867, 1);
  await page.getByRole("button", { name: /Select rack E2E Rack A/ }).click();
  await expect(page.getByTestId("selected-asset-id")).toHaveAttribute("data-asset-id", world.rackA.id);
  await expect(page.getByRole("link", { name: "Open rack elevation" })).toHaveAttribute("href", `/racks/${world.rackA.id}`);
  await expect(page.getByText("1867 mm (42U)")).toBeVisible();

  await shot(page, "3d-selected");
  await page.getByLabel("Operational overlay").selectOption("power");
  await expect(page.getByTestId("selected-overlay")).toContainText("Warning", { timeout: 15_000 });
  await shot(page, "3d-power-overlay");
  await expect(page.getByTestId("overlay-legend")).toContainText("Power topology and protection-device state");

  // ---------------------------------------------------------------- incomplete data is declared, never guessed
  const unpositioned = await request.post("/api/v1/racks", {
    headers: world.headers, data: { asset_tag: `RKC-${world.sfx}`, model_revision_id: world.rackRevisionId, name: "E2E Rack C", room_id: world.roomId },
  });
  expect(unpositioned.ok(), await unpositioned.text()).toBeTruthy();
  await page.getByRole("link", { name: "2D floor plan" }).click();
  await page.getByRole("link", { name: "Open 3D twin" }).click();
  await expect(page.getByTestId("layout-state")).toHaveAttribute("data-layout-state", "incomplete");
  await expect(page.getByTestId("incomplete-panel")).toContainText("E2E Rack C");
  await expect(page.getByTestId("incomplete-panel")).toContainText("no floor position is recorded");
  await expect(page.locator('[data-testid^="cuboid-"]')).toHaveCount(2);
  await expect(page.getByText("≈")).toHaveCount(0);
});

test("hostile and mismatched uploads are refused with a readable reason and change nothing", async ({ page, request }) => {
  const world = await seed(request);
  await login(page);
  await page.getByRole("link", { name: "Floor Plans", exact: true }).click();
  await page.getByRole("link", { name: world.roomName }).click();
  await page.getByRole("button", { name: "New draft" }).click();
  const upload = page.getByLabel("Upload floor plan file");

  // declared as DXF, but the bytes are a PNG: rejected before anything is queued
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(64)]);
  await upload.setInputFiles({ name: "plan.dxf", mimeType: "application/octet-stream", buffer: png });
  await expect(page.getByTestId("action-error")).toContainText("declared as dxf but its content is png");
  await expect(page.getByTestId("action-error")).toHaveAttribute("role", "alert");

  // a VSDX package with a path-traversal entry: parsed in the sandbox, rejected, no candidates
  const hostile = storedZip({
    "[Content_Types].xml": "<Types/>",
    "../../evil.xml": "x",
    "visio/document.xml": "<VisioDocument/>",
  });
  await upload.setInputFiles({ name: "evil.vsdx", mimeType: "application/octet-stream", buffer: hostile });
  await expect(page.getByTestId("job-status")).toHaveText("failed", { timeout: 60_000 });
  await expect(page.getByTestId("job-failure")).toContainText("path traversal");
  await expect(page.getByTestId("review-workspace")).toHaveCount(0);

  // a truncated DXF fails with its reason rather than hanging in "processing"
  await upload.setInputFiles({ name: "cut.dxf", mimeType: "application/octet-stream", buffer: Buffer.from("0\nSECTION\n2\nENTITIES\n0\nLINE\n") });
  await expect(page.getByTestId("job-failure")).toContainText(/truncated|malformed/i, { timeout: 60_000 });
  await expect(page.getByTestId("job-status")).toHaveText("failed");
});
