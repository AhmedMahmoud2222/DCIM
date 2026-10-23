import { expect, test, type Page } from "@playwright/test";

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
    // Tailwind 4 colors compute as oklch(), not rgb(), so resolve any CSS color to sRGB through a canvas pixel.
    const channels = (color: string) => {
      const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
      context.fillStyle = "#000";
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
    };
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

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "Email" }).fill(ADMIN_EMAIL);
  await page.getByLabel("Password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}

/** Reads the focus ring of the focused element and whether its colour is the named CSS token. */
async function focusRing(page: Page, token: string) {
  return page.evaluate((tokenName) => {
    const element = document.activeElement as HTMLElement;
    const style = getComputedStyle(element);
    // Tailwind 4 colours compute as oklch(), so compare through a canvas pixel in sRGB.
    const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    const toRgb = (color: string) => {
      canvas.fillStyle = "#000";
      canvas.fillStyle = color;
      canvas.fillRect(0, 0, 1, 1);
      return Array.from(canvas.getImageData(0, 0, 1, 1).data.slice(0, 3)).join(",");
    };
    const tokenValue = getComputedStyle(document.documentElement).getPropertyValue(tokenName).trim();
    return {
      inSidebar: Boolean(element.closest("#app-sidebar")),
      outlineStyle: style.outlineStyle,
      outlineWidth: parseFloat(style.outlineWidth),
      matchesToken: toRgb(style.outlineColor) === toRgb(tokenValue),
    };
  }, token);
}

test("shared focus ring is visible on menu links and on form inputs that remove the browser outline", async ({ page }) => {
  await signIn(page);

  // Skip link first, then the first menu links in order.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Tab");
  for (let step = 0; step < 4; step += 1) {
    const ring = await focusRing(page, "--color-focus-on-sidebar");
    expect(ring.inSidebar).toBe(true);
    expect(ring.outlineStyle).toBe("solid");
    expect(ring.outlineWidth).toBeGreaterThanOrEqual(2);
    expect(ring.matchesToken).toBe(true);
    await page.keyboard.press("Tab");
  }

  // The New Equipment inputs use `focus:outline-hidden`; the shared rule must still draw a ring.
  await page.getByRole("link", { name: "Equipment", exact: true }).click();
  await page.getByRole("button", { name: "New Equipment" }).click();
  await page.getByPlaceholder("Asset tag (e.g. SRV-0142)").focus();
  const inputRing = await focusRing(page, "--color-focus");
  expect(inputRing.inSidebar).toBe(false);
  expect(inputRing.outlineStyle).toBe("solid");
  expect(inputRing.outlineWidth).toBeGreaterThanOrEqual(2);
  expect(inputRing.matchesToken).toBe(true);
});

test("design tokens can be replaced by a theme attribute without touching components", async ({ page }) => {
  await page.goto("/login");
  await page.addStyleTag({ content: ':root[data-theme="probe"] { --color-focus: #ff0000; }' });
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "probe"));
  await page.getByRole("textbox", { name: "Email" }).focus();
  const outlineColor = await page.evaluate(() => getComputedStyle(document.activeElement as HTMLElement).outlineColor);
  expect(outlineColor).toBe("rgb(255, 0, 0)");
});

test.describe("mobile navigation", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  const insideSidebar = (page: Page) => page.evaluate(() => Boolean(document.activeElement?.closest("#app-sidebar")));
  const noSidewaysScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

  test("menu drawer is keyboard accessible, trapped while open, and has 44px touch targets", async ({ page }) => {
    await signIn(page);

    // Content comes first; the menu is behind a button, not stacked above the page.
    const menuButton = page.getByRole("button", { name: "Open main menu" });
    await expect(menuButton).toBeVisible();
    await expect(menuButton).toHaveAttribute("aria-expanded", "false");
    const buttonBox = (await menuButton.boundingBox())!;
    expect(buttonBox.width).toBeGreaterThanOrEqual(44);
    expect(buttonBox.height).toBeGreaterThanOrEqual(44);
    const signOutBox = (await page.getByRole("button", { name: "Sign out" }).boundingBox())!;
    expect(signOutBox.height).toBeGreaterThanOrEqual(44);
    const mainBox = (await page.locator("#main-content").boundingBox())!;
    expect(mainBox.y).toBeLessThan(200);
    expect(await noSidewaysScroll(page)).toBe(true);

    // Closed: inert, and the keyboard never lands inside the hidden menu.
    await expect(page.locator("#app-sidebar")).toHaveAttribute("inert", "");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    for (let step = 0; step < 6; step += 1) {
      await page.keyboard.press("Tab");
      expect(await insideSidebar(page)).toBe(false);
    }

    // Open: a dialog, focus on Close, links at least 44px tall.
    await menuButton.click();
    const dialog = page.getByRole("dialog", { name: "Main menu" });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("button", { name: "Close menu" })).toBeFocused();
    const linkBox = (await dialog.getByRole("link", { name: "Racks", exact: true }).boundingBox())!;
    expect(linkBox.height).toBeGreaterThanOrEqual(44);
    expect(await noSidewaysScroll(page)).toBe(true);

    // Tab and Shift+Tab never leave the dialog, however many times they are pressed.
    for (let step = 0; step < 30; step += 1) {
      await page.keyboard.press("Tab");
      expect(await insideSidebar(page)).toBe(true);
    }
    for (let step = 0; step < 6; step += 1) {
      await page.keyboard.press("Shift+Tab");
      expect(await insideSidebar(page)).toBe(true);
    }

    // Escape closes it, hands focus back to the menu button, and makes it inert again.
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(menuButton).toBeFocused();
    await expect(page.locator("#app-sidebar")).toHaveAttribute("inert", "");

    // Choosing a link navigates and closes the drawer.
    await menuButton.click();
    await page.getByRole("dialog", { name: "Main menu" }).getByRole("link", { name: "Equipment", exact: true }).click();
    await expect(page).toHaveURL("/equipment");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(await noSidewaysScroll(page)).toBe(true);
  });

  test("reduced motion removes the drawer slide", async ({ page }) => {
    await signIn(page);
    const duration = () => page.locator("#app-sidebar").evaluate((element) => parseFloat(getComputedStyle(element).transitionDuration));

    await page.emulateMedia({ reducedMotion: "no-preference" });
    expect(await duration()).toBeCloseTo(0.15, 2);

    await page.emulateMedia({ reducedMotion: "reduce" });
    expect(await duration()).toBeLessThan(0.001);
  });
});
