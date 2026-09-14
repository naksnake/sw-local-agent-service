import { useEffect, useState } from "react";

import { type CycleStatus, type RunView, STATUS_WORD, type ValidationApi } from "./api";
import { NewValidationRunWizard } from "./NewValidationRunWizard";

// The Validation page (CLAUDE.md §9, §10.2): runs with their LED cycle map, console and
// findings, and one primary action — New validation run. Copy: docs/ui/validation.md.

interface Props {
  api: ValidationApi;
  /** Who is signed in; recorded on approvals. */
  user?: string;
  /** Open the New … wizard on arrival (Home's buttons). */
  startWizardOpen?: boolean;
}

const CELL_CLASS: Record<CycleStatus, string> = {
  waiting: "bg-slate-200 dark:bg-slate-700",
  running: "bg-sky-400 animate-pulse",
  ok: "bg-emerald-500",
  finding: "bg-amber-500",
  failed: "bg-red-600",
  skipped: "bg-slate-400",
};

export function ValidationPage({ api, user = "you", startWizardOpen = false }: Props) {
  const [runs, setRuns] = useState<RunView[] | null>(null);
  const [wizardOpen, setWizardOpen] = useState(startWizardOpen);

  useEffect(() => {
    void api.listRuns().then(setRuns);
  }, [api]);

  function replace(run: RunView) {
    setRuns((current) => (current ?? []).map((r) => (r.ticketId === run.ticketId ? run : r)));
  }

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Validation</h1>
          <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
            The Validation Agent drives one lab server at a time through a deterministic state
            machine. Models compile the plan and analyse the results; code performs every power
            action, and you approve anything destructive.
          </p>
        </div>
        {!wizardOpen && (
          <button
            type="button"
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900"
            onClick={() => setWizardOpen(true)}
          >
            New validation run
          </button>
        )}
      </header>

      {wizardOpen && (
        <NewValidationRunWizard
          api={api}
          onCancel={() => setWizardOpen(false)}
          onStarted={(run) => {
            setRuns((current) => [run, ...(current ?? [])]);
            setWizardOpen(false);
          }}
        />
      )}

      <section aria-labelledby="runs-heading">
        <h2 id="runs-heading" className="text-lg font-medium">
          Runs
        </h2>
        {runs === null ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">Loading runs…</p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">
            No validation run yet. Start one with a suite; every cycle shows up here as it happens.
          </p>
        ) : (
          <ul className="mt-3 space-y-6">
            {runs.map((run) => (
              <li
                key={run.ticketId}
                className="rounded-lg border border-slate-200 p-4 dark:border-slate-800"
                aria-label={run.ticketId}
              >
                <h3 className="font-medium">
                  {run.title} <span className="text-slate-500">· {run.ticketId} · {run.target}</span>
                </h3>
                <p className="text-sm text-slate-700 dark:text-slate-300">{run.sentence}</p>

                {run.approvalsPending.length > 0 && (
                  <div className="mt-3 rounded-md bg-amber-50 p-3 text-sm dark:bg-amber-950">
                    <p>
                      Nothing has touched {run.target} yet. {run.approvalsPending.length}{" "}
                      {run.approvalsPending.length === 1 ? "step needs" : "steps need"} your approval:{" "}
                      {run.approvalsPending.join(", ")}.
                    </p>
                    <button
                      type="button"
                      className="mt-2 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900"
                      onClick={() => void api.approve(run.ticketId, user).then(replace)}
                    >
                      Approve {run.approvalsPending.length === 1 ? "this step" : "these steps"} and continue
                    </button>
                  </div>
                )}

                <div className="mt-3">
                  <h4 className="text-sm font-medium">Cycles</h4>
                  <ol className="mt-1 flex flex-wrap gap-1" aria-label={`${run.ticketId} cycle map`}>
                    {run.cycles.map((cell) => (
                      <li
                        key={cell.n}
                        className={`h-5 w-5 rounded-sm ${CELL_CLASS[cell.status]}`}
                        aria-label={`Cycle ${cell.n}: ${STATUS_WORD[cell.status]}`}
                        title={cell.sentence || `Cycle ${cell.n}: ${STATUS_WORD[cell.status]}`}
                      />
                    ))}
                  </ol>
                  <p className="mt-1 text-xs text-slate-500">
                    green ok · amber finding · red did not boot · grey waiting
                  </p>
                </div>

                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div>
                    <h4 className="text-sm font-medium">Findings</h4>
                    {run.findings.length === 0 ? (
                      <p className="text-sm text-slate-600 dark:text-slate-400">
                        {run.cycles.some((c) => c.status !== "waiting")
                          ? "No change against the baseline so far."
                          : "Findings appear here after the first cycle."}
                      </p>
                    ) : (
                      <ul className="mt-1 space-y-2" aria-label={`${run.ticketId} findings`}>
                        {run.findings.map((finding) => (
                          <li key={finding.ticketId} className="text-sm">
                            {finding.sentence}. Owner: {finding.owner}.{" "}
                            <button
                              type="button"
                              className="rounded-md border border-slate-300 px-2 py-0.5 text-sm dark:border-slate-700"
                              aria-label={`Review ticket ${finding.ticketId}`}
                            >
                              Review ticket
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div>
                    <h4 className="text-sm font-medium">Console</h4>
                    <pre
                      aria-label={`${run.ticketId} console`}
                      className="mt-1 max-h-48 overflow-auto rounded-md bg-slate-950 p-2 text-xs text-slate-100"
                    >
                      {run.console.length === 0
                        ? "The serial console streams here once the run starts."
                        : run.console.slice(-40).join("\n")}
                    </pre>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
