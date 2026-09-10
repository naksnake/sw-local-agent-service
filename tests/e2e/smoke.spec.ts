import { expect, test } from "@playwright/test";

// INV-10: a fresh install reaches a working login page. The page arrives in Phase 1; this
// spec documents the check so the P1 deploy test only has to remove the skip.
test.describe("login page (INV-10)", () => {
  test.skip(true, "The login page arrives in Phase 1 (docs/DEVELOPMENT_PLAN.md).");

  test("a fresh install shows the login page", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "SW Local Agent Service" })).toBeVisible();
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  });
});
