// The Git panel, Settings → Git remotes and Admin → Git hosts as the UI sees them
// (CLAUDE.md §5.7, §9). Nothing here ever carries a credential: the API returns a
// fingerprint after save and the UI's paste-only fields are never echoed back.
// `FakeGitApi` stands in until apps/api and git-broker expose these calls over HTTP.

export type AuthType = "pat" | "ssh_key";
export type HostKind = "gitlab" | "gitea" | "github" | "generic";

export interface RemoteView {
  id: string;
  name: string;
  uri: string;
  host: string;
  authType: AuthType;
  /** "…m3Np" for a token, "SHA256:…" for a key. Never the credential. */
  fingerprint: string;
  defaultBranch: string;
  lastUsed: string | null;
}

export interface GitHostView {
  name: string;
  hostname: string;
  kind: HostKind;
  apiBase: string | null;
  protocols: ("https" | "ssh")[];
  sshHostKeyPinned: boolean;
  note: string;
}

export interface StatusEntry {
  code: string;
  path: string;
}

export interface CommitView {
  sha: string;
  subject: string;
  byAgent: boolean;
  ticketId: string | null;
}

export interface GateCheckView {
  name: string;
  ok: boolean;
  sentence: string;
}

export interface PushResultView {
  ok: boolean;
  sentence: string;
  checks: GateCheckView[];
  reviewUrl: string | null;
}

export interface BundleView {
  fileName: string;
  refs: string[];
  sizeBytes: number;
  sha256: string;
}

export interface TerminalLineView {
  command: string;
  output: string;
  exitCode: number;
}

export interface ThreePart {
  whatHappened: string;
  likelyCause: string;
  whatToDo: string;
}

export class GitApiError extends Error {
  readonly parts: ThreePart;
  constructor(parts: ThreePart) {
    super(parts.whatHappened);
    this.parts = parts;
  }
}

export interface GitApi {
  listRemotes(): Promise<RemoteView[]>;
  addRemote(input: { name: string; uri: string; authType: AuthType; secret: string }): Promise<RemoteView>;
  rotateRemote(id: string, secret: string): Promise<RemoteView>;
  deleteRemote(id: string): Promise<string>;
  testConnection(id: string): Promise<{ ok: boolean; sentence: string }>;
  listHosts(): Promise<GitHostView[]>;
  addHost(input: { name: string; hostname: string; kind: HostKind; sshHostKey: string }): Promise<GitHostView>;
  status(slug: string): Promise<{ branch: string; entries: StatusEntry[] }>;
  commit(slug: string, subject: string): Promise<CommitView>;
  history(slug: string): Promise<CommitView[]>;
  push(slug: string, remoteId: string, branch: string): Promise<PushResultView>;
  pull(slug: string, remoteId: string): Promise<string>;
  exportBundle(slug: string): Promise<BundleView>;
  importBundle(slug: string, fileName: string): Promise<string[]>;
  terminal(slug: string, line: string): Promise<TerminalLineView>;
}

export const PUSH_EXPLANATION = "Push happens from the Git panel, which uses your saved remote.";

const TOKEN_SHAPE = /^[A-Za-z0-9_\-.]{8,}$/;

export class FakeGitApi implements GitApi {
  remotes: RemoteView[] = [];
  hosts: GitHostView[] = [
    {
      name: "gitlab-internal",
      hostname: "gitlab.internal",
      kind: "gitlab",
      apiBase: "https://gitlab.internal/api/v4",
      protocols: ["https", "ssh"],
      sshHostKeyPinned: false,
      note: "Quickstart default. Pin the SSH host key before allowing ssh.",
    },
    {
      name: "gitea-internal",
      hostname: "gitea.internal",
      kind: "gitea",
      apiBase: "https://gitea.internal/api/v1",
      protocols: ["https"],
      sshHostKeyPinned: false,
      note: "Quickstart default.",
    },
  ];
  commits: CommitView[] = [
    { sha: "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0", subject: "Add fan control", byAgent: true, ticketId: "T-coding-0001" },
  ];
  changed: StatusEntry[] = [{ code: "M", path: "bmc/fan.py" }];
  /** Secrets that were pasted, kept only to prove the UI never shows them. */
  readonly pasted: string[] = [];
  private counter = 0;

  async listRemotes(): Promise<RemoteView[]> {
    return [...this.remotes];
  }

  async addRemote(input: { name: string; uri: string; authType: AuthType; secret: string }): Promise<RemoteView> {
    this.pasted.push(input.secret);
    const hostname = (/^(?:https:\/\/|ssh:\/\/[^@]+@|[^@]+@)([^/:]+)/.exec(input.uri.trim()) ?? [])[1] ?? "";
    const known = this.hosts.find((h) => h.hostname === hostname);
    if (!known) {
      throw new GitApiError({
        whatHappened: `${hostname || input.uri} is not an allowed Git host. Allowed: ${this.hosts.map((h) => h.hostname).join(", ")}.`,
        likelyCause: "git-broker reaches only the hosts an administrator listed under Admin → Git hosts.",
        whatToDo: "Use a listed host, or ask an administrator to add this one.",
      });
    }
    if (input.authType === "pat" && !TOKEN_SHAPE.test(input.secret.trim())) {
      throw new GitApiError({
        whatHappened: "The pasted value does not look like a token.",
        likelyCause: "A token is one line of at least 8 characters.",
        whatToDo: "Paste the token again, or choose SSH key.",
      });
    }
    this.counter += 1;
    const remote: RemoteView = {
      id: `rem-${String(this.counter).padStart(4, "0")}`,
      name: input.name,
      uri: input.uri.trim(),
      host: hostname,
      authType: input.authType,
      fingerprint: input.authType === "pat" ? `…${input.secret.trim().slice(-4)}` : "SHA256:fAkEfInGeRpRiNt",
      defaultBranch: "main",
      lastUsed: null,
    };
    this.remotes.push(remote);
    return remote;
  }

  async rotateRemote(id: string, secret: string): Promise<RemoteView> {
    this.pasted.push(secret);
    const remote = this.remotes.find((r) => r.id === id);
    if (!remote) {
      throw new GitApiError({ whatHappened: "That remote is not one of yours.", likelyCause: "It was deleted.", whatToDo: "Add it again." });
    }
    remote.fingerprint = remote.authType === "pat" ? `…${secret.trim().slice(-4)}` : "SHA256:rOtAtEdKeY";
    return { ...remote };
  }

  async deleteRemote(id: string): Promise<string> {
    const remote = this.remotes.find((r) => r.id === id);
    this.remotes = this.remotes.filter((r) => r.id !== id);
    return `Removed ${remote?.name ?? "the remote"}; its credential was deleted with it.`;
  }

  async testConnection(id: string): Promise<{ ok: boolean; sentence: string }> {
    const remote = this.remotes.find((r) => r.id === id);
    if (!remote) {
      return { ok: false, sentence: "That remote is not one of yours." };
    }
    remote.lastUsed = "2026-09-14T09:00:00Z";
    return { ok: true, sentence: `Connected to ${remote.name}: 3 branches, including main.` };
  }

  async listHosts(): Promise<GitHostView[]> {
    return [...this.hosts];
  }

  async addHost(input: { name: string; hostname: string; kind: HostKind; sshHostKey: string }): Promise<GitHostView> {
    if (/(^|\.)(github\.com|gitlab\.com)$/.test(input.hostname)) {
      throw new GitApiError({
        whatHappened: `${input.hostname} is a public host and needs an ADR before it can be allowed.`,
        likelyCause: "Reaching a public Git host is an exception to INV-1 (no external network dependency).",
        whatToDo: "Write the ADR under docs/adr, then add the host.",
      });
    }
    const host: GitHostView = {
      name: input.name,
      hostname: input.hostname,
      kind: input.kind,
      apiBase: input.kind === "generic" ? null : `https://${input.hostname}/api`,
      protocols: input.sshHostKey.trim() ? ["https", "ssh"] : ["https"],
      sshHostKeyPinned: input.sshHostKey.trim() !== "",
      note: "",
    };
    this.hosts.push(host);
    return host;
  }

  async status(_slug: string): Promise<{ branch: string; entries: StatusEntry[] }> {
    return { branch: "slas/T-coding-0001", entries: [...this.changed] };
  }

  async commit(_slug: string, subject: string): Promise<CommitView> {
    const commit: CommitView = { sha: `${this.commits.length + 1}`.padStart(40, "f"), subject, byAgent: false, ticketId: null };
    this.commits.unshift(commit);
    this.changed = [];
    return commit;
  }

  async history(_slug: string): Promise<CommitView[]> {
    return [...this.commits];
  }

  async push(_slug: string, remoteId: string, branch: string): Promise<PushResultView> {
    const remote = this.remotes.find((r) => r.id === remoteId);
    if (!remote) {
      return { ok: false, sentence: "Pick a saved remote first.", checks: [], reviewUrl: null };
    }
    const checks: GateCheckView[] = [
      { name: "path_scope", ok: true, sentence: "Every change stays inside the project." },
      { name: "hooks", ok: true, sentence: "No hook files were added." },
      { name: "secrets", ok: true, sentence: "No secret found by built-in patterns." },
    ];
    if (branch === remote.defaultBranch) {
      checks.push({
        name: "branch_policy",
        ok: false,
        sentence: "Direct pushes to a protected branch need git:push_protected, which is off by default; push to a branch and open a merge request instead.",
      });
      return { ok: false, sentence: "The push was refused by the validation gate.", checks, reviewUrl: null };
    }
    return {
      ok: true,
      sentence: `Pushed ${branch} to ${remote.name}. Opened a review request: https://${remote.host}/${_slug}/-/merge_requests/1`,
      checks,
      reviewUrl: `https://${remote.host}/${_slug}/-/merge_requests/1`,
    };
  }

  async pull(slug: string, remoteId: string): Promise<string> {
    const remote = this.remotes.find((r) => r.id === remoteId);
    return remote ? `Pulled main from ${remote.name}; ${slug} is up to date.` : "Pick a saved remote first.";
  }

  async exportBundle(slug: string): Promise<BundleView> {
    return { fileName: `${slug}-20260914-090000.bundle`, refs: ["refs/heads/main", "refs/heads/slas/T-coding-0001"], sizeBytes: 4096, sha256: "0".repeat(64) };
  }

  async importBundle(_slug: string, _fileName: string): Promise<string[]> {
    return ["main", "feature"];
  }

  async terminal(_slug: string, line: string): Promise<TerminalLineView> {
    if (/^\s*git\s+push\b/.test(line)) {
      return {
        command: line,
        output: `fatal: unable to access the remote: no route from the sandbox.\n${PUSH_EXPLANATION}`,
        exitCode: 128,
      };
    }
    return { command: line, output: `ran inside the sandbox: ${line}`, exitCode: 0 };
  }
}
