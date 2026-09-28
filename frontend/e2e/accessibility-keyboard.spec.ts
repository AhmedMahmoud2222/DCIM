import { expect, test } from "@playwright/test";

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "E2ePassw0rd!";

test("login labels and skip link work with a keyboard", async ({ page }) => {
  await page.goto("/login");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("textbox", { name: "Email" })).toBeFocused();
  await page.getByRole("textbox", { name: "Email" }).fill(ADMIN_EMAIL);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  const skip = page.getByRole("link", { name: "Skip to main content" });
  await page.keyboard.press("Tab");
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // Measure the rendered admin section label against its opaque sidebar background.
  const adminLabel = page.getByText("Admin", { exact: true });
  await expect(adminLabel).toBeVisible();
  const contrast = await adminLabel.evaluate((element) => {
    const channels = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (color: string) => {
      const [r, g, b] = channels(color).map((value) => {
        const srgb = value / 255;
        return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const foreground = luminance(getComputedStyle(element).color);
    const background = luminance(getComputedStyle(element.closest("aside")!).backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);

  // At 400% zoom on a 1280px-wide viewport, the CSS viewport is approximately 320px.
  await page.setViewportSize({ width: 320, height: 640 });
  const mainBounds = await page.locator("#main-content").boundingBox();
  expect(mainBounds).not.toBeNull();
  expect(mainBounds!.x).toBe(0);
  expect(mainBounds!.width).toBeGreaterThanOrEqual(320);
  expect(mainBounds!.x + mainBounds!.width).toBeLessThanOrEqual(320);
});
