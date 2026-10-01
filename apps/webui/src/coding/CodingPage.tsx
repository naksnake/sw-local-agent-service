import { useCallback, useEffect, useState } from "react";

import { isLive, POLL_INTERVAL_MS, usePolling } from "../api/polling";
import type { GitApi } from "../git/api";
import { GitPanel } from "../git/GitPanel";
import { type CodingApi, type CodingTask, isFinished, type StepStatus } from "./api";
import { NewCodingTaskWizard } from "./NewCodingTaskWizard";

// The Coding page (CLAUDE.md §9): tasks with their plan checklist and activity feed, and
// one primary action — New coding task. Each task can open its project's Git panel
// (Status · Commit · History · Push/Pull · Bundle · Terminal). A finished task can be
// removed (its ticket, SOP and artifacts; the project's repository stays), one at a time or
// all at once with "Clear finished tasks". The list is re-read every few seconds while a
// task is still running (live progress). Copy: docs/ui/coding.md.

interface Props {
  api: CodingApi;
  gitApi?: GitApi;
  /** Open the New … wizard on arrival (Home's buttons). */
  startWizardOpen?: boolean;
}

function slugOf(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48);
}

const STATUS_WORD: Record<StepStatus, string> = {
  pending: "waiting",
  running: "running",
  done: "done",
  failed: "needs you",
  skipped: "skipped",
};

export function CodingPage({ api, gitApi, startWizardOpen = false }: Props) {
  const [tasks, setTasks] = useState<CodingTask[] | null>(null);
  const [wizardOpen, setWizardOpen] = useState(startWizardOpen);
  const [gitOpenFor, setGitOpenFor] = useState<string | null>(null);
  /** The last removal's sentence, or what went wrong; shown above the list. */
  const [notice, setNotice] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);

  const refresh = useCallback(async () => setTasks(await api.listTasks()), [api]);

  useEffect(() => {
    void refresh().catch(() => {
      // The page stays on "Loading tasks…"; the next poll or visit tries again.
    });
  }, [refresh]);
  usePolling(refresh, tasks?.some((task) => isLive(task.state)) === true ? POLL_INTERVAL_MS : null);

  const finished = (tasks ?? []).filter(isFinished);

  const remove = async (targets: CodingTask[]) => {
    setRemoving(true);
    const sentences: string[] = [];
    try {
      for (const task of targets) {
        try {
          sentences.push(await api.remove(task.ticketId));
          setTasks((current) => (current ?? []).filter((t) => t.ticketId !== task.ticketId));
        } catch (error: unknown) {
          sentences.push(
            `${task.ticketId} was not removed: ${error instanceof Error ? error.message : "the service did not answer."}`,
          );
        }
      }
      setNotice(
        targets.length > 1
          ? `${targets.length - sentences.filter((s) => s.includes("was not removed")).length} of ${targets.length} finished tasks were removed. ${sentences.filter((s) => s.includes("was not removed")).join(" ")}`.trim()
          : (sentences[0] ?? null),
      );
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Coding</h1>
          <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
            The Coding Agent works in an isolated sandbox on this host, commits on its own branch,
            and never holds a Git credential.
          </p>
        </div>
        {!wizardOpen && (
          <button
            type="button"
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900"
            onClick={() => setWizardOpen(true)}
          >
            New coding task
          </button>
        )}
      </header>

      {wizardOpen && (
        <NewCodingTaskWizard
          api={api}
          onCancel={() => setWizardOpen(false)}
          onStarted={(task) => {
            setTasks((current) => [task, ...(current ?? [])]);
            setWizardOpen(false);
          }}
        />
      )}

      <section aria-labelledby="tasks-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="tasks-heading" className="text-lg font-medium">
            Tasks
          </h2>
          {finished.length > 0 && (
            <button
              type="button"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
              disabled={removing}
              title="Removes every task that is done, failed or waiting for review; the projects' repositories stay."
              onClick={() => void remove(finished)}
            >
              Clear finished tasks
            </button>
          )}
        </div>
        {notice !== null && (
          <p className="mt-2 text-sm text-slate-700 dark:text-slate-300" role="status">
            {notice}
          </p>
        )}
        {tasks === null ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">Loading tasks…</p>
        ) : tasks.length === 0 ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">
            No coding task yet. Start one with a plan; the agent shows every step here as it works.
          </p>
        ) : (
          <ul className="mt-3 space-y-6">
            {tasks.map((task) => (
              <li
                key={task.ticketId}
                className="rounded-lg border border-slate-200 p-4 dark:border-slate-800"
                aria-label={task.ticketId}
              >
                <h3 className="font-medium">
                  {task.title} <span className="text-slate-500">· {task.ticketId}</span>
                </h3>
                <p className="text-sm text-slate-700 dark:text-slate-300">{task.sentence}</p>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div>
                    <h4 className="text-sm font-medium">Plan</h4>
                    <ol className="mt-1 space-y-1">
                      {task.steps.map((step) => (
                        <li key={step.n} className="text-sm">
                          <span className="text-slate-500">{step.n}.</span> {step.title}{" "}
                          <span className="text-slate-500">— {STATUS_WORD[step.status]}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                  <div>
                    <h4 className="text-sm font-medium">Activity</h4>
                    <ul className="mt-1 space-y-1" aria-label={`${task.ticketId} activity`}>
                      {task.feed.map((line, index) => (
                        <li key={index} className="text-sm text-slate-700 dark:text-slate-300">
                          {line}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
                <p className="mt-3 text-sm text-slate-500">
                  Open the Terminal tab to inspect the branch; push happens from the Git panel,
                  which uses your saved remote.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {gitApi !== undefined && (
                    <button
                      type="button"
                      className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
                      onClick={() => setGitOpenFor((current) => (current === task.ticketId ? null : task.ticketId))}
                    >
                      {gitOpenFor === task.ticketId ? "Hide Git panel" : "Git panel"}
                    </button>
                  )}
                  {isFinished(task) && (
                    <button
                      type="button"
                      className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
                      disabled={removing}
                      aria-label={`Remove ${task.ticketId}`}
                      title="Removes this task's ticket, SOP and artifacts; the project's repository stays."
                      onClick={() => void remove([task])}
                    >
                      Remove
                    </button>
                  )}
                </div>
                {gitApi !== undefined && gitOpenFor === task.ticketId && (
                  <div className="mt-3">
                    <GitPanel api={gitApi} slug={slugOf(task.title)} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
