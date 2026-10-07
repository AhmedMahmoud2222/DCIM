import { createHmac, randomBytes } from "node:crypto";

import { APIRequestContext, expect, test } from "@playwright/test";

/** Issue #101 end to end against the real backend: profile → integration → signed collector
 * discovery → operator reconciliation → physical cable → trace. Only inventory scaffolding and the
 * collector's own signed requests go through the API (the collector is not a browser user); every
 * operator decision is made in the UI, and the outcome is re-read from the API. */

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
  const manufacturer = await ok<{ id: string }>(await api.post("/api/v1/catalog/manufacturers", { headers, data: { name: `Net Acme ${suffix}` } }));
  const model = await ok<{ id: string }>(
    await api.post("/api/v1/catalog/models", { headers, data: { manufacturer_id: manufacturer.id, category: "equipment", model_name: `Switch ${suffix}` } }),
  );
  let revision = await ok<{ id: string; version: number }>(await api.post(`/api/v1/catalog/models/${model.id}/revisions`, { headers }));
  revision = await ok(
    await api.patch(`/api/v1/catalog/revisions/${revision.id}`, {
      headers: { ...headers, "If-Match": String(revision.version) },
      data: { dimension_unit: "mm", width_value: 440, height_value: 44.45, depth_value: 600, weight_unit: "kg", weight_value: 10, rack_unit_height: 1, supported_placement_types: ["rack_mounted"] },
    }),
  );
  let version = revision.version;
  for (const name of ["Eth1/1", "Eth1/24"]) {
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

function signedHeaders(collectorId: string, secret: string, body: string): Record<string, string> {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = randomBytes(12).toString("hex");
  const signature = createHmac("sha256", secret).update(`${collectorId}.${timestamp}.${nonce}.${body}`).digest("hex");
  return {
    "Content-Type": "application/json", "X-Collector-Id": collectorId, "X-Collector-Timestamp": timestamp,
    "X-Collector-Nonce": nonce, "X-Collector-Signature": signature,
  };
}

test.describe("Issue #101: profile, discovery, reconciliation, cable and trace", () => {
  const localHost = `edge-sw-${suffix}`;
  const remoteHost = `core-sw-${suffix}`;
  const octet = () => 1 + Math.floor(Math.random() * 250);
  const localIp = `10.${octet()}.${octet()}.${octet()}`;
  const remoteIp = `10.${octet()}.${octet()}.${octet()}`;
  const cableLabel = `E2E-${suffix}`;
  let collector: { id: string; secret: string };
  let integrationId: string;

  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const headers = await login(api);
    const revisionId = await publishRevision(api, headers);
    const local = await ok<{ id: string }>(await api.post("/api/v1/equipment/instantiate", { headers, data: { asset_tag: `NET-L-${suffix}`, catalog_model_revision_id: revisionId, hostname: localHost, ip_address: localIp } }));
    await ok(await api.post("/api/v1/equipment/instantiate", { headers, data: { asset_tag: `NET-R-${suffix}`, catalog_model_revision_id: revisionId, hostname: remoteHost, ip_address: remoteIp } }));

    collector = await ok(await api.post("/api/v1/collectors", { headers, data: { name: `net-collector-${suffix}`, collector_type: "central" } }));
    expect((await api.post(`/api/v1/collectors/${collector.id}/capabilities`, { headers, data: { protocol_codes: ["snmp"] } })).status()).toBe(204);
    const integration = await ok<{ id: string }>(
      await api.post("/api/v1/integrations", {
        headers,
        data: {
          name: `net-int-${suffix}`, integration_type: "snmp", target_host: localIp,
          snmpv3: { username: "poller", auth_protocol: "sha256", auth_secret: `auth-secret-${suffix}`, priv_protocol: "aes128", priv_secret: `priv-secret-${suffix}` },
        },
      }),
    );
    integrationId = integration.id;
    await ok(await api.post(`/api/v1/collectors/${collector.id}/assignments`, { headers, data: { integration_id: integrationId } }));

    // The collector reports the device it polled; an operator reconciles it to the local switch.
    const deviceBatch = JSON.stringify({
      batch_id: randomBytes(8).toString("hex"),
      records: [{ dedup_key: randomBytes(8).toString("hex"), integration_id: integrationId, external_identifier: localIp, occurred_at: new Date().toISOString(), raw_attributes: { facts: { sys_object_id: "1.3.6.1.4.1.9.1.1" } } }],
    });
    await ok(await api.post(`/api/v1/collectors/${collector.id}/ingest`, { headers: signedHeaders(collector.id, collector.secret, deviceBatch), data: deviceBatch }));
    const devices = await ok<Array<{ id: string; integration_id: string }>>(await api.get("/api/v1/discovery/devices", { headers }));
    const device = devices.find((d) => d.integration_id === integrationId)!;
    const diffs = await ok<Array<{ id: string; discovered_device_id: string }>>(await api.get("/api/v1/discovery/reconciliation", { headers }));
    const diff = diffs.find((d) => d.discovered_device_id === device.id)!;
    await ok(await api.post(`/api/v1/discovery/reconciliation/${diff.id}/accept`, { headers, data: { matched_managed_asset_id: local.id } }));
    await api.dispose();
  });

  test("operator reviews evidence, confirms it, records the cable and traces it", async ({ page, playwright }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");

    // 1. Profile: create from a reviewed template, then ask the matcher about a Cisco sysObjectID.
    await page.getByRole("link", { name: "Network Profiles" }).click();
    await expect(page.getByRole("heading", { name: "Vendor & Device Profiles" })).toBeVisible();
    if ((await page.getByText("Cisco", { exact: true }).count()) === 0) {
      await page.getByLabel("Profile template").selectOption({ label: "Cisco IOS/NX-OS style device (CDP + LLDP)" });
      await page.getByRole("button", { name: "Create profiles" }).click();
    }
    await expect(page.getByText("Cisco", { exact: true }).first()).toBeVisible();
    await page.getByLabel("sys_object_id").fill("1.3.6.1.4.1.9.1.1");
    await page.getByRole("button", { name: "Test match" }).click();
    await expect(page.getByRole("status")).toContainText("matched");
    await expect(page.getByRole("status")).toContainText("Cisco switch");

    // Bind the profile to the integration (explicit operator action) and confirm the collector's plan carries it.
    const api = await playwright.request.newContext({ baseURL: BASE_URL });
    const headers = await login(api);
    const profiles = await ok<Array<{ id: string; code: string }>>(await api.get("/api/v1/network-profiles/devices", { headers }));
    const profile = profiles.find((p) => p.code === "cisco-switch")!;
    const integration = await ok<{ version: number }>(await api.get(`/api/v1/integrations/${integrationId}`, { headers }));
    await ok(await api.patch(`/api/v1/integrations/${integrationId}`, { headers: { ...headers, "If-Match": String(integration.version) }, data: { device_profile_id: profile.id } }));
    const plan = await ok<Array<{ integration_id: string; plan: { neighbor_discovery: { lldp: { enabled: boolean } } } | null }>>(
      await api.get(`/api/v1/collectors/${collector.id}/discovery-plan`, { headers: signedHeaders(collector.id, collector.secret, "") }),
    );
    expect(plan.find((p) => p.integration_id === integrationId)?.plan?.neighbor_discovery.lldp.enabled).toBe(true);

    // 2. Discovery: the collector reports an LLDP neighbor (signed, as a real collector would).
    const neighborBatch = JSON.stringify({
      batch_id: randomBytes(8).toString("hex"),
      records: [{
        dedup_key: randomBytes(8).toString("hex"), integration_id: integrationId, external_identifier: localIp, occurred_at: new Date().toISOString(), record_type: "neighbor",
        raw_attributes: { scan_id: "e2e-scan", neighbor: {
          protocol: "lldp", local_port: { name: "Eth1/1", ref: "1" },
          remote: { chassis_id: remoteHost, chassis_id_subtype: "local", port_id: "Eth1/24", port_id_subtype: "interface_name", system_name: remoteHost, management_address: remoteIp },
          capabilities: ["bridge"], raw: { sys_name: remoteHost },
        } },
      }],
    });
    const ingested = await ok<{ results: Array<{ status: string }> }>(
      await api.post(`/api/v1/collectors/${collector.id}/ingest`, { headers: signedHeaders(collector.id, collector.secret, neighborBatch), data: neighborBatch }),
    );
    expect(ingested.results[0].status).toBe("accepted");

    // 3. Reconciliation in the UI: evidence is proposed (matched by management address), not authoritative.
    await page.getByRole("link", { name: "Neighbors" }).click();
    await page.getByRole("button", { name: new RegExp(`Review neighbor ${remoteHost}`) }).click();
    await expect(page.getByLabel("Neighbor detail")).toContainText("proposed");
    await expect(page.getByLabel("Neighbor detail")).toContainText("Waiting for an operator");
    expect((await ok<{ total: number }>(await api.get("/api/v1/cables", { headers }))).total).toBe(0);
    await page.getByRole("button", { name: "Confirm proposal" }).click();
    await expect(page.getByLabel("Neighbor detail")).toContainText("An operator linked the ports");
    expect((await ok<{ total: number }>(await api.get("/api/v1/cables", { headers }))).total).toBe(0); // confirming is not cabling

    // 4. Physical cable: an explicit second action.
    await page.getByLabel("Cable label").fill(cableLabel);
    await page.getByRole("button", { name: "Record cable" }).click();
    await expect(page.getByRole("status")).toContainText(`Cable ${cableLabel} recorded`);

    // 5. Cables list and trace.
    await page.getByRole("link", { name: "Cables", exact: true }).click();
    const row = page.getByRole("row", { name: new RegExp(cableLabel) });
    await expect(row).toContainText(`${localHost} · Eth1/1`);
    await expect(row).toContainText(`${remoteHost} · Eth1/24`);
    await expect(row).toContainText("installed");
    await expect(row).toContainText("from confirmed discovery");
    await page.getByRole("link", { name: `Trace cable ${cableLabel}` }).click();
    await expect(page.getByRole("region", { name: "Recorded path" })).toContainText(localHost);
    await expect(page.getByRole("region", { name: "Recorded path" })).toContainText(cableLabel);
    await expect(page.getByRole("region", { name: "Recorded path" })).toContainText(remoteHost);
    await expect(page.getByRole("region", { name: "Discovery evidence" })).toContainText("Discovery agrees with the recorded cable");

    const cables = await ok<{ items: Array<{ label: string; source: string; status: string }> }>(await api.get("/api/v1/cables", { headers }));
    expect(cables.items.find((c) => c.label === cableLabel)).toMatchObject({ source: "discovery_confirmed", status: "installed" });
    await api.dispose();
  });
});
