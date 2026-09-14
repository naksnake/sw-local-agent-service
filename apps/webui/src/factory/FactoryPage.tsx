import { useEffect, useState } from "react";

import { type CellStatus, type FactoryApi, type FactoryJob, STATUS_WORD } from "./api";
import { NewFactoryJobWizard } from "./NewFactoryJobWizard";

// The Factory page (CLAUDE.md §9, §10.3): jobs with their test-step map and the station's
// screenshot strip, the verdict as a sentence, and the line lead's decision when a unit is
// held. One primary action — New factory job. Copy: docs/ui/factory.md.

interface Props {
  api: FactoryApi;
  /** Who is signed in; recorded on a line-lead decision. */
  user?: string;
}

const CELL_CLASS: Record<CellStatus, string> = {
  waiting: "bg-slate-200 dark:bg-slate-700",
  running: "bg-sky-400 animate-pulse",
  ok: "bg-emerald-500",
  failed: "bg-red-600",
  skipped: "bg-slate-400",
};

export function FactoryPage({ api, user = "you" }: Props) {
  const [jobs, setJobs] = useState<FactoryJob[] | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [notes, setNotes] = useState<Record<string, string>>({});

  useEffect(() => {
    void api.listJobs().then(setJobs);
  }, [api]);

  function replace(job: FactoryJob) {
    setJobs((current) => (current ?? []).map((j) => (j.ticketId === job.ticketId ? job : j)));
  }

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Factory</h1>
          <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
            The Factory Agent takes one unit at a time through the test loop on its station. Code
            performs every step and screenshots every GUI action; three voters review the result,
            and a unit passes only when all three agree.
          </p>
        </div>
        {!wizardOpen && (
          <button
            type="button"
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900"
            onClick={() => setWizardOpen(true)}
          >
            New factory job
          </button>
        )}
      </header>

      {wizardOpen && (
        <NewFactoryJobWizard
          api={api}
          onCancel={() => setWizardOpen(false)}
          onStarted={(job) => {
            setJobs((current) => [job, ...(current ?? [])]);
            setWizardOpen(false);
          }}
        />
      )}

      <section aria-labelledby="jobs-heading">
        <h2 id="jobs-heading" className="text-lg font-medium">
          Jobs
        </h2>
        {jobs === null ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">Loading jobs…</p>
        ) : jobs.length === 0 ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">
            No factory job yet. Start one from a production ticket or a label; every step shows up here
            with its screenshots.
          </p>
        ) : (
          <ul className="mt-3 space-y-6">
            {jobs.map((job) => (
              <li key={job.ticketId} className="rounded-lg border border-slate-200 p-4 dark:border-slate-800" aria-label={job.ticketId}>
                <h3 className="font-medium">
                  {job.title} <span className="text-slate-500">· {job.ticketId} · {job.mesTicketNo}</span>
                </h3>
                <p className="text-sm text-slate-700 dark:text-slate-300">{job.sentence}</p>

                <div className="mt-3">
                  <h4 className="text-sm font-medium">Test steps</h4>
                  <ol className="mt-1 flex flex-wrap gap-1" aria-label={`${job.ticketId} step map`}>
                    {job.steps.map((cell) => (
                      <li
                        key={cell.n}
                        className={`h-5 min-w-5 rounded-sm px-1 text-xs text-white ${CELL_CLASS[cell.status]}`}
                        aria-label={`Step ${cell.n}: ${STATUS_WORD[cell.status]}`}
                        title={cell.sentence || `${cell.title} — ${STATUS_WORD[cell.status]}`}
                      >
                        {cell.n}
                      </li>
                    ))}
                  </ol>
                  <ol className="mt-2 space-y-1">
                    {job.steps.map((cell) => (
                      <li key={cell.n} className="text-sm">
                        <span className="text-slate-500">{cell.n}.</span> {cell.title}{" "}
                        <span className="text-slate-500">— {STATUS_WORD[cell.status]}</span>
                      </li>
                    ))}
                  </ol>
                </div>

                <div className="mt-3">
                  <h4 className="text-sm font-medium">Station screenshots</h4>
                  {job.steps.every((s) => s.screenshots.length === 0) ? (
                    <p className="text-sm text-slate-600 dark:text-slate-400">
                      Screenshots appear here as soon as the first GUI step runs.
                    </p>
                  ) : (
                    <ul className="mt-1 flex gap-1 overflow-x-auto" aria-label={`${job.ticketId} screenshots`}>
                      {job.steps.flatMap((cell) =>
                        cell.screenshots.map((src, index) => (
                          <li key={`${cell.n}-${index}`} className="shrink-0">
                            <img
                              src={src}
                              alt={`Step ${cell.n}, screenshot ${index + 1}`}
                              className="h-16 w-24 rounded border border-slate-300 bg-slate-100 object-cover dark:border-slate-700"
                            />
                          </li>
                        )),
                      )}
                    </ul>
                  )}
                </div>

                <div className="mt-3">
                  <h4 className="text-sm font-medium">Verdict</h4>
                  <p className="text-sm" data-testid={`${job.ticketId}-verdict`}>
                    {job.verdictSentence || "Not decided yet."}
                  </p>
                  {job.draftTicketId !== null && (
                    <p className="text-sm">
                      A ticket is drafted for the line lead.{" "}
                      <button type="button" className="rounded-md border border-slate-300 px-2 py-0.5 text-sm dark:border-slate-700" aria-label={`Review ticket ${job.draftTicketId}`}>
                        Review ticket
                      </button>
                    </p>
                  )}
                  {job.backupPath !== null && (
                    <p className="text-sm text-slate-600 dark:text-slate-400">Station backup: {job.backupPath}</p>
                  )}
                </div>

                {job.held && (
                  <div className="mt-3 rounded-md bg-amber-50 p-3 text-sm dark:bg-amber-950">
                    <p>
                      {job.station} is held and the unit stays on until you decide. Your decision is recorded
                      with your name; the voters' view is input, not the verdict.
                    </p>
                    <input
                      aria-label={`Decision note ${job.ticketId}`}
                      className="mt-2 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                      placeholder="What you checked, in a sentence"
                      value={notes[job.ticketId] ?? ""}
                      onChange={(event) => setNotes({ ...notes, [job.ticketId]: event.target.value })}
                    />
                    <div className="mt-2 flex gap-2">
                      <button type="button" className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900" aria-label={`Decide PASS ${job.ticketId}`} onClick={() => void api.decide(job.ticketId, "PASS", user, notes[job.ticketId] ?? "").then(replace)}>
                        Decide PASS
                      </button>
                      <button type="button" className="rounded-md border border-red-700 px-3 py-1.5 text-sm text-red-700 dark:border-red-300 dark:text-red-300" aria-label={`Decide FAIL ${job.ticketId}`} onClick={() => void api.decide(job.ticketId, "FAIL", user, notes[job.ticketId] ?? "").then(replace)}>
                        Decide FAIL
                      </button>
                    </div>
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
