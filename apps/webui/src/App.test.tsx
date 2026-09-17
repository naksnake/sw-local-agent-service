import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakePeopleApi, FakeSettingsApi } from "./admin/fake";
import { PRODUCT_NAME } from "./branding";
import { FakeCodingApi } from "./coding/api";
import { FakeFactoryApi, FakeStationsAdminApi } from "./factory/api";
import { FakeGitApi } from "./git/api";
import { FakeHomeListsApi } from "./home/fake";
import { FakeModelsApi } from "./models/fake";
import { FakeWorld } from "./session/fake";
import { renderApp, signedIn } from "./test-utils";
import { FakeValidationApi } from "./validation/api";

describe("App shell (docs/ui/home.md)", () => {
  it("has the rail with the brand and Home as the page heading", () => {
    renderApp();
    const rail = screen.getByRole("navigation", { name: "Pages" });
    expect(within(rail).getByText(PRODUCT_NAME)).toBeTruthy();
    expect(within(rail).getByText("Self-hosted, no cloud")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Home");
    expect(screen.getByText("What is running now, and what needs you.")).toBeTruthy();
  });

  it("lists only the pages whose API exists, plus the ones that say when they arrive", () => {
    renderApp();
    const rail = screen.getByRole("navigation", { name: "Pages" });
    const labels = within(rail)
      .getAllByRole("button")
      .map((b) => b.textContent);
    expect(labels).toEqual(["Home", "Runs", "Models", "Skills"]);
  });

  it("says what is healthy in one sentence and shows the version", async () => {
    renderApp();
    expect(await screen.findByText("Everything is healthy. Nothing is running.")).toBeTruthy();
    expect(screen.getByText(/Version 0\.0\.1/)).toBeTruthy();
    expect(screen.getByText("Air-gapped mode is on")).toBeTruthy();
  });

  it("gives the later pages a sentence, never a blank page", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Models" }));
    expect(await screen.findByText(/Models are read from Models\/models\.yaml/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    expect(await screen.findByText(/arrive with Phase 4/)).toBeTruthy();
  });

  it("renders Home from the api's list-only calls and keeps the agent pages off the rail", async () => {
    renderApp({ homeListsApi: new FakeHomeListsApi() });
    expect(await screen.findByText("Everything is healthy. Nothing is running.")).toBeTruthy();
    expect(screen.getByText("Nothing is running. Start a task, run or job with the buttons above.")).toBeTruthy();
    const rail = screen.getByRole("navigation", { name: "Pages" });
    expect(within(rail).queryByRole("button", { name: "Coding" })).toBeNull();
    expect(within(rail).queryByRole("button", { name: "Validation" })).toBeNull();
    expect(within(rail).queryByRole("button", { name: "Factory" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New validation run" })).toBeNull();
  });

  it("shows the real Models page when its API exists", async () => {
    renderApp({ modelsApi: new FakeModelsApi() }, "/models");
    expect(await screen.findByTestId("registry-sentence")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Models");
  });
});

describe("Agents an installation starts (ADR-0017)", () => {
  it("keeps Validation, Factory and Stations off the rail when the installation starts only coding", async () => {
    const { world, sessionApi } = signedIn("admin@slas.local");
    world.agents = ["coding"];
    renderApp({
      sessionApi,
      codingApi: new FakeCodingApi(),
      validationApi: new FakeValidationApi(),
      factoryApi: new FakeFactoryApi(),
      stationsApi: new FakeStationsAdminApi(),
      peopleApi: new FakePeopleApi(world),
    });
    const rail = await screen.findByRole("navigation", { name: "Pages" });
    expect(await within(rail).findByRole("button", { name: "Coding" })).toBeTruthy();
    expect(within(rail).queryByRole("button", { name: "Validation" })).toBeNull();
    expect(within(rail).queryByRole("button", { name: "Factory" })).toBeNull();
    fireEvent.click(within(rail).getByRole("button", { name: "Admin" }));
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stations" })).toBeNull();
  });

  it("shows every agent page when the installation starts all three", async () => {
    const { sessionApi } = signedIn("admin@slas.local");
    renderApp({
      sessionApi,
      codingApi: new FakeCodingApi(),
      validationApi: new FakeValidationApi(),
      factoryApi: new FakeFactoryApi(),
    });
    const rail = await screen.findByRole("navigation", { name: "Pages" });
    expect(await within(rail).findByRole("button", { name: "Validation" })).toBeTruthy();
    expect(within(rail).getByRole("button", { name: "Factory" })).toBeTruthy();
  });
});

describe("Admin section (ADR-0009)", () => {
  it("has People and Settings tabs next to Git hosts and Stations, opening on the first", async () => {
    const world = new FakeWorld();
    renderApp({
      peopleApi: new FakePeopleApi(world),
      settingsApi: new FakeSettingsApi(world),
      gitApi: new FakeGitApi(),
      stationsApi: new FakeStationsAdminApi(),
    });
    fireEvent.click(screen.getByRole("button", { name: "Admin" }));
    const tabs = await screen.findByRole("navigation", { name: "Admin sections" });
    expect(within(tabs).getAllByRole("button").map((b) => b.textContent)).toEqual(["People", "Settings", "Git hosts", "Stations"]);
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeTruthy();
    fireEvent.click(within(tabs).getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeTruthy();
    expect(within(tabs).getByRole("button", { name: "Settings" }).getAttribute("aria-current")).toBe("page");
    fireEvent.click(within(tabs).getByRole("button", { name: "Git hosts" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Git hosts" })).toBeTruthy();
  });

  it("shows Admin only to a person with an admin capability, and the People page to an administrator", async () => {
    const { world, sessionApi } = signedIn("admin@slas.local");
    renderApp({ sessionApi, peopleApi: new FakePeopleApi(world), settingsApi: new FakeSettingsApi(world) });
    await screen.findByRole("heading", { level: 1, name: "Home" });
    fireEvent.click(screen.getByRole("button", { name: "Admin" }));
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Admin sections" })).toBeTruthy();
  });

  it("hides Admin from an engineer and answers /admin with the not-allowed sentence", async () => {
    const { world, sessionApi } = signedIn("pat@slas.local");
    renderApp({ sessionApi, peopleApi: new FakePeopleApi(world), settingsApi: new FakeSettingsApi(world) }, "/admin");
    expect(await screen.findByRole("region", { name: "Not allowed" })).toBeTruthy();
    const rail = screen.getByRole("navigation", { name: "Pages" });
    expect(within(rail).queryByRole("button", { name: "Admin" })).toBeNull();
  });
});

describe("§9 anti-patterns", () => {
  it("has no toast component anywhere in the WebUI", () => {
    const sources = import.meta.glob("./**/*.{ts,tsx,css}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith("App.test.tsx"))
      .filter(([, source]) => /toast/i.test(source))
      .map(([path]) => path);
    expect(Object.keys(sources).length).toBeGreaterThan(10);
    expect(offenders).toEqual([]);
  });
});
