import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { settings as copy } from "../copy/en";
import { FakeWorld } from "../session/fake";
import { FakeSettingsApi } from "./fake";
import { SettingsPage } from "./SettingsPage";

function setup() {
  const api = new FakeSettingsApi(new FakeWorld());
  render(<SettingsPage api={api} />);
  return api;
}

const saveButton = () => screen.getByRole("button", { name: copy.save });

describe("Admin → Settings (docs/ui/admin-settings.md)", () => {
  it("loads the three runtime settings with labels, help and choices, and the install facts as text", async () => {
    setup();
    expect(screen.getByText(copy.loading)).toBeTruthy();
    const name = (await screen.findByLabelText(copy.installationName.label)) as HTMLInputElement;
    expect(name.value).toBe("Lab 3");
    expect(screen.getByText(copy.installationName.help)).toBeTruthy();
    expect(screen.getByText(copy.lede)).toBeTruthy();

    expect(screen.getByRole("radio", { name: "Traditional (繁體) — default" })).toHaveProperty("checked", true);
    expect(screen.getByRole("radio", { name: "Simplified (简体)" })).toHaveProperty("checked", false);
    expect(screen.getByText(copy.chineseVariant.help)).toBeTruthy();
    expect(screen.getByRole("radio", { name: "8 hours" })).toHaveProperty("checked", true);
    expect(screen.getByRole("radio", { name: "1 day" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "7 days" })).toBeTruthy();
    expect(screen.getByText(copy.sessionLifetime.help)).toBeTruthy();

    const facts = screen.getByRole("region", { name: copy.installHeading });
    expect(within(facts).queryAllByRole("textbox")).toEqual([]);
    const rows = within(facts).getAllByRole("term").map((dt, i) => `${dt.textContent}: ${within(facts).getAllByRole("definition")[i]?.textContent}`);
    expect(rows).toEqual([
      "Where data is stored: /AI/Agent",
      "Web port: 443",
      "Addresses on the certificate: 127.0.0.1, localhost, lab3.internal, 10.20.0.15",
      "TLS certificate: self-signed by this installation",
      "Profile: quickstart",
      "Version: 0.0.1",
    ]);
    expect(within(facts).getByText(copy.footer)).toBeTruthy();
  });

  it("keeps Save changes disabled until something changed, saves only the change, and says when", async () => {
    const api = setup();
    const save = vi.spyOn(api, "save");
    const name = (await screen.findByLabelText(copy.installationName.label)) as HTMLInputElement;
    expect(saveButton()).toHaveProperty("disabled", true);

    fireEvent.change(name, { target: { value: "Lab 4" } });
    expect(saveButton()).toHaveProperty("disabled", false);
    fireEvent.change(name, { target: { value: "Lab 3" } });
    expect(saveButton()).toHaveProperty("disabled", true);

    fireEvent.click(screen.getByRole("radio", { name: "1 day" }));
    fireEvent.change(name, { target: { value: "Lab 4" } });
    fireEvent.click(saveButton());
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/^Saved at \d{2}:\d{2}\. It applies now; nothing restarted\.$/);
    expect(save).toHaveBeenCalledWith({ installation_name: "Lab 4", session_lifetime_hours: 24 });
    expect(api.runtime.installation_name).toBe("Lab 4");
    expect(api.world.installationName).toBe("Lab 4");
    expect(saveButton()).toHaveProperty("disabled", true);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the mirror-failed notice in three parts, naming the .env path, after a save that still applied", async () => {
    const api = setup();
    api.mirrorFails = true;
    const name = await screen.findByLabelText(copy.installationName.label);
    fireEvent.change(name, { target: { value: "Lab 4" } });
    fireEvent.click(saveButton());
    const notice = await screen.findByText(copy.mirrorFailed("/AI/Agent/.env").whatHappened);
    const section = notice.closest("section");
    expect(section?.className).toContain("warn");
    expect(section?.textContent).toContain("The data root isn't writable by the api service.");
    expect(section?.textContent).toContain("fix permissions on /AI/Agent/.env so they survive a reinstall.");
    expect(section?.getAttribute("role")).toBe("status");
    expect(screen.getByText(/^Saved at \d{2}:\d{2}\./)).toBeTruthy();
  });

  it("refuses a name over 60 characters before asking the api", async () => {
    const api = setup();
    const save = vi.spyOn(api, "save");
    const name = await screen.findByLabelText(copy.installationName.label);
    fireEvent.change(name, { target: { value: "x".repeat(90) } });
    fireEvent.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("strong").textContent).toBe("The name is too long: it has 90 characters, the limit is 60.");
    expect(within(alert).getAllByRole("definition").map((d) => d.textContent)).toEqual(["Shorten it."]);
    expect(save).not.toHaveBeenCalled();
  });

  it("says when the settings weren't saved, and when they didn't load", async () => {
    const api = setup();
    api.databaseDown = true;
    const name = await screen.findByLabelText(copy.installationName.label);
    fireEvent.change(name, { target: { value: "Lab 4" } });
    fireEvent.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(copy.notSaved.whatHappened);
    expect(alert.textContent).toContain(copy.notSaved.likelyCause);
    expect(alert.textContent).toContain(copy.notSaved.whatToDo);
  });

  it("offers Try again when the settings didn't load", async () => {
    const api = new FakeSettingsApi(new FakeWorld());
    api.down = true;
    render(<SettingsPage api={api} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(copy.failedToLoad.whatHappened);
    api.down = false;
    fireEvent.click(within(alert).getByRole("button", { name: copy.tryAgain }));
    await waitFor(() => expect(screen.getByLabelText(copy.installationName.label)).toBeTruthy());
  });
});
