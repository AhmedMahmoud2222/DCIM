import { execFileSync } from "node:child_process";
import { createHmac } from "node:crypto";
import path from "node:path";

import { APIRequestContext, expect, test } from "@playwright/test";

import { MockProvider, startMockProvider } from "./fixtures/mockProvider";

/** Issue #103 E2E: collector offline -> correlated incident -> webhook (with an injected provider outage and
 * its retry) -> ServiceNow-compatible ticket -> recovery, through the real API, Celery worker and browser.
 * The provider is a local mock (no external tenant). Alarm rows and the aged heartbeat are created by
 * backend/scripts/e2e_ops_driver.py because the telemetry pipeline would need five real minutes; everything
 * after that point (sweep, correlation, queueing, delivery, ticketing, UI) is the production code path. */

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";
const PORT = Number(process.env.E2E_MOCK_PORT ?? 18089);
const SIGNING_SECRET = "e2e-signing-secret";
const BACKEND_DIR = path.resolve(process.cwd(), "../backend");

function driver(...args: string[]): Record<string, unknown> {
  const out = execFileSync(process.env.E2E_PYTHON ?? "python", ["scripts/e2e_ops_driver.py", ...args], {
    cwd: BACKEND_DIR, env: process.env, encoding: "utf-8",
  });
  return JSON.parse(out.trim().split("\n").pop() ?? "{}");
}

async function authed(api: APIRequestContext) {
  const login = await api.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(login.ok()).toBeTruthy();
  return { Authorization: `Bearer ${(await login.json()).access_token}` };
}

test("collector offline becomes one correlated incident with delivery and ticket history, then recovers", async ({ page, request }) => {
  test.setTimeout(120_000);
  const provider: MockProvider = await startMockProvider(PORT);
  try {
    const headers = await authed(request);
    const sfx = Math.random().toString(36).slice(2, 8);
    const post = async (url: string, data: unknown) => {
      const r = await request.post(`/api/v1${url}`, { headers, data });
      expect(r.ok(), `${url}: ${r.status()} ${await r.text()}`).toBeTruthy();
      return r.json();
    };
    const org = await post("/organizations", { name: `E2E-103 Org ${sfx}` });
    const country = await post("/countries", { organization_id: org.id, name: "Testland", iso_code: "TL" });
    const city = await post("/cities", { country_id: country.id, name: "Testville" });
    const site = await post("/sites", { city_id: city.id, code: `E103${sfx}`.slice(0, 10), name: `E2E-103 Site ${sfx}` });

    const channel = await post("/operations/notification-channels", {
      name: `hook-${sfx}`, url: `http://127.0.0.1:${PORT}/hook`, signing_secret: SIGNING_SECRET,
    });
    expect(JSON.stringify(channel)).not.toContain(SIGNING_SECRET);
    await post("/operations/notification-policies", {
      name: `policy-${sfx}`, channel_id: channel.id, event_types: ["incident.opened", "collector.offline", "collector.online"], site_id: site.id,
    });
    await post("/operations/itsm-connections", {
      name: `snow-${sfx}`, base_url: `http://127.0.0.1:${PORT}`, username: "svc_dcim", password: "e2e-snow-password", auto_create: true, site_id: site.id,
    });

    const seeded = driver("seed", "--site-id", site.id, "--label", sfx) as { collector_id: string; collector_name: string; alarm_ids: string[] };
    provider.failNextWebhooks(1); // one outage the retry logic must survive
    driver("tick");

    await page.goto("/login");
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL("/");
    await page.getByRole("link", { name: "Events", exact: true }).click();

    // Collector offline is visible with its transition.
    await page.getByRole("tab", { name: "Collector health" }).click();
    await expect(page.getByTestId(`collector-state-${seeded.collector_name}`)).toHaveText("Offline");
    await expect(page.getByRole("table", { name: "Transition history" }).getByRole("row", { name: new RegExp(seeded.collector_name) })).toContainText("unknown to offline");

    // The two alarms became exactly one incident whose cause is the collector.
    await page.getByRole("tab", { name: "Incidents" }).click();
    const cause = `Collector ${seeded.collector_name} offline`;
    await page.getByRole("button", { name: cause }).click();
    const panel = page.getByRole("region", { name: "Incident detail" });
    await expect(panel.getByTestId("rationale")).toContainText(seeded.collector_name);
    const sources = panel.getByRole("table", { name: "Source events" });
    await expect(sources.getByRole("row")).toHaveCount(4); // header + collector cause + two alarms
    await expect(sources).toContainText(`e2e-${sfx}.device-0.availability`);

    // Ticket created once, with the remote reference shown in DCIM.
    await expect(panel.getByRole("table", { name: "ITSM tickets" })).toContainText(/INC\d{7}/, { timeout: 30_000 });
    await expect.poll(() => Object.keys(provider.tickets).length, { timeout: 30_000 }).toBe(1);

    // The injected outage is visible, then cleared by the retry; every message arrives exactly once.
    await expect(panel.getByText(/Provider unavailable \(HTTP_5XX\): retrying/).or(panel.getByText("Delivered")).first()).toBeVisible({ timeout: 30_000 });
    driver("release-retries");
    driver("tick");
    await expect.poll(() => provider.webhooks.length, { timeout: 40_000 }).toBe(2);
    const events = provider.webhooks.map((w) => JSON.parse(w.body).event).sort();
    expect(events).toEqual(["collector.offline", "incident.opened"]);
    for (const w of provider.webhooks) {
      const expected = createHmac("sha256", SIGNING_SECRET).update(`${w.timestamp}.${w.body}`).digest("hex");
      expect(w.signature).toBe(`sha256=${expected}`);
    }
    await page.getByRole("tab", { name: "Notifications" }).click();
    await expect(page.getByRole("table", { name: "Delivery history" })).toBeVisible();
    await page.getByRole("tab", { name: "Incidents" }).click();
    await page.getByRole("button", { name: cause }).click();
    await expect(page.getByRole("region", { name: "Incident detail" }).getByRole("table", { name: "Notifications" })).not.toContainText("retrying", { timeout: 20_000 });

    // Re-running the sweeps changes nothing: no second incident, ticket or message.
    driver("tick");
    driver("tick");
    await page.waitForTimeout(1500);
    expect(Object.keys(provider.tickets)).toHaveLength(1);
    expect(provider.webhooks).toHaveLength(2);
    const incidents = await (await request.get(`/api/v1/operations/incidents?limit=100`, { headers })).json();
    expect(incidents.items.filter((i: { cause_label: string }) => i.cause_label === cause)).toHaveLength(1);

    // Resolving in DCIM updates the ticket and leaves the source alarms alone.
    await page.getByRole("button", { name: cause }).click();
    await page.getByRole("button", { name: "Resolve incident" }).click();
    await expect.poll(() => Object.values(provider.tickets)[0]?.description ?? "", { timeout: 40_000 }).toContain("Status in DCIM: resolved");
    const active = await (await request.get(`/api/v1/alarms?status=ACTIVE&limit=1000`, { headers })).json();
    for (const id of seeded.alarm_ids) expect(active.map((a: { id: string }) => a.id)).toContain(id);

    // Recovery: a fresh heartbeat flips the collector back online and notifies once.
    driver("heartbeat", "--collector-id", seeded.collector_id, "--age-seconds", "0");
    driver("tick");
    await page.getByRole("tab", { name: "Collector health" }).click();
    await expect(page.getByTestId(`collector-state-${seeded.collector_name}`)).toHaveText("Online", { timeout: 20_000 });
    await expect(page.getByRole("table", { name: "Transition history" }).getByRole("row", { name: new RegExp(seeded.collector_name) }).first()).toContainText("offline to online");
    await expect.poll(() => provider.webhooks.map((w) => JSON.parse(w.body).event).includes("collector.online"), { timeout: 40_000 }).toBe(true);
  } finally {
    await provider.close();
  }
});
