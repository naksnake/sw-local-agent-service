import { expect, test } from "@playwright/test";

test("the WebUI shell loads with the rail, the health line and Home", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("SW Local Agent Service");
  const rail = page.getByRole("navigation", { name: "Pages" });
  await expect(rail.getByText("SW Local Agent Service")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Home");
  await expect(page.getByRole("status")).toContainText(/Everything is healthy|needs? you/);
  await expect(page.getByText("What is running now, and what needs you.")).toBeVisible();
});
