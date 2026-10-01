import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../branding";
import { choosePassword, notAllowed, shell, signIn as copy, unknownAddress } from "../copy/en";
import { renderApp, signedIn } from "../test-utils";
import { FakeSessionApi, FakeWorld } from "./fake";

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function attempt(email: string, password: string) {
  fill(copy.email, email);
  fill(copy.password, password);
  fireEvent.click(screen.getByRole("button", { name: copy.button }));
}

function alertParts(): string[] {
  const alert = screen.getByRole("alert");
  return [within(alert).getByRole("strong").textContent ?? "", ...within(alert).getAllByRole("definition").map((d) => d.textContent ?? "")];
}

describe("Sign in (docs/ui/sign-in.md)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is the front door: an anonymous visit lands on the installation's name, the lede and two fields", async () => {
    renderApp({ sessionApi: new FakeSessionApi() }, "/");
    expect(await screen.findByRole("heading", { level: 1, name: "Lab 3" })).toBeTruthy();
    expect(screen.getByText(copy.lede)).toBeTruthy();
    expect(screen.getByLabelText(copy.email)).toBeTruthy();
    expect(screen.getByLabelText(copy.password)).toBeTruthy();
    expect(screen.getByRole("button", { name: copy.button })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Pages" })).toBeNull();
  });

  it("falls back to the product name when the public endpoint does not answer, and says the server didn't answer", async () => {
    const sessionApi = new FakeSessionApi();
    sessionApi.mode = "unreachable";
    renderApp({ sessionApi }, "/sign-in");
    expect(await screen.findByRole("heading", { level: 1, name: PRODUCT_NAME })).toBeTruthy();
    await attempt("pat@slas.local", "pat-password-12345");
    await screen.findByRole("alert");
    expect(alertParts()).toEqual([
      copy.serverNotAnswering.whatHappened,
      copy.serverNotAnswering.likelyCause,
      copy.serverNotAnswering.whatToDo,
    ]);
    expect(screen.getByRole("button", { name: copy.buttonRetry })).toBeTruthy();
  });

  it("shows the wrong-password case in three parts, inline, until the next attempt", async () => {
    renderApp({ sessionApi: new FakeSessionApi() }, "/sign-in");
    await screen.findByLabelText(copy.email);
    await attempt("pat@slas.local", "nope");
    await screen.findByRole("alert");
    expect(alertParts()).toEqual([copy.wrongPassword.whatHappened, copy.wrongPassword.likelyCause, copy.wrongPassword.whatToDo]);
    expect(screen.getByRole("alert")).toBeTruthy();
    await attempt("pat@slas.local", "pat-password-12345");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the switched-off, too-many-attempts and rate-limiter cases", async () => {
    const world = new FakeWorld();
    const pat = world.byEmail("pat@slas.local");
    if (pat === undefined) throw new Error("no pat");
    pat.is_active = false;
    const sessionApi = new FakeSessionApi(world);
    renderApp({ sessionApi }, "/sign-in");
    await screen.findByLabelText(copy.email);

    await attempt("pat@slas.local", "pat-password-12345");
    await screen.findByRole("alert");
    expect(alertParts()).toEqual([copy.switchedOff.whatHappened, copy.switchedOff.likelyCause, copy.switchedOff.whatToDo]);

    sessionApi.exhaustAttempts();
    await attempt("pat@slas.local", "wrong");
    await waitFor(() => expect(alertParts()[0]).toBe(copy.tooManyAttempts.whatHappened));
    expect(alertParts()).toEqual([copy.tooManyAttempts.whatHappened, copy.tooManyAttempts.likelyCause, copy.tooManyAttempts.whatToDo]);

    sessionApi.mode = "rate-limiter-down";
    await attempt("pat@slas.local", "wrong");
    await waitFor(() => expect(alertParts()[0]).toBe(copy.rateLimiterDown.whatHappened));
    expect(alertParts()).toEqual([copy.rateLimiterDown.whatHappened, copy.rateLimiterDown.likelyCause, copy.rateLimiterDown.whatToDo]);
    expect(alertParts()[2]).toContain("slas logs redis");
  });

  it("says Signing in… while waiting and, after 5 seconds, that the server is slow", async () => {
    const sessionApi = new FakeSessionApi();
    sessionApi.mode = "hang";
    renderApp({ sessionApi }, "/sign-in");
    await screen.findByLabelText(copy.email);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await attempt("pat@slas.local", "pat-password-12345");
    expect(screen.getByRole("button", { name: copy.buttonWaiting })).toBeTruthy();
    expect(screen.queryByText(copy.stillChecking)).toBeNull();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByText(copy.stillChecking)).toBeTruthy();
  });

  it("signs in, shows who is signed in with their role, and signs out to 'You're signed out.'", async () => {
    renderApp({ sessionApi: new FakeSessionApi() }, "/");
    await screen.findByLabelText(copy.email);
    await attempt("pat@slas.local", "pat-password-12345");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeTruthy();
    expect(screen.getByTestId("signed-in-as").textContent).toBe(shell.signedInAs("Pat Lin", "Engineer"));
    expect(screen.getByTestId("welcome").textContent).toBe(
      "Signed in as Pat Lin. You can operate screens, run commands on targets, work with Git remotes and approve destructive steps. Coding, Validation and Factory arrive in later phases.",
    );
    const rail = screen.getByRole("navigation", { name: "Pages" });
    expect(within(rail).queryByRole("button", { name: "Admin" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: shell.signOut }));
    const notice = await screen.findByText(copy.signedOut);
    expect(notice.getAttribute("role")).toBe("status");
    expect(screen.getByLabelText(copy.email)).toBeTruthy();
  });

  it("shows the session-ended sentence with the configured lifetime after a 401 with reason expired", async () => {
    const { sessionApi, world } = signedIn();
    world.sessionLifetimeHours = 24;
    renderApp({ sessionApi }, "/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    act(() => {
      sessionApi.expireSession();
    });
    expect(await screen.findByText(copy.sessionEnded("1 day"))).toBeTruthy();
    expect(screen.getByLabelText(copy.email)).toBeTruthy();
  });

  it("says Checking your sign-in… while the session is being checked", () => {
    const sessionApi = new FakeSessionApi();
    sessionApi.mode = "hang";
    renderApp({ sessionApi }, "/");
    expect(screen.getByRole("status").textContent).toBe(shell.checking);
  });
});

describe("Choose a new password (docs/ui/sign-in.md)", () => {
  async function signInAsBootstrapAdmin() {
    renderApp({ sessionApi: new FakeSessionApi() }, "/");
    await screen.findByLabelText(copy.email);
    await attempt("admin@slas.local", "admin-one-time-pw");
    expect(await screen.findByRole("heading", { level: 1, name: choosePassword.heading })).toBeTruthy();
  }

  function save(next: string, again: string) {
    fill(choosePassword.newPassword, next);
    fill(choosePassword.again, again);
    fireEvent.click(screen.getByRole("button", { name: choosePassword.button }));
  }

  it("is forced after a one-time password and checks mismatch, length and the email", async () => {
    await signInAsBootstrapAdmin();
    expect(screen.getByText(choosePassword.lede)).toBeTruthy();
    expect(screen.getByText(choosePassword.help)).toBeTruthy();
    expect(screen.queryByLabelText(choosePassword.current)).toBeNull();

    save("correct-horse-battery", "correct-horse-batery");
    expect(alertParts()).toEqual([choosePassword.mismatch.whatHappened, choosePassword.mismatch.likelyCause, choosePassword.mismatch.whatToDo]);

    save("shortpw8", "shortpw8");
    expect(alertParts()).toEqual(["The password is too short: it has 8 characters and needs at least 12.", "Add a few more words."]);

    save("admin@slas.local", "admin@slas.local");
    expect(alertParts()).toEqual([choosePassword.sameAsEmail.whatHappened, choosePassword.sameAsEmail.whatToDo]);

    save("correct-horse-battery", "correct-horse-battery");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeTruthy();
    expect(screen.getByTestId("signed-in-as").textContent).toBe(shell.signedInAs("Administrator", "Administrator"));
    expect(screen.getByTestId("welcome").textContent).toBe(
      "Signed in as Administrator. Coding, Validation and Factory arrive in later phases; People and Settings are under Admin.",
    );
  });

  it("keeps a reloaded page here, asking for the current password again, until one is saved", async () => {
    const world = new FakeWorld();
    const sessionApi = new FakeSessionApi(world);
    sessionApi.current = world.byEmail("admin@slas.local") ?? null;
    renderApp({ sessionApi }, "/admin/people");
    expect(await screen.findByRole("heading", { level: 1, name: choosePassword.heading })).toBeTruthy();
    expect(screen.getByLabelText(choosePassword.current)).toBeTruthy();
    expect(screen.getByText(choosePassword.currentHelp)).toBeTruthy();

    // A wrong current password is a 401 from the api, but the session is still good: the
    // page shows the error and nobody is signed out.
    fill(choosePassword.current, "not-the-one-time-password");
    save("correct-horse-battery", "correct-horse-battery");
    await screen.findByRole("alert");
    expect(alertParts()[0]).toBe(copy.wrongPassword.whatHappened);
    expect(screen.getByRole("heading", { level: 1, name: choosePassword.heading })).toBeTruthy();

    fill(choosePassword.current, "admin-one-time-pw");
    save("correct-horse-battery", "correct-horse-battery");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeTruthy();
  });

  it("renders the server-not-answering case with the page's own words", async () => {
    const sessionApi = new FakeSessionApi();
    renderApp({ sessionApi }, "/");
    await screen.findByLabelText(copy.email);
    await attempt("admin@slas.local", "admin-one-time-pw");
    await screen.findByRole("heading", { level: 1, name: choosePassword.heading });
    sessionApi.mode = "unreachable";
    save("correct-horse-battery", "correct-horse-battery");
    await screen.findByRole("alert");
    expect(alertParts()).toEqual([
      choosePassword.serverNotAnswering.whatHappened,
      choosePassword.serverNotAnswering.likelyCause,
      choosePassword.serverNotAnswering.whatToDo,
    ]);
    expect(screen.getByRole("heading", { level: 1, name: choosePassword.heading })).toBeTruthy();
  });
});

describe("Guards (docs/ui/sign-in.md)", () => {
  it("shows the not-allowed sentence and a link Home on /admin/* without the capability", async () => {
    const { sessionApi, world } = signedIn();
    const { FakePeopleApi, FakeSettingsApi } = await import("../admin/fake");
    renderApp({ sessionApi, peopleApi: new FakePeopleApi(world), settingsApi: new FakeSettingsApi(world) }, "/admin/people");
    const section = await screen.findByRole("region", { name: "Not allowed" });
    expect(within(section).getByText(notAllowed.sentence)).toBeTruthy();
    fireEvent.click(within(section).getByRole("link", { name: notAllowed.link }));
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeTruthy();
  });

  it("shows one sentence and a link Home at an unknown address", async () => {
    const { sessionApi } = signedIn();
    renderApp({ sessionApi }, "/nowhere/at/all");
    const section = await screen.findByRole("region", { name: "Unknown address" });
    expect(within(section).getByText(unknownAddress.sentence)).toBeTruthy();
    expect(within(section).getByRole("link", { name: unknownAddress.link })).toBeTruthy();
  });

  it("sends a signed-in person who opens /sign-in back to Home", async () => {
    const { sessionApi } = signedIn();
    renderApp({ sessionApi }, "/sign-in");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeTruthy();
  });
});
