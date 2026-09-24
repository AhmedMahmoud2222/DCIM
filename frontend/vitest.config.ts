import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    // frontend/e2e/ holds Playwright specs (`npm run test:e2e`), not Vitest ones — both
    // tools default to matching `*.spec.ts`, so they must be excluded here explicitly.
    exclude: ["**/node_modules/**", "**/e2e/**"],
  },
});
