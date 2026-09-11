import { defineConfig, devices } from "@playwright/test";

// End-to-end tests live in tests/e2e (CLAUDE.md §13). The WebUI dev server is started
// here on loopback; CI installs Chromium for this exact @playwright/test version.
export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: true,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm --filter @slas/webui dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
