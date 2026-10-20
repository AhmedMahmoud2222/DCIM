import { APIRequestContext, expect, test } from "@playwright/test";

/** Issue #101 B5 end to end against the real backend: a path that crosses a patch panel.
 *
 *   switch A Eth1/1 --cable 1-- patch panel Front01 =pass-through= Rear01 --cable 2-- switch B Eth1/24
 *
 * Inventory scaffolding goes through the API; the pass-through, both cables and the trace are made in the UI,
 * and the trace is checked in both directions and against the API response. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";
const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5173";
const suffix = Math.random().toString(36).slice(2, 8);

async function login(api: APIRequestContext): Promise<{ Authorization: string }> {
  const resp = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(resp.ok(), `login failed: ${resp.status()}`).toBeTruthy();
  return { Authorization: `Bearer ${(await resp.json()).access_token}` };
}

async function ok<T>(resp: Awaited<ReturnType<APIRequestContext["post"]>>): Promise<T> {
  expect(resp.ok(), `${resp.url()} -> ${resp.status()} ${await resp.text()}`).toBeTruthy();
  return (await resp.json()) as T;
}

async function publishRevision(api: APIRequestContext, headers: Record<string, string>): Promise<string> {
  const manufacturer = await ok<{ id: string }>(await api.post("/api/v1/catalog/manufacturers", { headers, data: { name: `Hop Acme ${suffix}` } }));
  const model = await ok<{ id: string }>(
    await api.post("/api/v1/catalog/models", { headers, data: { manufacturer_id: manufacturer.id, category: "equipment", model_name: `Hop ${suffix}` } }),
  );
  let revision = await ok<{ id: string; version: number }>(await api.post(`/api/v1/catalog/models/${model.id}/revisions`, { headers }));
  revision = await ok(
    await api.patch(`/api/v1/catalog/revisions/${revision.id}`, {
      headers: { ...headers, "If-Match": String(revision.version) },
      data: { dimension_unit: "mm", width_value: 440, height_value: 44.45, depth_value: 600, weight_unit: "kg", weight_value: 10, rack_unit_height: 1, supported_placement_types: ["rack_mounted"] },
    }),
  );
  let version = revision.version;
  for (const name of ["Eth1/1", "Eth1/24", "Front01", "Rear01"]) {
    const port = await ok<{ revision_version: number }>(
      await api.post(`/api/v1/catalog/revisions/${revision.id}/network-ports`, {
        headers: { ...headers, "If-Match": String(version) },
        data: { stable_key: name.toLowerCase().replace("/", "-"), display_name: name, media_type: "copper", supported_speeds_mbps: [1000], connector_type: "rj45", side: "rear" },
      }),
    );
    version = port.revision_version;
  }
  await ok(await api.post(`/api/v1/catalog/revisions/${revision.id}/publish`, { headers }));
  return revision.id;
}

test.describe("Issue #101 B5: multi-hop trace through a patch panel", () => {
  const swA = { host: `hop-swa-${suffix}`, tag: `HOP-A-${suffix}` };
  const panel = { host: `hop-pp-${suffix}`, tag: `HOP-P-${suffix}` };
  const swB = { host: `hop-swb-${suffix}`, tag: `HOP-B-${suffix}` };
  const cable1 = `HOP-C1-${suffix}`;
  const cable2 = `HOP-C2-${suffix}`;
  const ptLabel = `HOP-PT-${suffix}`;
  const option = (e: { host: string; tag: string }) => `${e.host} (${e.tag})`;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const headers = await login(api);
    const revisionId = await publishRevision(api, headers);
    for (const e of [swA, panel, swB]) {
      await ok(await api.post("/api/v1/equipment/instantiate", { headers, data: { asset_tag: e.tag, catalog_model_revision_id: revisionId, hostname: e.host } }));
    }
    await api.dispose();
  });

  async function recordCable(page: import("@playwright/test").Page, label: string, a: [typeof swA, string], b: [typeof swA, string]) {
    await page.getByLabel("Label", { exact: true }).fill(label);
    await page.getByLabel("Endpoint A equipment").selectOption({ label: option(a[0]) });
    await page.getByLabel("Endpoint A port").selectOption({ label: a[1] });
    await page.getByLabel("Endpoint B equipment").selectOption({ label: option(b[0]) });
    await page.getByLabel("Endpoint B port").selectOption({ label: b[1] });
    await page.getByLabel("Initial status").selectOption("installed");
    await page.getByRole("button", { name: "Record cable" }).click();
    await expect(page.getByRole("row", { name: new RegExp(label) })).toBeVisible();
  }

  async function trace(page: import("@playwright/test").Page, from: typeof swA, port: string) {
    await page.getByRole("link", { name: "Trace", exact: true }).click();
    await page.getByLabel("Start port equipment").selectOption({ label: option(from) });
    await page.getByLabel("Start port port").selectOption({ label: port });
    await page.getByRole("button", { name: "Trace", exact: true }).click();
    await expect(page.getByTestId("trace-termination")).toBeVisible();
    return (await page.getByRole("region", { name: "Recorded path" }).textContent()) ?? "";
  }

  function expectInOrder(text: string, tokens: string[]) {
    let from = 0;
    for (const token of tokens) {
      const at = text.indexOf(token, from);
      expect(at, `"${token}" should appear after position ${from} in: ${text}`).toBeGreaterThanOrEqual(from);
      from = at + token.length;
    }
  }

  test("records the pass-through and both cables in the UI, then shows the full ordered path both ways", async ({ page, playwright }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // 1. The pass-through inside the patch panel.
    await page.getByRole("link", { name: "Pass-throughs" }).click();
    await expect(page.getByRole("heading", { name: "Pass-throughs" })).toBeVisible();
    await page.getByLabel("Pass-through device").selectOption({ label: option(panel) });
    await page.getByLabel("Pass-through port a").selectOption({ label: "Front01" });
    await page.getByLabel("Pass-through port b").selectOption({ label: "Rear01" });
    await page.getByLabel("Label (optional)").fill(ptLabel);
    await page.getByRole("button", { name: "Record pass-through" }).click();
    const row = page.getByRole("row", { name: new RegExp(ptLabel) });
    await expect(row).toContainText("Front01 ↔ Rear01");
    await expect(row).toContainText(panel.host);

    // 2. Cable 1 (switch A to the panel's front) and cable 2 (the panel's rear to switch B).
    await page.getByRole("link", { name: "Cables", exact: true }).click();
    await recordCable(page, cable1, [swA, "Eth1/1"], [panel, "Front01"]);
    await recordCable(page, cable2, [panel, "Rear01"], [swB, "Eth1/24"]);

    // 3. Trace from switch A: every hop, in order, through the patch panel.
    const forward = await trace(page, swA, "Eth1/1");
    expectInOrder(forward, [swA.host, cable1, panel.host, "Front01", "Passes through", ptLabel, "Rear01", cable2, swB.host, "Eth1/24"]);
    await expect(page.getByTestId("trace-step")).toHaveCount(2);
    await expect(page.getByTestId("trace-pass-through")).toHaveCount(1);
    await expect(page.getByTestId("trace-termination")).toContainText("End of path");

    // 4. The reverse direction is the mirror image.
    const reverse = await trace(page, swB, "Eth1/24");
    expectInOrder(reverse, [swB.host, cable2, panel.host, "Rear01", "Passes through", ptLabel, "Front01", cable1, swA.host, "Eth1/1"]);
    await expect(page.getByTestId("trace-termination")).toContainText("End of path");

    // 5. The UI shows what the API reports, and the pass-through is the stored relationship (not cable metadata).
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const headers = await login(api);
    const equipment = await ok<{ items: Array<{ id: string; hostname: string }> }>(await api.get("/api/v1/equipment?limit=200", { headers }));
    const a = equipment.items.find((e) => e.hostname === swA.host)!;
    const ports = await ok<{ ports: Array<{ id: string; display_name: string }> }>(await api.get(`/api/v1/equipment/${a.id}/ports`, { headers }));
    const start = ports.ports.find((p) => p.display_name === "Eth1/1")!;
    const apiTrace = await ok<{ format: string; terminated: string; hop_count: number; path: Array<{ link: { cable: { label: string } }; pass_through: { label: string } | null }> }>(
      await api.get(`/api/v1/topology/ports/${start.id}/trace`, { headers }),
    );
    expect(apiTrace).toMatchObject({ format: "trace.v2", terminated: "end_of_path", hop_count: 2 });
    expect(apiTrace.path.map((p) => p.link.cable.label)).toEqual([cable1, cable2]);
    expect(apiTrace.path[0].pass_through?.label).toBe(ptLabel);
    const stored = await ok<{ items: Array<{ label: string }> }>(await api.get("/api/v1/pass-throughs", { headers }));
    expect(stored.items.map((i) => i.label)).toContain(ptLabel);
    await api.dispose();
  });
});
