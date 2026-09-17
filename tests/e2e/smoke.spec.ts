import { expect, test } from "@playwright/test";

// The round-1 journey against the dev server, which runs on the in-memory fakes (apps/webui/
// src/apis.fake.ts): the sign-in front door, the forced password change, the shell with Home,
// Admin → People and Settings, Models, and sign out. The same steps run against the real stack
// in the deploy job with the one-time password install.sh prints (ADR-0009).

test("sign in with a one-time password, choose a new one, reach Home, Admin and Models, sign out", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("SW Local Agent Service");

  // Sign in: the installation name is the heading; nothing else is on the page.
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Lab 3");
  await expect(page.getByText("Use the account an administrator created for you. Nothing you type leaves this network.")).toBeVisible();
  await page.getByLabel("Email", { exact: true }).fill("admin@slas.local");
  await page.getByLabel("Password", { exact: true }).fill("admin-one-time-pw");
  await page.getByRole("button", { name: "Sign in" }).click();

  // A one-time password leads to Choose a new password before anything else, and a reload
  // keeps the person there (asking for the one-time password again, since it is not stored).
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Choose a new password");
  await expect(page).toHaveURL(/\/choose-password$/);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Choose a new password");
  await page.getByLabel("Current password", { exact: true }).fill("admin-one-time-pw");
  await page.getByLabel("New password", { exact: true }).fill("correct-horse-battery");
  await page.getByLabel("Type it again", { exact: true }).fill("correct-horse-battery");
  await page.getByRole("button", { name: "Save and continue" }).click();

  // The shell: rail, health sentence, Home, who is signed in.
  const rail = page.getByRole("navigation", { name: "Pages" });
  await expect(rail.getByText("SW Local Agent Service")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Home");
  await expect(page.getByRole("status")).toContainText(/Everything is healthy|needs? you/);
  await expect(page.getByText("What is running now, and what needs you.")).toBeVisible();
  await expect(page.getByTestId("signed-in-as")).toHaveText("Signed in as Administrator · Administrator");

  // Admin → People and Settings.
  await rail.getByRole("button", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("People");
  await expect(page.getByRole("row", { name: "Administrator" })).toContainText("Can sign in");
  await page.getByRole("navigation", { name: "Admin sections" }).getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Settings");
  await expect(page.getByLabel("Name shown on the sign-in page")).toHaveValue("Lab 3");
  await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();

  // Models, read from the registry.
  await rail.getByRole("button", { name: "Models" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Models");
  await expect(page.getByTestId("registry-sentence")).toContainText("2 voters from 2 model families.");

  // Sign out returns to the front door with one sentence.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByText("You're signed out.")).toBeVisible();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
});

test("an engineer sees no Admin, and /admin/people answers with the not-allowed sentence", async ({ page }) => {
  await page.goto("/sign-in");
  await page.getByLabel("Email", { exact: true }).fill("pat@slas.local");
  await page.getByLabel("Password", { exact: true }).fill("pat-password-12345");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Home");
  await expect(page.getByTestId("signed-in-as")).toHaveText("Signed in as Pat Lin · Engineer");
  await expect(page.getByRole("navigation", { name: "Pages" }).getByRole("button", { name: "Admin" })).toHaveCount(0);

  await page.goto("/admin/people");
  await expect(
    page.getByText("This part is for administrators. Ask an administrator if you need something changed here."),
  ).toBeVisible();
  await page.getByRole("link", { name: "Go to Home" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Home");
});

test("a wrong password is explained in three parts, inline", async ({ page }) => {
  await page.goto("/sign-in");
  await page.getByLabel("Email", { exact: true }).fill("pat@slas.local");
  await page.getByLabel("Password", { exact: true }).fill("not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("That email and password don't match.");
  await expect(alert).toContainText("A typo, or the password was changed.");
  await expect(alert).toContainText("Try again, or ask an administrator to reset your password.");
});
