import { expect, test } from "@playwright/test";

// A real native-text PDF, generated with a correct byte-offset xref table.
function datasheetPdf(model: string): Buffer {
  const stream = `BT /F1 12 Tf 72 720 Td (${model} Technical Specifications) Tj 0 -24 Td (Typical power consumption: 2 kW) Tj ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ];
  let pdf = "%PDF-1.4\n"; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n => `${String(n).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

test("datasheet upload -> extract -> review -> explicit apply -> persisted draft and provenance", async ({ page, request }) => {
  test.setTimeout(120_000);
  const email = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
  const password = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";
  const login = await request.post("/api/v1/auth/login", { data: { email, password } });
  expect(login.ok()).toBeTruthy();
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const suffix = Math.random().toString(36).slice(2, 8);
  const manufacturerResponse = await request.post("/api/v1/catalog/manufacturers", { headers, data: { name: `Extraction E2E ${suffix}` } });
  expect(manufacturerResponse.status()).toBe(201);
  const modelName = `CX-${suffix}`;
  const modelResponse = await request.post("/api/v1/catalog/models", { headers,
    data: { manufacturer_id: (await manufacturerResponse.json()).id, category: "equipment", model_name: modelName } });
  expect(modelResponse.status()).toBe(201);
  const draftResponse = await request.post(`/api/v1/catalog/models/${(await modelResponse.json()).id}/revisions`, { headers });
  expect(draftResponse.status()).toBe(201);
  const draft = await draftResponse.json();
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login$/);
  // SPA navigation preserves the in-memory access token.
  await page.evaluate((id: string) => { window.history.pushState({}, "", `/admin/catalog/revisions/${id}`); window.dispatchEvent(new PopStateEvent("popstate")); }, draft.id);
  const panel = page.getByRole("region", { name: "Datasheets" });
  await panel.getByLabel("Upload PDF datasheet").setInputFiles({ name: "native-datasheet.pdf", mimeType: "application/pdf", buffer: datasheetPdf(modelName) });
  await expect(panel.getByLabel("Attached datasheet")).not.toHaveValue("");
  await panel.getByRole("button", { name: "Extract datasheet", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Accept power_typical_w", exact: true })).toBeVisible({ timeout: 60_000 });
  const candidate = panel.getByRole("article", { name: "power_typical_w candidate" });
  await expect(candidate).toContainText("Page 1:");
  await expect(candidate).toContainText("2 kW");
  if (await candidate.getByLabel("I confirm this value belongs to the target model.").isVisible()) {
    await candidate.getByLabel("I confirm this value belongs to the target model.").check();
  }
  await candidate.getByLabel("Review note for power_typical_w").fill("Verified against original datasheet");
  await candidate.getByRole("button", { name: "Accept power_typical_w", exact: true }).click();
  const before = await request.get(`/api/v1/catalog/revisions/${draft.id}`, { headers });
  expect((await before.json()).typical_power_w).toBeNull();
  await candidate.getByLabel("Select power_typical_w for apply").check();
  await panel.getByRole("button", { name: "Apply selected accepted values to draft" }).click();
  await expect.poll(async () => (await (await request.get(`/api/v1/catalog/revisions/${draft.id}`, { headers })).json()).typical_power_w).toBe(2000);
  const revision = await (await request.get(`/api/v1/catalog/revisions/${draft.id}`, { headers })).json();
  expect(revision.lifecycle_status).toBe("draft");
  const history = await (await request.get(`/api/v1/catalog/revisions/${draft.id}/extraction-applications`, { headers })).json();
  expect(history).toHaveLength(1);
  expect(history[0].candidates[0]).toMatchObject({ source_unit: "kW", canonical_unit: "W", applied_value: "2000.00" });
  await panel.getByText("Application provenance (1)", { exact: true }).click();
  await expect(panel).toContainText("Verified against original datasheet");
});
