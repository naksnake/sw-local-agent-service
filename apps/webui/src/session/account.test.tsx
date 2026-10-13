import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeGitApi } from "../git/api";
import { renderApp, signedIn } from "../test-utils";

function fill(container: HTMLElement, label: string, value: string) {
  fireEvent.change(within(container).getByLabelText(label), { target: { value } });
}

describe("Settings → Your account (docs/ui/account.md)", () => {
  it("is where Settings opens, next to Git remotes, for everyone signed in", async () => {
    const { sessionApi } = signedIn();
    renderApp({ sessionApi, gitApi: new FakeGitApi() }, "/settings");
    expect(await screen.findByRole("heading", { level: 1, name: "Your account" })).toBeTruthy();
    const tabs = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(tabs).getAllByRole("button").map((b) => b.textContent)).toEqual(["Your account", "Git remotes"]);
    fireEvent.click(within(tabs).getByRole("button", { name: "Git remotes" }));
    expect(within(tabs).getByRole("button", { name: "Git remotes" }).getAttribute("aria-current")).toBe("page");
  });

  it("shows the sign-in name and role, and renames you everywhere at once", async () => {
    const { sessionApi } = signedIn();
    renderApp({ sessionApi }, "/settings/account");
    const signin = await screen.findByRole("region", { name: "Sign-in" });
    expect(within(signin).getByText("pat")).toBeTruthy();
    expect(within(signin).getByText(/It is also your folder, Coding\/pat\//)).toBeTruthy();
    expect(within(signin).getByText("Engineer")).toBeTruthy();

    const name = screen.getByRole("form", { name: "Name" });
    expect(within(name).getByRole("button", { name: "Save name" })).toHaveProperty("disabled", true);
    fill(name, "Your name", "   ");
    fireEvent.click(within(name).getByRole("button", { name: "Save name" }));
    expect((await within(name).findByRole("alert")).textContent).toContain("The name is empty.");
    fill(name, "Your name", "Pat Lin-Chen");
    fireEvent.click(within(name).getByRole("button", { name: "Save name" }));
    expect((await within(name).findByRole("status")).textContent).toBe("Saved. New commits and tickets show Pat Lin-Chen.");
    await waitFor(() => expect(screen.getByTestId("signed-in-as").textContent).toContain("Pat Lin-Chen"));
  });

  it("changes the password with the current one, and refuses a mismatch first", async () => {
    const { sessionApi, account } = signedIn();
    renderApp({ sessionApi }, "/settings/account");
    const box = await screen.findByRole("form", { name: "Password" });
    fill(box, "Current password", "pat-password-12345");
    fill(box, "New password", "a-new-long-password");
    fill(box, "Type it again", "a-new-long-passw0rd");
    fireEvent.click(within(box).getByRole("button", { name: "Change password" }));
    expect((await within(box).findByRole("alert")).textContent).toContain("The two passwords don't match.");
    fill(box, "Type it again", "a-new-long-password");
    fireEvent.click(within(box).getByRole("button", { name: "Change password" }));
    expect((await within(box).findByRole("status")).textContent).toBe(
      "Your password was changed. Your other sign-ins have ended; this one stays.",
    );
    expect(account.password).toBe("a-new-long-password");
    expect(screen.getByRole("heading", { level: 1, name: "Your account" })).toBeTruthy();
  });

  it("signs you out everywhere else and keeps this one", async () => {
    const { sessionApi } = signedIn();
    sessionApi.otherSessions = 2;
    renderApp({ sessionApi }, "/settings/account");
    const box = await screen.findByRole("region", { name: "Other sign-ins" });
    fireEvent.click(within(box).getByRole("button", { name: "Sign out everywhere else" }));
    expect((await within(box).findByRole("status")).textContent).toBe("Signed out of 2 other sessions. This one stays signed in.");
    fireEvent.click(within(box).getByRole("button", { name: "Sign out everywhere else" }));
    await waitFor(() =>
      expect(within(box).getByRole("status").textContent).toBe("You were signed in only here; nothing else was signed out."),
    );
  });
});
