import { expect, test, type Page } from "@playwright/test";

// Narrow-layout checks for the Normal/Dark toggle. All API calls are mocked, so this spec needs no backend.
const USER = { id: "1", email: "ops@example.com", full_name: "Ops", role_names: ["admin"], permission_codes: ["*"] };

async function mockApi(page: Page) {
  await page.route("**/api/v1/**", (route) => {
    const url = route.request().url();
    if (url.includes("/auth/me")) return route.fulfill({ json: USER });
    if (url.includes("/auth/refresh")) return route.fulfill({ json: { access_token: "t" } });
    return route.fulfill({ json: [] });
  });
}

async function expectUsableToggle(page: Page) {
  const group = page.getByRole("radiogroup", { name: "Color mode" });
  await expect(group).toBeVisible();
  const viewport = page.viewportSize()!;
  const box = (await group.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
  for (const name of ["Normal", "Dark"]) {
    const target = (await group.getByRole("radio", { name }).boundingBox())!;
    expect(target.height, `${name} target height`).toBeGreaterThanOrEqual(36);
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "horizontal overflow").toBeLessThanOrEqual(0);
}

for (const width of [320, 390]) {
  test.describe(`at ${width}px wide`, () => {
    test.use({ viewport: { width, height: 740 } });

    test("sign-in page keeps the toggle on screen and switches modes", async ({ page }) => {
      await page.goto("/login");
      await expectUsableToggle(page);
      await page.getByRole("radio", { name: "Dark" }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
      await page.reload();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
      await page.getByRole("radio", { name: "Normal" }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    });

    test("app header keeps the toggle on screen next to the menu button", async ({ page }) => {
      await mockApi(page);
      await page.goto("/events");
      await expect(page.getByRole("button", { name: "Open main menu" })).toBeVisible();
      await expectUsableToggle(page);
      await page.getByRole("radio", { name: "Dark" }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    });
  });
}
