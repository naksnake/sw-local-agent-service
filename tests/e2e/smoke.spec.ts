import { expect, test } from "@playwright/test";

test("the WebUI shell loads and names the product", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("SW Local Agent Service");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("SW Local Agent Service");
  await expect(page.getByText(/being set up on this host/)).toBeVisible();
});
