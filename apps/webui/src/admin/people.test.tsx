import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { people as copy } from "../copy/en";
import { FakeWorld, personOf } from "../session/fake";
import { FakePeopleApi } from "./fake";
import { PeoplePage } from "./PeoplePage";

function world() {
  const w = new FakeWorld();
  const admin = w.byEmail("admin@slas.local");
  if (admin === undefined) throw new Error("no admin");
  admin.must_change_password = false;
  return { w, admin, api: new FakePeopleApi(w) };
}

function alertParts(container: HTMLElement = screen.getByRole("dialog")): string[] {
  const alert = within(container).getByRole("alert");
  return [within(alert).getByRole("strong").textContent ?? "", ...within(alert).getAllByRole("definition").map((d) => d.textContent ?? "")];
}

function openMenu(name: string) {
  fireEvent.click(screen.getByRole("button", { name: copy.menu.label(name) }));
  return screen.getByRole("menu", { name: copy.menu.label(name) });
}

describe("Admin → People (docs/ui/admin-people.md)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists people with the five columns, human time and status words", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    expect(screen.getByText(copy.loading)).toBeTruthy();
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([...copy.columns, "Actions"]);
    const adminRow = screen.getByRole("row", { name: "Administrator" });
    expect(within(adminRow).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Administratoradmin@slas.local",
      "Administrator",
      "Manages people, settings, Git hosts, test stations and models, and can do everything an engineer can.",
      "Never",
      copy.status.canSignIn,
      "···",
    ]);
    const patRow = screen.getByRole("row", { name: "Pat Lin" });
    expect(within(patRow).getAllByRole("cell")[3]?.textContent).toBe("3 minutes ago");
    expect(within(patRow).getAllByRole("cell")[2]?.textContent).toBe(
      "Runs coding tasks, validation runs and factory jobs, and approves destructive steps.",
    );
    expect(screen.queryByText(copy.onlyYou)).toBeNull();
  });

  it("says 'Only you so far' when the administrator is alone", async () => {
    const { api, admin, w } = world();
    w.accounts = w.accounts.filter((a) => a.id === admin.id);
    render(<PeoplePage api={api} me={personOf(admin)} />);
    expect(await screen.findByText(copy.onlyYou)).toBeTruthy();
  });

  it("adds a person and shows the one-time password once, with Copy, then never again", async () => {
    const { api, admin } = world();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { container } = render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: copy.addButton }));
    const dialog = screen.getByRole("dialog", { name: copy.add.title });
    expect(within(dialog).getByText(copy.add.nameHelp)).toBeTruthy();
    expect(within(dialog).getByText(copy.add.emailHelp)).toBeTruthy();
    expect(within(dialog).getByRole("radio", { name: /Line lead/ })).toBeTruthy();
    expect(within(dialog).getByText("An engineer who also decides a factory PASS or FAIL when the voters disagree.")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Add person" })).toHaveProperty("disabled", true);

    fireEvent.change(within(dialog).getByLabelText(copy.add.name), { target: { value: "Ana" } });
    fireEvent.change(within(dialog).getByLabelText(copy.add.email), { target: { value: "ana@company.local" } });
    expect(within(dialog).getByText(copy.add.closing("Ana"))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Ana" }));

    const result = await screen.findByRole("dialog", { name: copy.result.title("Ana") });
    expect(within(result).getByText(copy.result.body("Ana"))).toBeTruthy();
    const password = within(result).getByLabelText(copy.result.passwordLabel).textContent ?? "";
    expect(password.length).toBeGreaterThan(8);
    fireEvent.click(within(result).getByRole("button", { name: copy.result.copy }));
    await waitFor(() => expect(within(result).getByRole("button", { name: copy.result.copied })).toBeTruthy());
    expect(writeText).toHaveBeenCalledWith(password);

    fireEvent.click(within(result).getByRole("button", { name: copy.result.done }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container.textContent).not.toContain(password);
    const anaRow = screen.getByRole("row", { name: "Ana" });
    expect(within(anaRow).getAllByRole("cell").map((c) => c.textContent)).toContain(copy.status.mustChoose);
    expect(within(anaRow).getAllByRole("cell")[1]?.textContent).toBe("Engineer");
    expect(api.world.byEmail("ana@company.local")?.password).toBe(password);
  });

  it("shows the duplicate-email and invalid-email cases inside the dialog", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: copy.addButton }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(copy.add.name), { target: { value: "Pat Again" } });
    fireEvent.change(within(dialog).getByLabelText(copy.add.email), { target: { value: "pat@slas.local" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Pat Again" }));
    await within(dialog).findByRole("alert");
    const dup = copy.duplicateEmail("pat@slas.local");
    expect(alertParts()).toEqual([dup.whatHappened, dup.likelyCause, dup.whatToDo]);

    fireEvent.change(within(dialog).getByLabelText(copy.add.email), { target: { value: "not-an-email" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Pat Again" }));
    await waitFor(() => expect(alertParts()[0]).toBe(copy.invalidEmail.whatHappened));
    expect(alertParts()).toEqual([copy.invalidEmail.whatHappened, copy.invalidEmail.likelyCause, copy.invalidEmail.whatToDo]);
    expect(screen.getAllByRole("dialog").length).toBe(1);
  });

  it("refuses to change the last administrator's role", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Administrator")).getByRole("menuitem", { name: copy.menu.changeRole }));
    const dialog = screen.getByRole("dialog", { name: copy.changeRole.title("Administrator") });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Line lead/ }));
    expect(within(dialog).getByText(copy.changeRole.closing("Administrator", "Line lead"))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: copy.changeRole.button }));
    await within(dialog).findByRole("alert");
    expect(alertParts()).toEqual([
      copy.lastAdministratorRole.whatHappened,
      copy.lastAdministratorRole.likelyCause,
      copy.lastAdministratorRole.whatToDo,
    ]);
  });

  it("changes a role and says what changes", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Pat Lin")).getByRole("menuitem", { name: copy.menu.changeRole }));
    const dialog = screen.getByRole("dialog", { name: copy.changeRole.title("Pat Lin") });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Line lead/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: copy.changeRole.button }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(screen.getByRole("row", { name: "Pat Lin" })).getAllByRole("cell")[1]?.textContent).toBe("Line lead");
  });

  it("refuses to switch off your own account before asking the api", async () => {
    const { api, admin } = world();
    const setActive = vi.spyOn(api, "setActive");
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Administrator")).getByRole("menuitem", { name: copy.menu.switchOff }));
    const dialog = screen.getByRole("dialog", { name: copy.switchOff.title("Administrator") });
    expect(within(dialog).getByText(copy.switchOff.body("Administrator"))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: copy.switchOff.button }));
    expect(alertParts()).toEqual([copy.switchOff.yourself.whatHappened, copy.switchOff.yourself.likelyCause, copy.switchOff.yourself.whatToDo]);
    expect(setActive).not.toHaveBeenCalled();
  });

  it("refuses to switch off the last administrator", async () => {
    const { api, w } = world();
    const pat = w.byEmail("pat@slas.local");
    if (pat === undefined) throw new Error("no pat");
    render(<PeoplePage api={api} me={personOf(pat)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Administrator")).getByRole("menuitem", { name: copy.menu.switchOff }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: copy.switchOff.button }));
    await within(screen.getByRole("dialog")).findByRole("alert");
    expect(alertParts()).toEqual([
      copy.lastAdministratorOff.whatHappened,
      copy.lastAdministratorOff.likelyCause,
      copy.lastAdministratorOff.whatToDo,
    ]);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: copy.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(screen.getByRole("row", { name: "Administrator" })).getAllByRole("cell")[4]?.textContent).toBe(copy.status.canSignIn);
  });

  it("switches a person off (signed out everywhere) and back on", async () => {
    const { api, admin, w } = world();
    w.addAccount("ana@company.local", "Ana", "engineer", "ana-password-1234");
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Ana")).getByRole("menuitem", { name: copy.menu.switchOff }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: copy.switchOff.button }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(screen.getByRole("row", { name: "Ana" })).getAllByRole("cell")[4]?.textContent).toBe(copy.status.switchedOff);

    fireEvent.click(within(openMenu("Ana")).getByRole("menuitem", { name: copy.menu.switchOn }));
    const on = screen.getByRole("dialog", { name: copy.switchOn.title("Ana") });
    expect(within(on).getByText(copy.switchOn.body("Ana"))).toBeTruthy();
    fireEvent.click(within(on).getByRole("button", { name: copy.switchOn.button }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(screen.getByRole("row", { name: "Ana" })).getAllByRole("cell")[4]?.textContent).toBe(copy.status.canSignIn);
  });

  it("resets a password: the same dialog advances to the one-time password panel", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(within(openMenu("Pat Lin")).getByRole("menuitem", { name: copy.menu.resetPassword }));
    const dialog = screen.getByRole("dialog", { name: copy.resetPassword.title("Pat Lin") });
    expect(within(dialog).getByText(copy.resetPassword.body("Pat Lin"))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: copy.resetPassword.button }));
    const result = await screen.findByRole("dialog", { name: copy.result.title("Pat Lin") });
    const password = within(result).getByLabelText(copy.result.passwordLabel).textContent;
    expect(api.world.byEmail("pat@slas.local")?.password).toBe(password);
    fireEvent.click(within(result).getByRole("button", { name: copy.result.done }));
    expect(within(screen.getByRole("row", { name: "Pat Lin" })).getAllByRole("cell")[4]?.textContent).toBe(copy.status.mustChoose);
  });

  it("says when the list didn't load and offers Try again", async () => {
    const { api, admin } = world();
    api.down = true;
    render(<PeoplePage api={api} me={personOf(admin)} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(copy.failedToLoad.whatHappened);
    expect(alert.textContent).toContain(copy.failedToLoad.whatToDo);
    api.down = false;
    fireEvent.click(within(alert).getByRole("button", { name: copy.tryAgain }));
    expect(await screen.findByRole("table")).toBeTruthy();
  });

  it("says when a person wasn't added because the api didn't answer", async () => {
    const { api, admin } = world();
    render(<PeoplePage api={api} me={personOf(admin)} />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: copy.addButton }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(copy.add.name), { target: { value: "Ana" } });
    fireEvent.change(within(dialog).getByLabelText(copy.add.email), { target: { value: "ana@company.local" } });
    api.down = true;
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Ana" }));
    await within(dialog).findByRole("alert");
    const words = copy.add.notAdded("Ana");
    expect(alertParts()).toEqual([words.whatHappened, words.likelyCause, words.whatToDo]);
  });
});
