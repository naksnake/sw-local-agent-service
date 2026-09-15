import { useEffect, useState } from "react";

import { type GitApi, GitApiError, type GitHostView, type HostKind } from "./api";

// Admin → Git hosts (CLAUDE.md §5.7 egress): the only hosts git-broker may reach.
// Copy: docs/ui/git-hosts.md.

interface Props {
  api: GitApi;
}

const field = "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
const primary = "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900";

export function GitHostsAdmin({ api }: Props) {
  const [hosts, setHosts] = useState<GitHostView[] | null>(null);
  const [name, setName] = useState("");
  const [hostname, setHostname] = useState("");
  const [kind, setKind] = useState<HostKind>("gitlab");
  const [sshHostKey, setSshHostKey] = useState("");
  const [problem, setProblem] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    void api.listHosts().then(setHosts);
  }, [api]);

  async function add() {
    setProblem(null);
    try {
      const host = await api.addHost({ name: name.trim(), hostname: hostname.trim().toLowerCase(), kind, sshHostKey });
      setHosts((current) => [...(current ?? []), host]);
      setNotice(`${host.hostname} is now an allowed Git host over ${host.protocols.join(" and ")}. It applies now; nothing restarted.`);
      setName("");
      setHostname("");
      setSshHostKey("");
    } catch (error: unknown) {
      setProblem(
        error instanceof GitApiError
          ? [error.parts.whatHappened, error.parts.likelyCause, error.parts.whatToDo]
          : ["The host wasn't added.", "The api service didn't answer.", "Try again; if it repeats, run `slas logs api` on the host."],
      );
    }
  }

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Git hosts</h1>
        <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
          git-broker reaches only these hosts. A remote on any other host is refused with the list
          shown here. Changes apply as soon as you save; nothing is restarted.
        </p>
      </header>
      <section aria-labelledby="hosts-heading">
        <h2 id="hosts-heading" className="text-lg font-medium">
          Allowed hosts
        </h2>
        {hosts === null ? (
          <p className="text-sm">Loading hosts…</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {hosts.map((host) => (
              <li key={host.name} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800" aria-label={host.hostname}>
                <span className="font-medium">{host.hostname}</span> · {host.kind} · {host.protocols.join(" and ")}
                {host.protocols.includes("ssh") && !host.sshHostKeyPinned ? " · ssh waits for a pinned host key" : ""}
                {host.note ? <span className="text-slate-500"> · {host.note}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="add-host-heading" className="space-y-3">
        <h2 id="add-host-heading" className="text-lg font-medium">
          Allow a host
        </h2>
        <label className="block text-sm">
          Name
          <input aria-label="Host name" className={field} placeholder="gitlab-lab3" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          Hostname
          <input aria-label="Hostname" className={field} placeholder="gitlab.lab3.internal" value={hostname} onChange={(e) => setHostname(e.target.value)} />
        </label>
        <label className="block text-sm">
          Kind
          <select aria-label="Host kind" className={field} value={kind} onChange={(e) => setKind(e.target.value as HostKind)}>
            <option value="gitlab">GitLab (opens merge requests)</option>
            <option value="gitea">Gitea (opens pull requests)</option>
            <option value="github">GitHub (opens pull requests; needs an ADR)</option>
            <option value="generic">Plain Git (push only)</option>
          </select>
        </label>
        <label className="block text-sm">
          SSH host key (one known_hosts line; leave empty to allow https only)
          <input aria-label="SSH host key" className={`${field} font-mono`} value={sshHostKey} onChange={(e) => setSshHostKey(e.target.value)} />
        </label>
        <button type="button" className={primary} disabled={name.trim() === "" || hostname.trim() === ""} onClick={() => void add()}>
          Allow host
        </button>
        {notice !== null && (
          <p role="status" className="text-sm" data-testid="host-notice">
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
