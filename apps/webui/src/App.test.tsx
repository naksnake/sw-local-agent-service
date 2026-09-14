import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { PRODUCT_NAME } from "./branding";

describe("App shell (docs/ui/home.md)", () => {
  it("has the rail with the brand and Home as the page heading", () => {
    render(<App />);
    const rail = screen.getByRole("navigation", { name: "Pages" });
    expect(within(rail).getByText(PRODUCT_NAME)).toBeTruthy();
    expect(within(rail).getByText("Self-hosted, no cloud")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Home");
    expect(screen.getByText("What is running now, and what needs you.")).toBeTruthy();
  });

  it("lists only the pages whose API exists, plus the ones that say when they arrive", () => {
    render(<App />);
    const rail = screen.getByRole("navigation", { name: "Pages" });
    const labels = within(rail)
      .getAllByRole("button")
      .map((b) => b.textContent);
    expect(labels).toEqual(["Home", "Runs", "Models", "Skills"]);
  });

  it("says what is healthy in one sentence and shows the version", async () => {
    render(<App />);
    expect(await screen.findByText("Everything is healthy. Nothing is running.")).toBeTruthy();
    expect(screen.getByText(/Version 0\.0\.1/)).toBeTruthy();
    expect(screen.getByText("Air-gapped mode is on")).toBeTruthy();
  });

  it("gives the later pages a sentence, never a blank page", async () => {
    const { getByRole, findByText } = render(<App />);
    getByRole("button", { name: "Models" }).click();
    expect(await findByText(/Models are read from Models\/models\.yaml/)).toBeTruthy();
    getByRole("button", { name: "Skills" }).click();
    expect(await findByText(/arrive with Phase 4/)).toBeTruthy();
  });
});
