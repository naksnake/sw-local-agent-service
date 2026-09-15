import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeGitApi, PUSH_EXPLANATION } from "./api";
import { GitHostsAdmin } from "./GitHostsAdmin";
import { GitPanel } from "./GitPanel";
import { GitRemotesSettings } from "./GitRemotesSettings";

const TOKEN = "glpat-Zq8xw2Yv7Rt4Ks9Lm3Np6Bd";

describe("Settings → Git remotes", () => {
  it("saves a paste-only token, shows only the fingerprint, tests, rotates and deletes", async () => {
    const api = new FakeGitApi();
    const { container } = render(<GitRemotesSettings api={api} />);
    expect(await screen.findByText(/You have no remote yet/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Remote name"), { target: { value: "gitlab-firmware" } });
    fireEvent.change(screen.getByLabelText("Remote address"), { target: { value: "https://gitlab.internal/firmware/bmc.git" } });
    const tokenField = screen.getByLabelText("Token") as HTMLInputElement;
    expect(tokenField.type).toBe("password");
    fireEvent.change(tokenField, { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Save remote" }));

    const notice = await screen.findByTestId("notice");
    expect(notice.textContent).toBe(
      "Saved gitlab-firmware. The token is stored encrypted; only its fingerprint …p6Bd is shown from now on.",
    );
    expect(container.textContent).not.toContain(TOKEN);
    expect(tokenField.value).toBe("");
    const card = screen.getByLabelText("gitlab-firmware");
    expect(card.textContent).toContain("token …p6Bd");

    fireEvent.click(within(card).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(screen.getByTestId("notice").textContent).toBe("Connected to gitlab-firmware: 3 branches, including main."));

    fireEvent.click(within(card).getByRole("button", { name: "Rotate" }));
    fireEvent.change(screen.getByLabelText("New credential for gitlab-firmware"), { target: { value: "glpat-NewNewNewNewNewNew1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Save new credential" }));
    await waitFor(() => expect(screen.getByTestId("notice").textContent).toBe("Rotated gitlab-firmware; the new fingerprint is …1234."));
    expect(container.textContent).not.toContain("NewNewNew");

    fireEvent.click(within(screen.getByLabelText("gitlab-firmware")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.getByTestId("notice").textContent).toBe("Removed gitlab-firmware; its credential was deleted with it."));
    expect(api.pasted).toEqual([TOKEN, "glpat-NewNewNewNewNewNew1234"]);
  });

  it("refuses a host that is not allowed with the three-part sentence", async () => {
    const api = new FakeGitApi();
    render(<GitRemotesSettings api={api} />);
    await screen.findByText(/You have no remote yet/);
    fireEvent.change(screen.getByLabelText("Remote name"), { target: { value: "gh" } });
    fireEvent.change(screen.getByLabelText("Remote address"), { target: { value: "https://github.com/octo/repo.git" } });
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Save remote" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("github.com is not an allowed Git host. Allowed: gitlab.internal, gitea.internal.");
    expect(alert.textContent).toContain("Use a listed host, or ask an administrator to add this one.");
  });
});

describe("Admin → Git hosts", () => {
  it("lists the allowlist, adds a host and refuses a public one", async () => {
    const api = new FakeGitApi();
    render(<GitHostsAdmin api={api} />);
    expect((await screen.findByLabelText("gitlab.internal")).textContent).toContain("ssh waits for a pinned host key");
    fireEvent.change(screen.getByLabelText("Host name"), { target: { value: "gitlab-lab3" } });
    fireEvent.change(screen.getByLabelText("Hostname"), { target: { value: "GitLab.lab3.internal" } });
    fireEvent.change(screen.getByLabelText("SSH host key"), { target: { value: "gitlab.lab3.internal ssh-ed25519 AAAA" } });
    fireEvent.click(screen.getByRole("button", { name: "Allow host" }));
    expect((await screen.findByTestId("host-notice")).textContent).toBe(
      "gitlab.lab3.internal is now an allowed Git host over https and ssh. It applies now; nothing restarted.",
    );
    fireEvent.change(screen.getByLabelText("Host name"), { target: { value: "gh" } });
    fireEvent.change(screen.getByLabelText("Hostname"), { target: { value: "github.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Allow host" }));
    expect((await screen.findByRole("alert")).textContent).toContain("github.com is a public host and needs an ADR");
  });
});

describe("Git panel", () => {
  it("shows status, commits, history, pushes through the gate, bundles and runs the terminal", async () => {
    const api = new FakeGitApi();
    await api.addRemote({ name: "gitlab-firmware", uri: "https://gitlab.internal/firmware/bmc.git", authType: "pat", secret: TOKEN });
    render(<GitPanel api={api} slug="bmc" />);
    expect(await screen.findByText(/on slas\/T-coding-0001/)).toBeTruthy();
    expect(within(screen.getByLabelText("Changed files")).getByText("bmc/fan.py")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    fireEvent.change(screen.getByLabelText("Commit message"), { target: { value: "Tune fan curve" } });
    fireEvent.click(screen.getByRole("button", { name: "Commit changes" }));
    await waitFor(() => expect(screen.getByTestId("git-notice").textContent).toContain("Tune fan curve"));

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    const history = await screen.findByLabelText("History");
    expect(history.textContent).toContain("Tune fan curve");
    expect(history.textContent).toContain("agent · T-coding-0001");

    fireEvent.click(screen.getByRole("button", { name: "Push/Pull" }));
    fireEvent.click(await screen.findByRole("button", { name: "Push to gitlab-firmware" }));
    const result = await screen.findByTestId("push-result");
    expect(result.textContent).toContain("Pushed slas/T-coding-0001 to gitlab-firmware. Opened a review request:");
    expect(result.textContent).toContain("✓ No secret found by built-in patterns.");
    expect(result.textContent).not.toContain(TOKEN);

    fireEvent.click(screen.getByRole("button", { name: "Bundle" }));
    fireEvent.click(screen.getByRole("button", { name: "Export bundle" }));
    expect((await screen.findByTestId("bundle-info")).textContent).toContain("Bundle bmc-20260914-090000.bundle: 2 refs, 4,096 bytes");

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    fireEvent.change(screen.getByLabelText("Terminal input"), { target: { value: "git push origin main" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(screen.getByLabelText("Terminal transcript").textContent).toContain(PUSH_EXPLANATION));
  });

  it("says where to add a remote when there is none", async () => {
    render(<GitPanel api={new FakeGitApi()} slug="bmc" />);
    fireEvent.click(await screen.findByRole("button", { name: "Push/Pull" }));
    expect(await screen.findByText(/Add one under Settings → Git remotes/)).toBeTruthy();
  });
});
