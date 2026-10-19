import { randomUUID } from "node:crypto";

import { APIRequestContext, expect, test } from "@playwright/test";

/** Issue #150 M6 — authenticated end-to-end coverage of the PDU catalog path against a
 * real backend + PostgreSQL: create a `pdu` catalog model through the designer UI,
 * configure its electrical specification and outlet templates, publish it, instantiate a
 * PDU from the published revision, connect one of its outlets into the existing power
 * topology, and verify the resulting graph in the topology view.
 *
 * Prerequisites are seeded over the API — the same prerequisite-building role
 * `tests/api/_phase2_helpers.py` plays for the backend's own HTTP tests: a manufacturer
 * and a published equipment revision with a power supply (instantiated so that a real
 * `equipment_power_input` node exists to connect an outlet to), plus an Operator account
 * for the cross-permission test. The browser is exercised for exactly what M5 adds.
 *
 * Requires a running backend at BACKEND_URL (default http://127.0.0.1:8000) and an
 * Administrator whose credentials come from E2E_ADMIN_EMAIL/E2E_ADMIN_PASSWORD. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";
const OPERATOR_EMAIL = process.env.E2E_OPERATOR_EMAIL ?? "e2e-operator@example.com";
const OPERATOR_PASSWORD = process.env.E2E_OPERATOR_PASSWORD ?? "E2eOperat0r!";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5173";

// This file builds a whole catalog model and walks the full power-topology flow in one
// browser session, and the first navigation into the Vite dev server also pays its
// on-demand transform cost — so it runs well past the suite's 30s default.
test.describe.configure({ timeout: 120_000 });

interface Seed {
  manufacturerId: string;
  manufacturerName: string;
  pduModelName: string;
  inletNodeId: string;
  inletLabel: string;
}

async function login(
  api: APIRequestContext,
  email = ADMIN_EMAIL,
  password = ADMIN_PASSWORD,
): Promise<string> {
  const resp = await api.post("/api/v1/auth/login", { data: { email, password } });
  expect(resp.ok(), `login failed for ${email}: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  return (await resp.json()).access_token as string;
}

/** A published `equipment` revision carrying one PSU, instantiated so a real
 * `equipment_power_input` PowerNode exists — the load side of the outlet connection. */
async function seedPublishedEquipmentInlet(
  api: APIRequestContext,
  token: string,
  suffix: string,
): Promise<{ inletNodeId: string; inletLabel: string }> {
  const headers = { Authorization: `Bearer ${token}` };

  const manufacturer = await (
    await api.post("/api/v1/catalog/manufacturers", { headers, data: { name: `E2E PDU Load Mfr ${suffix}` } })
  ).json();

  const model = await (
    await api.post("/api/v1/catalog/models", {
      headers,
      data: { manufacturer_id: manufacturer.id, category: "equipment", model_name: `E2E PDU Load ${suffix}` },
    })
  ).json();

  let revision = await (await api.post(`/api/v1/catalog/models/${model.id}/revisions`, { headers })).json();

  revision = await (
    await api.patch(`/api/v1/catalog/revisions/${revision.id}`, {
      headers: { ...headers, "If-Match": String(revision.version) },
      data: {
        dimension_unit: "mm", width_value: 440, height_value: 88.9, depth_value: 600, weight_unit: "kg",
        weight_value: 10, rack_unit_height: 2, supported_placement_types: ["rack_mounted"],
      },
    })
  ).json();

  await api.post(`/api/v1/catalog/revisions/${revision.id}/power-supplies`, {
    headers: { ...headers, "If-Match": String(revision.version) },
    data: { stable_key: "psu1", label: `PSU ${suffix}`, quantity: 1, connector_type: "C14" },
  });

  const publishResp = await api.post(`/api/v1/catalog/revisions/${revision.id}/publish`, { headers });
  expect(publishResp.ok(), `equipment publish failed: ${publishResp.status()} ${await publishResp.text()}`).toBeTruthy();

  const instantiateResp = await api.post("/api/v1/equipment/instantiate", {
    headers,
    data: { asset_tag: `E2E-LOAD-${suffix}`, catalog_model_revision_id: revision.id, hostname: `e2e-load-${suffix}` },
  });
  expect(
    instantiateResp.ok(),
    `equipment instantiate failed: ${instantiateResp.status()} ${await instantiateResp.text()}`,
  ).toBeTruthy();
  const instantiated = await instantiateResp.json();
  const inlet = instantiated.power_inlets?.[0];
  expect(inlet, "equipment instantiation must create a power inlet").toBeTruthy();
  return { inletNodeId: inlet.power_node_id as string, inletLabel: inlet.label as string };
}

/** Idempotent across re-runs: `.agents`-style CI fixtures are not reset between jobs. */
async function ensureOperatorAccount(api: APIRequestContext, token: string): Promise<void> {
  const resp = await api.post("/api/v1/users", {
    headers: { Authorization: `Bearer ${token}` },
    data: { email: OPERATOR_EMAIL, full_name: "E2E Operator", password: OPERATOR_PASSWORD, role_name: "Operator" },
  });
  expect([201, 409], `unexpected user-create status ${resp.status()}: ${await resp.text()}`).toContain(resp.status());
}

test.describe("Issue #150: PDU catalog workflow", () => {
  let seed: Seed;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const token = await login(api);
    const suffix = Math.random().toString(36).slice(2, 8);

    const inlet = await seedPublishedEquipmentInlet(api, token, suffix);
    await ensureOperatorAccount(api, token);

    seed = {
      manufacturerId: (await (await api.post("/api/v1/catalog/manufacturers", {
        headers: { Authorization: `Bearer ${token}` },
        data: { name: `E2E PDU Mfr ${suffix}` },
      })).json()).id as string,
      manufacturerName: `E2E PDU Mfr ${suffix}`,
      pduModelName: `E2E Rack PDU ${suffix}`,
      inletNodeId: inlet.inletNodeId,
      inletLabel: inlet.inletLabel,
    };
    await api.dispose();
  });

  test("create a PDU model, configure outlets, publish, instantiate, connect, verify topology", async ({ page }) => {
    const assetTag = `E2E-PDU-${Math.random().toString(36).slice(2, 7)}`;

    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // Client-side navigation throughout (nav links / in-app buttons), never page.goto or
    // page.reload after the initial /login: the access token lives only in memory.
    await page.getByRole("link", { name: "Asset Catalog" }).click();
    await expect(page).toHaveURL("/admin/catalog");
    await expect(page.getByRole("heading", { name: "Asset Catalog" })).toBeVisible();

    // --- 1. PDU model creation through the designer UI: the category select offers `pdu`.
    await page.getByRole("button", { name: "New Model" }).click();
    const categorySelect = page.getByRole("combobox", { name: "Model category" });
    await expect(categorySelect.locator('option[value="pdu"]')).toHaveCount(1);
    await page.getByRole("textbox", { name: "Search manufacturers" }).fill(seed.manufacturerName);
    await page.getByRole("combobox", { name: "Model manufacturer" }).selectOption(seed.manufacturerId);
    await categorySelect.selectOption("pdu");
    await page.getByRole("textbox", { name: "Model name" }).fill(seed.pduModelName);
    await page.getByRole("button", { name: "Create Model" }).click();

    // --- 2. Open the new model (filter the list so the row is on screen) and draft it.
    await page.getByRole("textbox", { name: "Search models" }).fill(seed.pduModelName);
    await page.getByRole("link", { name: seed.pduModelName }).click();
    await expect(page).toHaveURL(/\/admin\/catalog\/models\//);
    await page.getByRole("button", { name: "New Draft" }).click();
    await expect(page).toHaveURL(/\/admin\/catalog\/revisions\//);

    // --- 3. Electrical specification + outlet templates (the PDU editor is draft-only).
    const pduSpec = page.locator('[aria-label="PDU electrical specification"]');
    await expect(pduSpec).toBeVisible();
    await expect(pduSpec).toContainText("no rack-placement API");
    await pduSpec.getByRole("spinbutton", { name: "Rated input voltage (V)" }).fill("230");
    await pduSpec.getByRole("spinbutton", { name: "Rated input current (A)" }).fill("32");
    await pduSpec.getByRole("combobox", { name: "Phase" }).selectOption("single");
    await pduSpec.getByRole("combobox", { name: "Mounting orientation" }).selectOption("vertical");

    await pduSpec.getByRole("button", { name: "+ Add outlet" }).click();
    await pduSpec.getByRole("spinbutton", { name: "Outlet 1 rated voltage" }).fill("230");
    await pduSpec.getByRole("spinbutton", { name: "Outlet 1 rated current" }).fill("10");
    await pduSpec.getByRole("button", { name: "+ Add outlet" }).click();
    await pduSpec.getByRole("spinbutton", { name: "Outlet 2 rated voltage" }).fill("230");
    await pduSpec.getByRole("spinbutton", { name: "Outlet 2 rated current" }).fill("16");
    await expect(pduSpec.getByTestId("pdu-outlet-row")).toHaveCount(2);

    const versionBefore = await pduSpec.locator("span").filter({ hasText: "Saved against If-Match version" }).innerText();
    await pduSpec.getByRole("button", { name: "Save PDU specification" }).click();
    await expect(pduSpec.getByRole("button", { name: "Save PDU specification" })).toBeEnabled();
    // A successful save is proven server-side: /validate re-runs the M1 rules over the
    // stored rows, so it can only report Ready once the PATCH persisted.
    await expect(pduSpec.locator("span").filter({ hasText: "Saved against If-Match version" })).not.toHaveText(
      versionBefore,
    );
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    await expect(page.getByText("Ready to publish.")).toBeVisible();

    // --- 4. Publish; the draft-only PDU editor disappears and the (published-gated)
    //        instantiation panel appears.
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(page.locator("span").filter({ hasText: /^published$/ }).first()).toBeVisible();
    await expect(page.locator('[aria-label="PDU electrical specification"]')).toHaveCount(0);
    const instantiatePanel = page.locator('[aria-label="Instantiate PDU"]');
    await expect(instantiatePanel).toBeVisible();

    // --- 5. Instantiate a PDU from the published revision (power:manage).
    await instantiatePanel.getByRole("textbox", { name: "Asset tag" }).fill(assetTag);
    await instantiatePanel.getByRole("textbox", { name: "Name" }).fill("E2E Rack PDU");
    await instantiatePanel.getByRole("button", { name: "Instantiate PDU" }).click();

    const result = page.locator('[aria-label="PDU instantiation result"]');
    await expect(result).toBeVisible();
    await expect(result).toContainText("with 2 outlets");
    // Instantiated outlets are displayed with their power-node identities.
    const outletRow1 = result.locator("li").filter({ hasText: "Outlet 1" });
    const outletNodeId = (await outletRow1.locator("span.font-mono").innerText()).trim();
    expect(outletNodeId).toMatch(/^[0-9a-f-]{36}$/i);
    await expect(result.locator("li")).toHaveCount(2);

    // --- 6. Connect the outlet into the existing power topology.
    await result.getByRole("link", { name: /Open Power Topology/ }).click();
    await expect(page).toHaveURL("/power");

    const outletsPanel = page.locator('[aria-label="PDU outlets"]');
    // Identify the row by its power-node id (unique per instantiation) rather than by a
    // label-based count, so the assertion does not depend on how many PDU outlets the
    // database already holds.
    const topologyOutletRow = outletsPanel.getByTestId("pdu-outlet-node-row").filter({ hasText: outletNodeId });
    await expect(topologyOutletRow).toHaveCount(1);
    await expect(topologyOutletRow).toContainText("Outlet 1");

    await topologyOutletRow.getByRole("button", { name: "Connect outlet Outlet 1" }).click();
    const connectForm = page.locator('[aria-label="Connect PDU outlet"]');
    await connectForm.getByLabel("Target node").selectOption(seed.inletNodeId);
    await connectForm.getByRole("button", { name: "Create connection" }).click();
    await expect(connectForm).toHaveCount(0);

    // --- 7. Topology visualization: the load appears downstream of the selected outlet.
    await topologyOutletRow.getByRole("button", { name: "Outlet 1", exact: true }).click();
    await expect(page.getByTestId("power-downstream")).toContainText(seed.inletLabel);

    // Cross-check the real graph (not just the rendered view) over the API.
    const apiToken = await login(page.request);
    const connections = await (
      await page.request.get(`/api/v1/power/connections?node_id=${outletNodeId}`, {
        headers: { Authorization: `Bearer ${apiToken}` },
      })
    ).json();
    const edge = (connections.items ?? []).find(
      (c: { source_node_id: string; target_node_id: string }) =>
        c.source_node_id === outletNodeId && c.target_node_id === seed.inletNodeId,
    );
    expect(edge, "the outlet → equipment-inlet PowerConnection must exist").toBeTruthy();
    expect(edge.feed_label).toBe("single");
  });

  test("cross-permission: an Operator may read PDU outlets but not manage power", async ({ page, playwright }) => {
    // Rejection happens on the real routes, before any resource lookup.
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const operatorToken = await login(api, OPERATOR_EMAIL, OPERATOR_PASSWORD);

    const me = await (
      await api.get("/api/v1/auth/me", { headers: { Authorization: `Bearer ${operatorToken}` } })
    ).json();
    expect(me.permission_codes).toContain("power:read");
    expect(me.permission_codes).not.toContain("power:manage");

    const instantiateResp = await api.post("/api/v1/power/pdus/instantiate", {
      headers: { Authorization: `Bearer ${operatorToken}` },
      data: { asset_tag: "E2E-DENY", name: "E2E Deny", catalog_model_revision_id: randomUUID() },
    });
    expect(instantiateResp.status(), "PDU instantiation requires power:manage").toBe(403);

    const connectResp = await api.post("/api/v1/power/connections", {
      headers: { Authorization: `Bearer ${operatorToken}` },
      data: { source_node_id: randomUUID(), target_node_id: randomUUID() },
    });
    expect(connectResp.status(), "connection creation requires power:manage").toBe(403);
    await api.dispose();

    // In the browser the same Operator sees the outlets panel read-only: no connect
    // controls at all, and no catalog-designer navigation.
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(OPERATOR_EMAIL);
    await page.locator('input[type="password"]').fill(OPERATOR_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");
    await expect(page.getByRole("link", { name: "Asset Catalog" })).toHaveCount(0);

    await page.getByRole("link", { name: "Power Topology" }).click();
    await expect(page).toHaveURL("/power");
    const outletsPanel = page.locator('[aria-label="PDU outlets"]');
    await expect(outletsPanel.getByText("Read-only")).toBeVisible();
    await expect(outletsPanel.getByRole("button", { name: /^Connect outlet / })).toHaveCount(0);
  });
});
