import { useEffect, useState } from "react";

import type { BundleView, CommitView, GitApi, PushResultView, RemoteView, StatusEntry, TerminalLineView } from "./api";

// The per-project Git panel (CLAUDE.md §5.7, §9): Status · Commit · History · Push/Pull ·
// Bundle, plus the Terminal tab that runs inside the sandbox. Copy: docs/ui/git-panel.md.

interface Props {
  api: GitApi;
  slug: string;
}

type Tab = "status" | "commit" | "history" | "sync" | "bundle" | "terminal";
const TABS: { id: Tab; label: string }[] = [
  { id: "status", label: "Status" },
  { id: "commit", label: "Commit" },
  { id: "history", label: "History" },
  { id: "sync", label: "Push/Pull" },
  { id: "bundle", label: "Bundle" },
  { id: "terminal", label: "Terminal" },
];

const field = "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
const primary = "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900";
const secondary = "rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700";

export function GitPanel({ api, slug }: Props) {
  const [tab, setTab] = useState<Tab>("status");
  const [branch, setBranch] = useState("");
  const [entries, setEntries] = useState<StatusEntry[]>([]);
  const [history, setHistory] = useState<CommitView[]>([]);
  const [remotes, setRemotes] = useState<RemoteView[]>([]);
  const [remoteId, setRemoteId] = useState("");
  const [subject, setSubject] = useState("");
  const [pushResult, setPushResult] = useState<PushResultView | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [bundle, setBundle] = useState<BundleView | null>(null);
  const [lines, setLines] = useState<TerminalLineView[]>([]);
  const [input, setInput] = useState("");

  async function refresh() {
    const status = await api.status(slug);
    setBranch(status.branch);
    setEntries(status.entries);
    setHistory(await api.history(slug));
    const list = await api.listRemotes();
    setRemotes(list);
    setRemoteId((current) => current || list[0]?.id || "");
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, slug]);

  const remoteName = remotes.find((r) => r.id === remoteId)?.name;

  return (
    <section aria-label={`Git panel for ${slug}`} className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">
          Git · {slug} <span className="text-slate-500">on {branch || "…"}</span>
        </h3>
        <nav aria-label="Git panel tabs" className="flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-current={tab === t.id ? "page" : undefined}
              className={
                "rounded-md px-3 py-1 text-sm " +
                (tab === t.id ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900" : "text-slate-700 dark:text-slate-300")
              }
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="mt-4 space-y-3 text-sm">
        {tab === "status" &&
          (entries.length === 0 ? (
            <p>Nothing to commit; the working tree matches the last commit.</p>
          ) : (
            <ul aria-label="Changed files">
              {entries.map((e) => (
                <li key={e.path}>
                  <span className="font-mono text-slate-500">{e.code}</span> {e.path}
                </li>
              ))}
            </ul>
          ))}

        {tab === "commit" && (
          <div className="space-y-2">
            <p>
              {entries.length === 0
                ? "Nothing to commit."
                : `${entries.length} changed ${entries.length === 1 ? "file" : "files"} will be committed on ${branch} with your name.`}
            </p>
            <label className="block">
              Commit message
              <input aria-label="Commit message" className={field} value={subject} onChange={(e) => setSubject(e.target.value)} />
            </label>
            <button
              type="button"
              className={primary}
              disabled={subject.trim() === "" || entries.length === 0}
              onClick={async () => {
                const commit = await api.commit(slug, subject.trim());
                setNotice(`Committed ${commit.sha.slice(0, 10)}: ${commit.subject}`);
                setSubject("");
                await refresh();
              }}
            >
              Commit changes
            </button>
          </div>
        )}

        {tab === "history" && (
          <ol aria-label="History" className="space-y-1">
            {history.map((c) => (
              <li key={c.sha}>
                <span className="font-mono text-slate-500">{c.sha.slice(0, 10)}</span> {c.subject}
                {c.byAgent ? <span className="ml-2 rounded bg-slate-200 px-1.5 text-xs dark:bg-slate-700">agent · {c.ticketId}</span> : null}
              </li>
            ))}
          </ol>
        )}

        {tab === "sync" && (
          <div className="space-y-3">
            {remotes.length === 0 ? (
              <p>You have no saved remote. Add one under Settings → Git remotes; pushing happens from here, never from the sandbox.</p>
            ) : (
              <>
                <label className="block">
                  Remote
                  <select aria-label="Remote" className={field} value={remoteId} onChange={(e) => setRemoteId(e.target.value)}>
                    {remotes.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.name} ({r.authType === "pat" ? "token" : "key"} {r.fingerprint})
                      </option>
                    ))}
                  </select>
                </label>
                <p>
                  Push {branch} to {remoteName}: the validation gate runs first, then the branch is pushed and a review request is
                  opened where the host supports it. A human merges.
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={primary}
                    onClick={async () => {
                      setPushResult(await api.push(slug, remoteId, branch));
                    }}
                  >
                    Push to {remoteName}
                  </button>
                  <button type="button" className={secondary} onClick={async () => setNotice(await api.pull(slug, remoteId))}>
                    Pull
                  </button>
                </div>
                {pushResult !== null && (
                  <div role={pushResult.ok ? "status" : "alert"} data-testid="push-result">
                    <p>{pushResult.sentence}</p>
                    <ul className="mt-1 text-slate-600 dark:text-slate-400">
                      {pushResult.checks.map((c) => (
                        <li key={c.name}>
                          {c.ok ? "✓" : "✗"} {c.sentence}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {tab === "bundle" && (
          <div className="space-y-2">
            <p>A bundle carries this repository's history as one file for another site. Nothing is merged on import.</p>
            <div className="flex gap-2">
              <button type="button" className={primary} onClick={async () => setBundle(await api.exportBundle(slug))}>
                Export bundle
              </button>
              <button
                type="button"
                className={secondary}
                onClick={async () => {
                  const branches = await api.importBundle(slug, "incoming.bundle");
                  setNotice(`Imported ${branches.length} ${branches.length === 1 ? "branch" : "branches"} under bundle/: ${branches.join(", ")}.`);
                }}
              >
                Import bundle…
              </button>
            </div>
            {bundle !== null && (
              <p data-testid="bundle-info">
                Bundle {bundle.fileName}: {bundle.refs.length} refs, {bundle.sizeBytes.toLocaleString()} bytes, sha256{" "}
                {bundle.sha256.slice(0, 12)}…
              </p>
            )}
          </div>
        )}

        {tab === "terminal" && (
          <div className="space-y-2">
            <p className="text-slate-600 dark:text-slate-400">
              Runs inside your sandbox as the workspace user, with the same isolation as the agent. The session is recorded to the
              ticket with secrets redacted.
            </p>
            <pre aria-label="Terminal transcript" className="max-h-64 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs text-slate-100">
              {lines.map((l) => `$ ${l.command}\n${l.output}\n`).join("")}
            </pre>
            <form
              className="flex gap-2"
              onSubmit={async (event) => {
                event.preventDefault();
                if (input.trim() === "") {
                  return;
                }
                const line = await api.terminal(slug, input);
                setLines((current) => [...current, line]);
                setInput("");
              }}
            >
              <input aria-label="Terminal input" className={`${field} mt-0 font-mono`} value={input} onChange={(e) => setInput(e.target.value)} />
              <button type="submit" className={primary}>
                Run
              </button>
            </form>
          </div>
        )}

        {notice !== null && (
          <p role="status" data-testid="git-notice">
            {notice}
          </p>
        )}
      </div>
    </section>
  );
}
