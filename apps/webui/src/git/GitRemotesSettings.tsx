import { useEffect, useState } from "react";

import { type AuthType, type GitApi, GitApiError, type RemoteView } from "./api";

// Settings → Git remotes (CLAUDE.md §5.7 UI): paste-only fields, never echoed back; after
// save only the fingerprint is shown; Test connection runs ls-remote in git-broker.
// Copy: docs/ui/git-remotes.md.

interface Props {
  api: GitApi;
}

const field =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm " +
  "dark:border-slate-700 dark:bg-slate-900";
const primary = "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900";
const secondary = "rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700";

export function GitRemotesSettings({ api }: Props) {
  const [remotes, setRemotes] = useState<RemoteView[] | null>(null);
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  const [authType, setAuthType] = useState<AuthType>("pat");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string[] | null>(null);
  const [rotating, setRotating] = useState<{ id: string; secret: string } | null>(null);

  useEffect(() => {
    void api.listRemotes().then(setRemotes);
  }, [api]);

  async function save() {
    setSaving(true);
    setProblem(null);
    setNotice(null);
    try {
      const remote = await api.addRemote({ name: name.trim(), uri: uri.trim(), authType, secret });
      setRemotes((current) => [...(current ?? []), remote]);
      setNotice(
        `Saved ${remote.name}. The ${authType === "pat" ? "token" : "key"} is stored encrypted; ` +
          `only its fingerprint ${remote.fingerprint} is shown from now on.`,
      );
      setName("");
      setUri("");
      setSecret("");
    } catch (error: unknown) {
      setProblem(
        error instanceof GitApiError
          ? [error.parts.whatHappened, error.parts.likelyCause, error.parts.whatToDo]
          : ["The remote wasn't saved.", "The api service didn't answer.", "Try again; if it repeats, run `slas logs api` on the host."],
      );
    } finally {
      setSaving(false);
    }
  }

  async function test(remote: RemoteView) {
    setNotice(`Testing ${remote.name}…`);
    const result = await api.testConnection(remote.id);
    setNotice(result.sentence);
    setRemotes(await api.listRemotes());
  }

  async function rotate() {
    if (rotating === null) {
      return;
    }
    const updated = await api.rotateRemote(rotating.id, rotating.secret);
    setRemotes((current) => (current ?? []).map((r) => (r.id === updated.id ? updated : r)));
    setNotice(`Rotated ${updated.name}; the new fingerprint is ${updated.fingerprint}.`);
    setRotating(null);
  }

  async function remove(remote: RemoteView) {
    setNotice(await api.deleteRemote(remote.id));
    setRemotes((current) => (current ?? []).filter((r) => r.id !== remote.id));
  }

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Git remotes</h1>
        <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
          Repositories you can push to and pull from. The credential is stored encrypted and used
          only inside git-broker; the sandbox never holds it, and this page never shows it again.
        </p>
      </header>

      <section aria-labelledby="remotes-list-heading">
        <h2 id="remotes-list-heading" className="text-lg font-medium">
          Your remotes
        </h2>
        {remotes === null ? (
          <p className="text-sm text-slate-600">Loading remotes…</p>
        ) : remotes.length === 0 ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">
            You have no remote yet. Add one below; a deploy key or a project token is better than a
            personal credential.
          </p>
        ) : (
          <ul className="mt-2 space-y-3">
            {remotes.map((remote) => (
              <li key={remote.id} className="rounded-lg border border-slate-200 p-3 dark:border-slate-800" aria-label={remote.name}>
                <p className="text-sm">
                  <span className="font-medium">{remote.name}</span> · {remote.uri} ·{" "}
                  {remote.authType === "pat" ? "token" : "SSH key"}{" "}
                  <span className="font-mono">{remote.fingerprint}</span> · default branch {remote.defaultBranch}
                  {remote.lastUsed ? ` · last used ${remote.lastUsed}` : " · never used"}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" className={secondary} onClick={() => void test(remote)}>
                    Test connection
                  </button>
                  <button type="button" className={secondary} onClick={() => setRotating({ id: remote.id, secret: "" })}>
                    Rotate
                  </button>
                  <button type="button" className={secondary} onClick={() => void remove(remote)}>
                    Delete
                  </button>
                </div>
                {rotating?.id === remote.id && (
                  <div className="mt-2 flex items-end gap-2">
                    <label className="block flex-1 text-sm">
                      New {remote.authType === "pat" ? "token" : "private key"} (paste; it is never shown again)
                      <input
                        aria-label={`New credential for ${remote.name}`}
                        className={field}
                        type="password"
                        autoComplete="off"
                        value={rotating.secret}
                        onChange={(event) => setRotating({ id: remote.id, secret: event.target.value })}
                      />
                    </label>
                    <button type="button" className={primary} disabled={rotating.secret.trim() === ""} onClick={() => void rotate()}>
                      Save new credential
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="add-remote-heading" className="space-y-3">
        <h2 id="add-remote-heading" className="text-lg font-medium">
          Add a remote
        </h2>
        <label className="block text-sm">
          Name
          <input aria-label="Remote name" className={field} placeholder="gitlab-firmware" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          Address
          <input
            aria-label="Remote address"
            className={field}
            placeholder="https://gitlab.internal/firmware/bmc.git or git@gitlab.internal:firmware/bmc.git"
            value={uri}
            onChange={(e) => setUri(e.target.value)}
          />
        </label>
        <fieldset className="text-sm">
          <legend className="font-medium">Credential</legend>
          {(["pat", "ssh_key"] as AuthType[]).map((value) => (
            <label key={value} className="mr-4 inline-flex items-center gap-2">
              <input type="radio" name="auth" checked={authType === value} onChange={() => setAuthType(value)} />
              {value === "pat" ? "Token (https)" : "SSH private key (ssh)"}
            </label>
          ))}
        </fieldset>
        <label className="block text-sm">
          {authType === "pat" ? "Token" : "Private key"} — paste only; it is stored encrypted and never shown again
          {authType === "pat" ? (
            <input
              aria-label="Token"
              className={field}
              type="password"
              autoComplete="off"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
          ) : (
            <textarea
              aria-label="Private key"
              className={`${field} min-h-24 font-mono`}
              autoComplete="off"
              spellCheck={false}
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
            />
          )}
        </label>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Prefer a deploy key or a project token over a personal credential; it can be revoked without
          touching your account.
        </p>
        <button
          type="button"
          className={primary}
          disabled={saving || name.trim() === "" || uri.trim() === "" || secret.trim() === ""}
          onClick={() => void save()}
        >
          {saving ? "Saving…" : "Save remote"}
        </button>
        {notice !== null && (
          <p role="status" className="text-sm text-slate-700 dark:text-slate-300" data-testid="notice">
            {notice}
          </p>
        )}
        {problem !== null && (
          <div role="alert" className="text-sm text-red-700 dark:text-red-300">
            {problem.map((line) => (
              <p key={line}>{line}</p>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
