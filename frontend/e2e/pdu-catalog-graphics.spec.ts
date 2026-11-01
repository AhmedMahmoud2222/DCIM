import { expect, test } from "@playwright/test";

/** Issue #150 acceptance-evidence — authenticated end-to-end coverage of the PDU front/rear
 * graphics path that `pdu-catalog.spec.ts` does not exercise: upload a front and a rear
 * image through the designer UI, place outlet-labelled markers on both, verify the markers
 * persisted server-side, publish, and verify the published revision renders the same images
 * and markers read-only (no upload/placement controls) and rejects direct marker writes.
 *
 * Requires the same running backend/Administrator as pdu-catalog.spec.ts. Tests only. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";
const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5173";

// Same minimal valid PNG the other catalog E2E specs upload (accepted by the backend's Pillow validation).
const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

test.describe.configure({ timeout: 120_000 });

test.describe("Issue #150: PDU front/rear graphics and markers", () => {
  let manufacturerId: string;
  let manufacturerName: string;
  let modelName: string;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const loginResp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
    expect(loginResp.ok()).toBeTruthy();
    const token = (await loginResp.json()).access_token as string;
    const suffix = Math.random().toString(36).slice(2, 8);
    manufacturerName = `E2E PDU Gfx Mfr ${suffix}`;
    modelName = `E2E PDU Gfx ${suffix}`;
    const manufacturer = await api.post("/api/v1/catalog/manufacturers", {
      headers: { Authorization: `Bearer ${token}` },
      data: { name: manufacturerName },
    });
    expect(manufacturer.ok()).toBeTruthy();
    manufacturerId = (await manufacturer.json()).id as string;
    await api.dispose();
  });

  test("upload front/rear images, place outlet markers, publish, and see them read-only", async ({ page }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // Client-side navigation only after login: the access token lives in memory.
    await page.getByRole("link", { name: "Asset Catalog" }).click();
    await page.getByRole("button", { name: "New Model" }).click();
    await page.getByRole("textbox", { name: "Search manufacturers" }).fill(manufacturerName);
    await page.getByRole("combobox", { name: "Model manufacturer" }).selectOption(manufacturerId);
    await page.getByRole("combobox", { name: "Model category" }).selectOption("pdu");
    await page.getByRole("textbox", { name: "Model name" }).fill(modelName);
    await page.getByRole("button", { name: "Create Model" }).click();
    await page.getByRole("textbox", { name: "Search models" }).fill(modelName);
    await page.getByRole("link", { name: modelName }).click();
    await page.getByRole("button", { name: "New Draft" }).click();
    await expect(page).toHaveURL(/\/admin\/catalog\/revisions\/([0-9a-f-]{36})/);
    const revisionId = /\/admin\/catalog\/revisions\/([0-9a-f-]{36})/.exec(page.url())![1];

    // --- PDU specification with two outlets labelled "Outlet 1" / "Outlet 2" (the marker label suggestions).
    const pduSpec = page.locator('[aria-label="PDU electrical specification"]');
    await expect(pduSpec).toBeVisible();
    await pduSpec.getByRole("spinbutton", { name: "Rated input voltage (V)" }).fill("230");
    await pduSpec.getByRole("spinbutton", { name: "Rated input current (A)" }).fill("32");
    await pduSpec.getByRole("combobox", { name: "Phase" }).selectOption("single");
    await pduSpec.getByRole("combobox", { name: "Mounting orientation" }).selectOption("vertical");
    for (const n of [1, 2]) {
      await pduSpec.getByRole("button", { name: "+ Add outlet" }).click();
      await pduSpec.getByRole("spinbutton", { name: `Outlet ${n} rated voltage` }).fill("230");
      await pduSpec.getByRole("spinbutton", { name: `Outlet ${n} rated current` }).fill("10");
    }
    const versionBefore = await pduSpec.locator("span").filter({ hasText: "Saved against If-Match version" }).innerText();
    await pduSpec.getByRole("button", { name: "Save PDU specification" }).click();
    await expect(pduSpec.locator("span").filter({ hasText: "Saved against If-Match version" })).not.toHaveText(versionBefore);

    // --- Front and rear images through the real upload controls.
    const front = page.locator("section").filter({ has: page.getByRole("heading", { name: /front view/i }) });
    const rear = page.locator("section").filter({ has: page.getByRole("heading", { name: /rear view/i }) });
    await front.locator('input[type="file"]').setInputFiles({ name: "front.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG });
    await expect(front.getByTestId("graphic-canvas")).toBeVisible();
    await rear.locator('input[type="file"]').setInputFiles({ name: "rear.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG });
    await expect(rear.getByTestId("graphic-canvas")).toBeVisible();
    await expect(front.getByRole("img", { name: "front view of this model" })).toBeVisible();
    await expect(rear.getByRole("img", { name: "rear view of this model" })).toBeVisible();

    // --- One outlet-labelled marker per side.
    for (const [panel, label] of [[front, "Outlet 1"], [rear, "Outlet 2"]] as const) {
      await panel.getByRole("button", { name: "+ Other" }).click();
      await panel.getByTestId("graphic-canvas").click({ position: { x: 40, y: 20 } });
      // With outlet-label suggestions the input carries a `list` (datalist), so its ARIA role is combobox, not textbox.
      await panel.getByRole("combobox", { name: "Marker label" }).fill(label);
      await panel.getByRole("button", { name: "Place marker" }).click();
      await expect(panel.getByRole("button", { name: new RegExp(`Other marker, ${label}`) })).toBeVisible();
    }

    // --- Persisted server-side with their labels and normalised coordinates.
    const token = (await (await page.request.post("/api/v1/auth/login", {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    })).json()).access_token as string;
    const headers = { Authorization: `Bearer ${token}` };
    const draftDetail = await (await page.request.get(`/api/v1/catalog/revisions/${revisionId}`, { headers })).json();
    const bySide = Object.fromEntries(
      draftDetail.graphics.map((g: { side: string; markers: { label: string; marker_x: number; marker_y: number }[] }) => [g.side, g.markers]),
    );
    expect(bySide.front).toHaveLength(1);
    expect(bySide.rear).toHaveLength(1);
    expect(bySide.front[0].label).toBe("Outlet 1");
    expect(bySide.rear[0].label).toBe("Outlet 2");
    for (const m of [bySide.front[0], bySide.rear[0]]) {
      expect(m.marker_x).toBeGreaterThanOrEqual(0);
      expect(m.marker_x).toBeLessThanOrEqual(1);
      expect(m.marker_y).toBeGreaterThanOrEqual(0);
      expect(m.marker_y).toBeLessThanOrEqual(1);
    }

    // --- Publish through the UI; graphics persist and become read-only.
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    await expect(page.getByText("Ready to publish.")).toBeVisible();
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(page.locator("span").filter({ hasText: /^published$/ }).first()).toBeVisible();

    for (const [panel, label] of [[front, "Outlet 1"], [rear, "Outlet 2"]] as const) {
      await expect(panel.getByRole("img", { name: /view of this model/ })).toBeVisible();
      await expect(panel.getByRole("button", { name: new RegExp(`Other marker, ${label}`) })).toBeVisible();
      await expect(panel.getByRole("button", { name: /Upload image|Replace image/ })).toHaveCount(0);
      await expect(panel.getByRole("button", { name: "+ Other" })).toHaveCount(0);
    }

    // --- The published revision is immutable for graphics at the API too, and the markers are unchanged.
    const published = await (await page.request.get(`/api/v1/catalog/revisions/${revisionId}`, { headers })).json();
    expect(published.lifecycle_status).toBe("published");
    const frontGraphic = published.graphics.find((g: { side: string }) => g.side === "front");
    expect(frontGraphic.markers.map((m: { label: string }) => m.label)).toEqual(["Outlet 1"]);
    const rejected = await page.request.post(`/api/v1/catalog/revisions/${revisionId}/graphics/${frontGraphic.id}/markers`, {
      headers: { ...headers, "If-Match": String(published.version) },
      data: { marker_type: "other", label: "late", marker_x: 0.5, marker_y: 0.5 },
    });
    expect([409, 422]).toContain(rejected.status());
    const afterReject = await (await page.request.get(`/api/v1/catalog/revisions/${revisionId}`, { headers })).json();
    expect(afterReject.graphics.find((g: { side: string }) => g.side === "front").markers).toHaveLength(1);
  });
});
