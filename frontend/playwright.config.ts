import { defineConfig, devices } from "@playwright/test";

/** Phase 10B E2E suite. Requires a running backend (migrated database + Redis — see
 * README.md's "Tests" section for the non-Docker setup, run against a dedicated
 * database, not `dcim`/`dcim_test`) reachable at BACKEND_URL (default
 * http://127.0.0.1:8000) and an Administrator account whose credentials are supplied via
 * E2E_ADMIN_EMAIL/E2E_ADMIN_PASSWORD. The frontend dev server is started here (proxying
 * `/api` to BACKEND_URL, same as vite.config.ts's own dev proxy) unless one is already
 * running at BASE_URL, in which case this attaches to it instead of starting a second
 * one — set for a manual/local run against servers already up. */
const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5173";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } },
    },
  ],
  webServer: {
    command: "npm run dev -- --port 5173",
    url: BASE_URL,
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
