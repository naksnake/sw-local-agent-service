import { defineConfig, devices } from "@playwright/test";

// Air-gapped hosts never download browsers (INV-1). Point at the bundled Chromium instead.
const executablePath = process.env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"];

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: true,
  forbidOnly: process.env["CI"] !== undefined,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env["SLAS_BASE_URL"] ?? "https://localhost",
    // quickstart serves a self-signed certificate (CLAUDE.md §3)
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(executablePath === undefined ? {} : { launchOptions: { executablePath } }),
      },
    },
  ],
});
