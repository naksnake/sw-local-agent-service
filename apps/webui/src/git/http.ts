// The Git panel, Settings → Git remotes and Admin → Git hosts over HTTP (docs/api-contract-
// round-2.md §7, §8): /api/v1/git/remotes*, /git/hosts*, /git/projects/{slug}/*, the last of
// which the api forwards to git-broker (and the terminal to the sandbox manager). A secret is
// sent once, in a POST body, and never comes back: the api answers with a fingerprint (INV-14).

import { asApiError, type HttpClient } from "../api/http";
import { asRecord, bool, isRecord, nullableStr, num, oneOf, recordList, seg, str, strList, type Wire } from "../api/wire";
import { agents } from "../copy/en";
import {
  type AuthType,
  type BundleView,
  type CommitView,
  type GateCheckView,
  type GitApi,
  GitApiError,
  type GitHostView,
  type HostKind,
  type PushResultView,
  type RemoteView,
  type StatusEntry,
  type TerminalLineView,
} from "./api";

const AUTH_TYPES: readonly AuthType[] = ["pat", "ssh_key"];
const HOST_KINDS: readonly HostKind[] = ["gitlab", "gitea", "github", "generic"];
const PROTOCOLS: readonly ("https" | "ssh")[] = ["https", "ssh"];

/** The pages catch `GitApiError`; every api failure becomes one with the three parts to show. */
function asGitError(error: unknown): GitApiError {
  if (error instanceof GitApiError) {
    return error;
  }
  return new GitApiError(asApiError(error).parts);
}

export function remoteFromWire(wire: Wire): RemoteView {
  return {
    id: str(wire["id"]),
    name: str(wire["name"]),
    uri: str(wire["uri"]),
    host: str(wire["host"]),
    authType: oneOf(wire["auth_type"], AUTH_TYPES, "pat"),
    fingerprint: str(wire["fingerprint"]),
    defaultBranch: str(wire["default_branch"], "main"),
    lastUsed: nullableStr(wire["last_used"]),
    expiresAt: nullableStr(wire["expires_at"]),
  };
}

export function hostFromWire(wire: Wire): GitHostView {
  const key = nullableStr(wire["ssh_host_key"]);
  return {
    name: str(wire["name"]),
    hostname: str(wire["hostname"]),
    kind: oneOf(wire["kind"], HOST_KINDS, "generic"),
    apiBase: nullableStr(wire["api_base"]),
    protocols: strList(wire["protocols"]).filter((p): p is "https" | "ssh" => (PROTOCOLS as readonly string[]).includes(p)),
    sshHostKeyPinned: bool(wire["ssh_host_key_pinned"], key !== null && key.trim() !== ""),
    note: str(wire["note"]),
  };
}

export function commitFromWire(wire: Wire): CommitView {
  return {
    sha: str(wire["sha"]),
    subject: str(wire["subject"]),
    byAgent: bool(wire["by_agent"]),
    ticketId: nullableStr(wire["ticket_id"]),
    author: str(wire["author"]),
    when: str(wire["when"]),
  };
}

function checkFromWire(wire: Wire): GateCheckView {
  return { name: str(wire["name"]), ok: bool(wire["ok"]), sentence: str(wire["sentence"]) };
}

/** `PushResult` plus `gate` as a check list (or a GateReport with `checks`) and `review_url`. */
export function pushResultFromWire(value: unknown): PushResultView {
  const w = asRecord(value);
  const gate = w["gate"];
  const checks = recordList(Array.isArray(gate) ? gate : asRecord(gate)["checks"]).map(checkFromWire);
  const mergeRequest = asRecord(w["merge_request"]);
  return {
    ok: bool(w["ok"], checks.every((c) => c.ok)),
    sentence: str(w["sentence"]),
    checks,
    reviewUrl: nullableStr(w["review_url"]) ?? nullableStr(mergeRequest["url"]),
  };
}

export function bundleFromWire(value: unknown): BundleView {
  const w = asRecord(value);
  const path = str(w["path"]);
  return {
    fileName: str(w["file_name"], path.split("/").pop() ?? path),
    refs: strList(w["refs"]),
    sizeBytes: num(w["size_bytes"]),
    sha256: str(w["sha256"]),
  };
}

export function terminalLineFromWire(value: unknown, command: string): TerminalLineView {
  const w = asRecord(value);
  return { command: str(w["command"], command), output: str(w["output"]), exitCode: num(w["exit_code"]) };
}

export class HttpGitApi implements GitApi {
  constructor(private readonly http: HttpClient) {}

  private async call<T>(work: () => Promise<T>): Promise<T> {
    try {
      return await work();
    } catch (error: unknown) {
      throw asGitError(error);
    }
  }

  listRemotes(): Promise<RemoteView[]> {
    return this.call(async () => recordList(await this.http.get<unknown>("/git/remotes")).map(remoteFromWire));
  }

  addRemote(input: { name: string; uri: string; authType: AuthType; secret: string }): Promise<RemoteView> {
    const body = { name: input.name, uri: input.uri, auth_type: input.authType, secret: input.secret };
    return this.call(async () => remoteFromWire(asRecord(await this.http.post<unknown>("/git/remotes", body))));
  }

  rotateRemote(id: string, secret: string): Promise<RemoteView> {
    return this.call(async () => remoteFromWire(asRecord(await this.http.post<unknown>(`/git/remotes/${seg(id)}/rotate`, { secret }))));
  }

  deleteRemote(id: string): Promise<string> {
    return this.call(async () => str(asRecord(await this.http.del<unknown>(`/git/remotes/${seg(id)}`))["sentence"]));
  }

  testConnection(id: string): Promise<{ ok: boolean; sentence: string }> {
    return this.call(async () => {
      const w = asRecord(await this.http.post<unknown>(`/git/remotes/${seg(id)}/test`, {}));
      return { ok: bool(w["ok"]), sentence: str(w["sentence"]) };
    });
  }

  listHosts(): Promise<GitHostView[]> {
    return this.call(async () => recordList(await this.http.get<unknown>("/git/hosts")).map(hostFromWire));
  }

  addHost(input: { name: string; hostname: string; kind: HostKind; sshHostKey: string }): Promise<GitHostView> {
    const body = { name: input.name, hostname: input.hostname, kind: input.kind, ssh_host_key: input.sshHostKey.trim() || null };
    return this.call(async () => hostFromWire(asRecord(await this.http.post<unknown>("/git/hosts", body))));
  }

  status(slug: string): Promise<{ branch: string; entries: StatusEntry[] }> {
    return this.call(async () => {
      const w = asRecord(await this.http.get<unknown>(`/git/projects/${seg(slug)}/status`));
      return {
        branch: str(w["branch"]),
        entries: recordList(w["entries"]).map((e) => ({ code: str(e["state"], str(e["code"])), path: str(e["path"]) })),
      };
    });
  }

  commit(slug: string, subject: string): Promise<CommitView> {
    return this.call(async () => commitFromWire(asRecord(await this.http.post<unknown>(`/git/projects/${seg(slug)}/commit`, { subject }))));
  }

  history(slug: string): Promise<CommitView[]> {
    return this.call(async () => recordList(await this.http.get<unknown>(`/git/projects/${seg(slug)}/history`)).map(commitFromWire));
  }

  /** A gate refusal answered in three parts is shown as a failed push, not thrown. */
  async push(slug: string, remoteId: string, branch: string): Promise<PushResultView> {
    try {
      return pushResultFromWire(await this.http.post<unknown>(`/git/projects/${seg(slug)}/push`, { remote_id: remoteId, branch }));
    } catch (error: unknown) {
      const apiError = asApiError(error);
      if (apiError.status === 401 || apiError.unreachable) {
        throw asGitError(apiError);
      }
      return { ok: false, sentence: agents.pushRefused(apiError.parts), checks: [], reviewUrl: null };
    }
  }

  pull(slug: string, remoteId: string): Promise<string> {
    return this.call(async () => str(asRecord(await this.http.post<unknown>(`/git/projects/${seg(slug)}/pull`, { remote_id: remoteId }))["sentence"]));
  }

  exportBundle(slug: string): Promise<BundleView> {
    return this.call(async () => bundleFromWire(await this.http.post<unknown>(`/git/projects/${seg(slug)}/bundle/export`, {})));
  }

  importBundle(slug: string, fileName: string): Promise<string[]> {
    return this.call(async () => {
      const w = asRecord(await this.http.post<unknown>(`/git/projects/${seg(slug)}/bundle/import`, { file_name: fileName }));
      return strList(w["sentences"]);
    });
  }

  terminal(slug: string, line: string): Promise<TerminalLineView> {
    return this.call(async () => {
      const answer = await this.http.post<unknown>(`/git/projects/${seg(slug)}/terminal`, { line });
      return terminalLineFromWire(isRecord(answer) ? answer : {}, line);
    });
  }
}
