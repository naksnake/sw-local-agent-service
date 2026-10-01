import { expect, type Locator, type Page, test } from "@playwright/test";

// The round-1 journey against the dev server, which runs on the in-memory fakes (apps/webui/
// src/apis.fake.ts): the sign-in front door, the forced password change, the shell with Home,
// Admin → People and Settings, Models, and sign out. The same steps run against the real stack
// in the deploy job with the one-time password install.sh prints (ADR-0009). Below it, the
// round-2 journeys: each agent's three-step wizard to its closing sentence and its verb button.

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

// --- The agent wizards (CLAUDE.md §9: three steps, a closing sentence, one verb button) -------
// Round 2 puts Coding, Validation and Factory on the rail; under `vite dev` they run on the
// in-memory fakes, so each journey walks the wizard to its sentence and starts a ticket.

async function signInAsPat(page: Page): Promise<Locator> {
  await page.goto("/sign-in");
  await page.getByLabel("Email", { exact: true }).fill("pat@slas.local");
  await page.getByLabel("Password", { exact: true }).fill("pat-password-12345");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Home");
  return page.getByRole("navigation", { name: "Pages" });
}

test("New coding task: Plan → Setup → Review ends in the sentence and Start task creates the ticket", async ({ page }) => {
  const rail = await signInAsPat(page);
  await rail.getByRole("button", { name: "Coding" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Coding");
  await expect(page.getByText("No coding task yet. Start one with a plan; the agent shows every step here as it works.")).toBeVisible();

  await page.getByRole("button", { name: "New coding task" }).click();
  await expect(page.getByText("Step 1 of 3 — Plan")).toBeVisible();
  await page.getByLabel("Plan", { exact: true }).fill("# Fan controller\n\n- Parse `config.yaml` into a dataclass in `fan_ctl.py`.\n- Add a `pytest` test for the parser.\n");
  await expect(page.getByTestId("detected")).toHaveText("Languages detected: Python, YAML/JSON config.");
  await page.getByRole("button", { name: "Next: Setup" }).click();

  await expect(page.getByText("Step 2 of 3 — Setup")).toBeVisible();
  await expect(page.getByLabel("Python version")).toHaveAttribute("placeholder", "newest bundled");
  await page.getByRole("button", { name: "Next: Review" }).click();

  await expect(page.getByText("Step 3 of 3 — Review")).toBeVisible();
  await expect(page.getByTestId("sentence")).toHaveText(
    "The agent will work in an isolated sandbox with Python 3.12.6, YAML/JSON config 1.35.1, do 2 tasks, commit on its own branch, cross-check the result with 3 voters, and export a ZIP.",
  );
  await page.getByRole("button", { name: "Start task" }).click();

  const card = page.getByRole("listitem", { name: "T-coding-0001" });
  await expect(card).toContainText("Fan controller · T-coding-0001");
  await expect(card).toContainText("T-coding-0001 is running: step 2 of 7.");
  await expect(card.getByRole("list", { name: "T-coding-0001 activity" }).getByRole("listitem").first()).toHaveText(
    "Toolchain: Python 3.12.6 and YAML/JSON config 1.35.1.",
  );
  // Nothing on the page ever shows a credential; the Git panel is where a push happens.
  await expect(card).toContainText("push happens from the Git panel, which uses your saved remote.");
});

test("New validation run: Suite → Target → Review & approve ends in the sentence and Approve and start creates the run", async ({ page }) => {
  const rail = await signInAsPat(page);
  await rail.getByRole("button", { name: "Validation" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Validation");

  await page.getByRole("button", { name: "New validation run" }).click();
  await expect(page.getByText("Step 1 of 3 — Suite")).toBeVisible();
  await page.getByLabel("Suite", { exact: true }).fill("# GX8 DC cycling\n\n- DC cycle x25, settle 60 s\n- Read SEL\n");
  await expect(page.getByTestId("items-note")).toHaveText("GX8 DC cycling: 2 items, 26 cycles in total.");
  await page.getByRole("button", { name: "Next: Target" }).click();

  await expect(page.getByText("Step 2 of 3 — Target")).toBeVisible();
  await expect(page.getByText("Credentials come from the vault; nothing here shows or asks for a password.")).toBeVisible();
  await expect(page.getByLabel("lab-gx8-02")).toBeDisabled();
  await page.getByLabel("lab-gx8-01").check();
  await page.getByRole("button", { name: "Next: Review" }).click();

  await expect(page.getByText("Step 3 of 3 — Review & approve")).toBeVisible();
  await expect(page.getByTestId("sentence")).toHaveText("GX8 DC cycling on lab-gx8-01: 25 power cycles and 2 suite items, 31 steps. Nothing destructive.");
  await expect(page.getByTestId("cross-check")).toHaveText("3 of 3 voters agree the plan stays within the guardrails. Your approval starts it.");
  await expect(page.getByTestId("guardrails").getByRole("listitem")).toHaveCount(7);
  await page.getByRole("button", { name: "Approve and start" }).click();

  const card = page.getByRole("listitem", { name: "T-validation-0001" });
  await expect(card).toContainText("25 of 25 cycles done: 12 with findings.");
  await expect(card.getByRole("list", { name: "T-validation-0001 cycle map" }).getByRole("listitem")).toHaveCount(25);
  await expect(card.getByRole("list", { name: "T-validation-0001 findings" })).toContainText("PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14. Owner: EE.");
  await expect(card.getByRole("button", { name: "Review ticket T-validation-0002" })).toBeVisible();
});

test("New factory job: Trigger → Test loop → Rules ends in the sentence and Start job creates the job", async ({ page }) => {
  const rail = await signInAsPat(page);
  await rail.getByRole("button", { name: "Factory" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Factory");

  await page.getByRole("button", { name: "New factory job" }).click();
  await expect(page.getByText("Step 1 of 3 — Trigger")).toBeVisible();
  await page.getByLabel("MES-88131").check();
  await expect(page.getByTestId("trigger-note")).toHaveText("Unit SN-GX8-0100 on station-07, from MES ticket MES-88131.");
  await page.getByRole("button", { name: "Next: Test loop" }).click();

  await expect(page.getByText("Step 2 of 3 — Test loop")).toBeVisible();
  await expect(page.getByLabel("Final test, 9 steps")).toBeChecked();
  await expect(page.getByTestId("template-steps").getByRole("listitem")).toHaveCount(10);
  await expect(page.getByTestId("skills-used")).toHaveText("Skills used for the GUI steps: station-login-burnin. Every GUI step is screenshot before and after.");
  await page.getByRole("button", { name: "Next: Rules" }).click();

  await expect(page.getByText("Step 3 of 3 — Rules")).toBeVisible();
  await expect(page.getByTestId("sentence")).toHaveText(
    "Final test, 9 steps for unit SN-GX8-0100 on station-07: 10 steps, using the station-login-burnin skill. PASS needs 3 of 3 voters; anything else holds the station for the line lead. The station state is backed up.",
  );
  await page.getByRole("button", { name: "Start job" }).click();

  const card = page.getByRole("listitem", { name: "T-factory-0001" });
  await expect(card).toContainText("10 of 10 steps done. Verdict: PASS (3 of 3 voters).");
  await expect(card.getByRole("list", { name: "T-factory-0001 step map" }).getByRole("listitem")).toHaveCount(10);
  await expect(card.getByTestId("T-factory-0001-verdict")).toHaveText("PASS: 3 of 3 voters say PASS. 3 of 3 agree with the conclusion.");
  await expect(card.getByRole("list", { name: "T-factory-0001 screenshots" }).getByRole("img")).toHaveCount(8);
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
