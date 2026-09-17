import { describe, expect, it } from "vitest";

import { expectRequestedWith, fetchWith, threePart } from "../api/testing";
import { serverNotAnswering } from "../copy/en";
import { GitApiError } from "./api";
import { HttpGitApi } from "./http";

const TOKEN = "glpat-Zq8xw2Yv7Rt4Ks9Lm3Np6Bd";

const REMOTE_WIRE = {
  id: "rem-0123456789abcdef",
  name: "gitlab-firmware",
  uri: "https://gitlab.internal/firmware/bmc.git",
  host: "gitlab.internal",
  auth_type: "pat",
  fingerprint: "…p6Bd",
  default_branch: "main",
  last_used: null,
  expires_at: null,
  sentence: "gitlab-firmware: https://gitlab.internal/firmware/bmc.git, token …p6Bd, default branch main.",
};

describe("HttpGitApi (contract §7, §8 /api/v1/git)", () => {
  it("adds a remote by POSTing the secret once and reads back only the fingerprint", async () => {
    const fake = fetchWith(() => REMOTE_WIRE);
    const remote = await new HttpGitApi(fake.http).addRemote({ name: "gitlab-firmware", uri: "https://gitlab.internal/firmware/bmc.git", authType: "pat", secret: TOKEN });
    const call = fake.only();
    expect(call).toMatchObject({ method: "POST", url: "/api/v1/git/remotes", body: { name: "gitlab-firmware", uri: "https://gitlab.internal/firmware/bmc.git", auth_type: "pat", secret: TOKEN } });
    expectRequestedWith(call);
    expect(remote).toEqual({
      id: "rem-0123456789abcdef",
      name: "gitlab-firmware",
      uri: "https://gitlab.internal/firmware/bmc.git",
      host: "gitlab.internal",
      authType: "pat",
      fingerprint: "…p6Bd",
      defaultBranch: "main",
      lastUsed: null,
      expiresAt: null,
    });
    expect(JSON.stringify(remote)).not.toContain(TOKEN);
  });

  it("lists, rotates, tests and deletes remotes on the contract's paths", async () => {
    const fake = fetchWith((call) => {
      if (call.url.endsWith("/test")) {
        return { ok: true, sentence: "Connected to gitlab-firmware: 3 branches, including main." };
      }
      if (call.method === "DELETE") {
        return { sentence: "Removed gitlab-firmware; its credential was deleted with it." };
      }
      return call.method === "GET" ? [REMOTE_WIRE] : { ...REMOTE_WIRE, fingerprint: "…1234", last_used: "2026-09-14T09:00:00Z" };
    });
    const api = new HttpGitApi(fake.http);
    expect((await api.listRemotes()).map((r) => r.name)).toEqual(["gitlab-firmware"]);
    expect(fake.calls[0]).toMatchObject({ method: "GET", url: "/api/v1/git/remotes" });
    expectRequestedWith(fake.calls[0] as never);

    const rotated = await api.rotateRemote("rem-0123456789abcdef", "glpat-New1234");
    expect(fake.calls[1]).toMatchObject({ method: "POST", url: "/api/v1/git/remotes/rem-0123456789abcdef/rotate", body: { secret: "glpat-New1234" } });
    expect(rotated).toMatchObject({ fingerprint: "…1234", lastUsed: "2026-09-14T09:00:00Z" });

    expect(await api.testConnection("rem-0123456789abcdef")).toEqual({ ok: true, sentence: "Connected to gitlab-firmware: 3 branches, including main." });
    expect(fake.calls[2]).toMatchObject({ method: "POST", url: "/api/v1/git/remotes/rem-0123456789abcdef/test", body: {} });

    expect(await api.deleteRemote("rem-0123456789abcdef")).toBe("Removed gitlab-firmware; its credential was deleted with it.");
    expect(fake.calls[3]).toMatchObject({ method: "DELETE", url: "/api/v1/git/remotes/rem-0123456789abcdef" });
    expectRequestedWith(fake.calls[3] as never);
  });

  it("turns a three-part refusal into the GitApiError the pages render", async () => {
    const fake = fetchWith(() =>
      threePart(400, "github.com is not an allowed Git host. Allowed: gitlab.internal, gitea.internal.", "git-broker reaches only listed hosts.", "Use a listed host, or ask an administrator to add this one."),
    );
    const error = (await new HttpGitApi(fake.http).addRemote({ name: "gh", uri: "https://github.com/o/r.git", authType: "pat", secret: TOKEN }).catch((e: unknown) => e)) as GitApiError;
    expect(error).toBeInstanceOf(GitApiError);
    expect(error.parts).toEqual({
      whatHappened: "github.com is not an allowed Git host. Allowed: gitlab.internal, gitea.internal.",
      likelyCause: "git-broker reaches only listed hosts.",
      whatToDo: "Use a listed host, or ask an administrator to add this one.",
    });
    const down = fetchWith(() => {
      throw new TypeError("Failed to fetch");
    });
    const unreachable = (await new HttpGitApi(down.http).listHosts().catch((e: unknown) => e)) as GitApiError;
    expect(unreachable).toBeInstanceOf(GitApiError);
    expect(unreachable.parts).toEqual(serverNotAnswering);
  });

  it("lists and adds hosts, deriving whether the SSH host key is pinned", async () => {
    const fake = fetchWith((call) =>
      call.method === "GET"
        ? [
            { name: "gitlab-internal", hostname: "gitlab.internal", kind: "gitlab", api_base: "https://gitlab.internal/api/v4", protocols: ["https", "ssh"], ssh_host_key: null, https_username: "oauth2", note: "Quickstart default.", sentence: "" },
          ]
        : { name: "gitlab-lab3", hostname: "gitlab.lab3.internal", kind: "gitlab", api_base: "https://gitlab.lab3.internal/api/v4", protocols: ["https", "ssh"], ssh_host_key: "gitlab.lab3.internal ssh-ed25519 AAAA", note: "" },
    );
    const api = new HttpGitApi(fake.http);
    expect(await api.listHosts()).toEqual([
      { name: "gitlab-internal", hostname: "gitlab.internal", kind: "gitlab", apiBase: "https://gitlab.internal/api/v4", protocols: ["https", "ssh"], sshHostKeyPinned: false, note: "Quickstart default." },
    ]);
    const added = await api.addHost({ name: "gitlab-lab3", hostname: "gitlab.lab3.internal", kind: "gitlab", sshHostKey: "gitlab.lab3.internal ssh-ed25519 AAAA" });
    expect(fake.calls[1]).toMatchObject({
      method: "POST",
      url: "/api/v1/git/hosts",
      body: { name: "gitlab-lab3", hostname: "gitlab.lab3.internal", kind: "gitlab", ssh_host_key: "gitlab.lab3.internal ssh-ed25519 AAAA" },
    });
    expect(added.sshHostKeyPinned).toBe(true);
    await api.addHost({ name: "plain", hostname: "git.internal", kind: "generic", sshHostKey: "  " });
    expect(fake.calls[2]?.body).toMatchObject({ ssh_host_key: null });
  });

  it("reads status, commits, history, pushes through the gate, pulls, bundles and runs the terminal", async () => {
    const fake = fetchWith((call) => {
      if (call.url.endsWith("/status")) {
        return { branch: "slas/T-coding-0001", entries: [{ path: "bmc/fan.py", state: "M" }] };
      }
      if (call.url.endsWith("/commit")) {
        return { sha: "f".repeat(40), subject: "Tune fan curve", author: "Pat Lin <pat@slas.local>", when: "2026-09-17T08:00:00Z", by_agent: false, ticket_id: null };
      }
      if (call.url.endsWith("/history")) {
        return [{ sha: "a".repeat(40), subject: "Add fan control", author: "Coding Agent", when: "2026-09-16T08:00:00Z", by_agent: true, ticket_id: "T-coding-0001" }];
      }
      if (call.url.endsWith("/push")) {
        return {
          branch: "slas/T-coding-0001",
          sha: "a".repeat(40),
          gate: [
            { name: "path_scope", ok: true, sentence: "Every change stays inside the project." },
            { name: "secrets", ok: true, sentence: "No secret found by built-in patterns." },
          ],
          merge_request: { url: "https://gitlab.internal/firmware/bmc/-/merge_requests/1", number: 1, title: "Tune fan curve" },
          review_url: "https://gitlab.internal/firmware/bmc/-/merge_requests/1",
          sentence: "Pushed slas/T-coding-0001 to gitlab-firmware. Opened a review request: https://gitlab.internal/firmware/bmc/-/merge_requests/1",
        };
      }
      if (call.url.endsWith("/pull")) {
        return { sentence: "Pulled main from gitlab-firmware; bmc is up to date." };
      }
      if (call.url.endsWith("/bundle/export")) {
        return { path: "/data/Coding/pat/Bundles/bmc-20260914-090000.bundle", sha256: "0".repeat(64), size_bytes: 4096, refs: ["refs/heads/main"] };
      }
      if (call.url.endsWith("/bundle/import")) {
        return { sentences: ["Imported main from incoming.bundle as bundle/main."] };
      }
      return { n: 1, at: "2026-09-17T08:00:00Z", command: "git push origin main", output: "fatal: no route from the sandbox.", exit_code: 128 };
    });
    const api = new HttpGitApi(fake.http);
    expect(await api.status("bmc")).toEqual({ branch: "slas/T-coding-0001", entries: [{ code: "M", path: "bmc/fan.py" }] });
    expect(await api.commit("bmc", "Tune fan curve")).toEqual({
      sha: "f".repeat(40),
      subject: "Tune fan curve",
      byAgent: false,
      ticketId: null,
      author: "Pat Lin <pat@slas.local>",
      when: "2026-09-17T08:00:00Z",
    });
    expect((await api.history("bmc"))[0]).toMatchObject({ byAgent: true, ticketId: "T-coding-0001" });
    expect(await api.push("bmc", "rem-0123456789abcdef", "slas/T-coding-0001")).toEqual({
      ok: true,
      sentence: "Pushed slas/T-coding-0001 to gitlab-firmware. Opened a review request: https://gitlab.internal/firmware/bmc/-/merge_requests/1",
      checks: [
        { name: "path_scope", ok: true, sentence: "Every change stays inside the project." },
        { name: "secrets", ok: true, sentence: "No secret found by built-in patterns." },
      ],
      reviewUrl: "https://gitlab.internal/firmware/bmc/-/merge_requests/1",
    });
    expect(await api.pull("bmc", "rem-0123456789abcdef")).toBe("Pulled main from gitlab-firmware; bmc is up to date.");
    expect(await api.exportBundle("bmc")).toEqual({ fileName: "bmc-20260914-090000.bundle", refs: ["refs/heads/main"], sizeBytes: 4096, sha256: "0".repeat(64) });
    expect(await api.importBundle("bmc", "incoming.bundle")).toEqual(["Imported main from incoming.bundle as bundle/main."]);
    expect(await api.terminal("bmc", "git push origin main")).toEqual({ command: "git push origin main", output: "fatal: no route from the sandbox.", exitCode: 128 });

    expect(fake.calls.map((c) => [c.method, c.url, c.body])).toEqual([
      ["GET", "/api/v1/git/projects/bmc/status", undefined],
      ["POST", "/api/v1/git/projects/bmc/commit", { subject: "Tune fan curve" }],
      ["GET", "/api/v1/git/projects/bmc/history", undefined],
      ["POST", "/api/v1/git/projects/bmc/push", { remote_id: "rem-0123456789abcdef", branch: "slas/T-coding-0001" }],
      ["POST", "/api/v1/git/projects/bmc/pull", { remote_id: "rem-0123456789abcdef" }],
      ["POST", "/api/v1/git/projects/bmc/bundle/export", {}],
      ["POST", "/api/v1/git/projects/bmc/bundle/import", { file_name: "incoming.bundle" }],
      ["POST", "/api/v1/git/projects/bmc/terminal", { line: "git push origin main" }],
    ]);
    for (const call of fake.calls) {
      expectRequestedWith(call);
    }
  });

  it("shows a push the gate refused in three parts as a failed result, and a GateReport's checks", async () => {
    const refused = fetchWith(() => threePart(409, "The push was refused by the validation gate.", "A hook file was added.", "Remove .git/hooks changes and push again."));
    expect(await new HttpGitApi(refused.http).push("bmc", "rem-0123456789abcdef", "main")).toEqual({
      ok: false,
      sentence: "The push was refused by the validation gate. A hook file was added. Remove .git/hooks changes and push again.",
      checks: [],
      reviewUrl: null,
    });
    const report = fetchWith(() => ({ sentence: "Refused.", gate: { checks: [{ name: "branch_policy", ok: false, sentence: "Protected branch." }], verdict: null }, merge_request: null }));
    expect(await new HttpGitApi(report.http).push("bmc", "rem-0123456789abcdef", "main")).toEqual({
      ok: false,
      sentence: "Refused.",
      checks: [{ name: "branch_policy", ok: false, sentence: "Protected branch." }],
      reviewUrl: null,
    });
  });
});
