import { useEffect, useState } from "react";

import type { CodingApi, CodingTask, StepStatus } from "./api";
import { NewCodingTaskWizard } from "./NewCodingTaskWizard";

// The Coding page (CLAUDE.md §9): tasks with their plan checklist and activity feed, and
// one primary action — New coding task. Copy: docs/ui/coding.md.

interface Props {
  api: CodingApi;
}

const STATUS_WORD: Record<StepStatus, string> = {
  pending: "waiting",
  running: "running",
  done: "done",
  failed: "needs you",
  skipped: "skipped",
};

export function CodingPage({ api }: Props) {
  const [tasks, setTasks] = useState<CodingTask[] | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    void api.listTasks().then(setTasks);
  }, [api]);

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
        <h2 id="tasks-heading" className="text-lg font-medium">
          Tasks
        </h2>
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
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
