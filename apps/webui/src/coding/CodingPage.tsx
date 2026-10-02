import { useCallback, useEffect, useState } from "react";

import { isLive, POLL_INTERVAL_MS, usePolling } from "../api/polling";
import type { GitApi } from "../git/api";
import { GitPanel } from "../git/GitPanel";
import { type CodingApi, type CodingTask, followUpPlan, isFinished, languagesOf, type StepStatus, zipUrl } from "./api";
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

const STATUS_MARK: Record<StepStatus, string> = {
  pending: "○",
  running: "◐",
  done: "✓",
  failed: "!",
  skipped: "–",
};

/** The pill for a ticket state: a word a person reads, coloured by what it asks of them. */
function stateTone(state: string): string {
  if (state === "Done") return "pill pass";
  if (state === "Failed") return "pill fail";
  if (state === "Needs review") return "pill warn";
  return "pill info";
}

function stateWord(state: string): string {
  if (state === "Needs review") return "Needs your review";
  if (state === "Open" || state === "Planned" || state === "Approved") return "Starting";
  return state;
}

function progressOf(task: CodingTask): number {
  const finishedSteps = task.steps.filter((step) => step.status === "done" || step.status === "skipped").length;
  return task.steps.length === 0 ? 0 : Math.round((finishedSteps / task.steps.length) * 100);
}

/** Feed lines that report a failure read in the failure colour; the rest stay calm. */
function feedTone(line: string): string {
  return /\b(failed|stopped|not finished|needs you|A person needs)\b/.test(line) ? "bad" : "";
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
  /** Follow-up prompts being typed, per task; the one being sent. */
  const [prompts, setPrompts] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null);

  const refresh = useCallback(async () => setTasks(await api.listTasks()), [api]);

  useEffect(() => {
    void refresh().catch(() => {
      // The page stays on "Loading tasks…"; the next poll or visit tries again.
    });
  }, [refresh]);
  usePolling(refresh, tasks?.some((task) => isLive(task.state)) === true ? POLL_INTERVAL_MS : null);

  const finished = (tasks ?? []).filter(isFinished);

  // Claude Code style: a prompt is one more task on the same project, which the agent
  // changes with its current files in view, then checks, commits and exports as usual.
  const followUp = async (task: CodingTask) => {
    const prompt = (prompts[task.ticketId] ?? "").trim();
    if (prompt === "" || sending !== null) {
      return;
    }
    setSending(task.ticketId);
    try {
      const plan = followUpPlan(task.title, prompt);
      const proposed = await api.propose(plan, "prompt.md");
      const languages = languagesOf(task);
      const started = await api.start(
        { ...proposed, title: task.title, languages: languages.length > 0 ? languages : proposed.languages },
        plan,
        "prompt.md",
      );
      setPrompts((current) => ({ ...current, [task.ticketId]: "" }));
      setNotice(`${started.ticketId} started on ${task.title}: ${prompt.split("\n")[0] ?? prompt}`);
      await refresh();
    } catch (error: unknown) {
      setNotice(
        `The change was not started: ${error instanceof Error ? error.message : "the service did not answer."}`,
      );
    } finally {
      setSending(null);
    }
  };

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
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Coding</h1>
          <p className="lede">
            The Coding Agent works in an isolated sandbox on this host, commits on its own branch,
            and never holds a Git credential.
          </p>
        </div>
        {!wizardOpen && (
          <button type="button" className="btn primary" onClick={() => setWizardOpen(true)}>
            New coding task
          </button>
        )}
      </div>

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

      <section aria-labelledby="tasks-heading" className="stack">
        <div className="row between wrap">
          <h2 id="tasks-heading">Tasks</h2>
          {finished.length > 0 && (
            <button
              type="button"
              className="btn small"
              disabled={removing}
              title="Removes every task that is done, failed or waiting for review; the projects' repositories stay."
              onClick={() => void remove(finished)}
            >
              Clear finished tasks
            </button>
          )}
        </div>
        {notice !== null && (
          <p className="sentence" role="status">
            {notice}
          </p>
        )}
        {tasks === null ? (
          <p className="muted">Loading tasks…</p>
        ) : tasks.length === 0 ? (
          <section className="panel empty">
            <p className="sentence">No coding task yet. Start one with a plan; the agent shows every step here as it works.</p>
          </section>
        ) : (
          <ul className="plain stack">
            {tasks.map((task) => (
              <li key={task.ticketId} className="panel task" aria-label={task.ticketId}>
                <div className="task-head">
                  <div>
                    <h3>
                      {task.title} <span className="faint mono">· {task.ticketId}</span>
                    </h3>
                    <p className="muted">{task.sentence}</p>
                  </div>
                  <div className="task-state">
                    <span className={stateTone(task.state)}>
                      {isLive(task.state) && <span className="spinner" aria-hidden="true" />}
                      {stateWord(task.state)}
                    </span>
                    {task.steps.length > 0 && (
                      <span className="row">
                        <span className="bar" aria-hidden="true">
                          <i style={{ width: `${progressOf(task)}%` }} />
                        </span>
                        <span className="faint">
                          {task.steps.filter((step) => step.status === "done").length} of {task.steps.length}
                        </span>
                      </span>
                    )}
                  </div>
                </div>
                <div className="cols cols-2 task-body">
                  <div>
                    <h4>Plan</h4>
                    <ol className="plain checklist">
                      {task.steps.map((step) => (
                        <li key={step.n} className={`check ${step.status}`}>
                          <span className="mark" aria-hidden="true">
                            {STATUS_MARK[step.status]}
                          </span>
                          <span>
                            <span className="faint">{step.n}.</span> {step.title}{" "}
                            <span className="faint">— {STATUS_WORD[step.status]}</span>
                          </span>
                        </li>
                      ))}
                    </ol>
                  </div>
                  <div>
                    <h4>Activity</h4>
                    <Feed ticketId={task.ticketId} lines={task.feed} />
                  </div>
                </div>
                <p className="faint task-hint">
                  Open the Terminal tab to inspect the branch; push happens from the Git panel,
                  which uses your saved remote.
                </p>
                {isFinished(task) && (
                  <form
                    className="prompt-box"
                    aria-label={`Ask for a change to ${task.title}`}
                    onSubmit={(event) => {
                      event.preventDefault();
                      void followUp(task);
                    }}
                  >
                    <label htmlFor={`prompt-${task.ticketId}`}>Ask the agent for a change to this project</label>
                    <textarea
                      id={`prompt-${task.ticketId}`}
                      className="input mono"
                      rows={3}
                      placeholder="For example: add a --csv option that writes the summary as CSV, with a test"
                      value={prompts[task.ticketId] ?? ""}
                      onChange={(e) => setPrompts((current) => ({ ...current, [task.ticketId]: e.target.value }))}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                          e.preventDefault();
                          void followUp(task);
                        }
                      }}
                    />
                    <div className="row wrap">
                      <button
                        type="submit"
                        className="btn primary"
                        disabled={sending !== null || (prompts[task.ticketId] ?? "").trim() === ""}
                      >
                        {sending === task.ticketId ? "Starting…" : "Send to the agent"}
                      </button>
                      <span className="faint">
                        Ctrl+Enter sends. The agent changes {task.title} with its current files in view, runs the checks and commits on a new branch.
                      </span>
                    </div>
                  </form>
                )}
                <div className="row wrap task-actions">
                  {gitApi !== undefined && (
                    <button
                      type="button"
                      className="btn small"
                      onClick={() => setGitOpenFor((current) => (current === task.ticketId ? null : task.ticketId))}
                    >
                      {gitOpenFor === task.ticketId ? "Hide Git panel" : "Git panel"}
                    </button>
                  )}
                  {zipUrl(task) !== null && (
                    <a
                      className="btn small"
                      href={zipUrl(task) ?? undefined}
                      download
                      title="Downloads the project as the agent left it, to build and test on your own machine."
                    >
                      Download ZIP
                    </a>
                  )}
                  {isFinished(task) && (
                    <button
                      type="button"
                      className="btn small ghost"
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
                  <div className="task-git">
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

/** How many feed lines show before "Show all"; the newest are the ones a person wants. */
const FEED_TAIL = 12;

function Feed({ ticketId, lines }: { ticketId: string; lines: string[] }) {
  const [all, setAll] = useState(false);
  const hidden = all ? 0 : Math.max(0, lines.length - FEED_TAIL);
  return (
    <div className="feed">
      {hidden > 0 && (
        <button type="button" className="btn small ghost" onClick={() => setAll(true)}>
          Show all {lines.length} lines
        </button>
      )}
      <ul className="plain" aria-label={`${ticketId} activity`}>
        {lines.slice(hidden).map((line, index) => (
          <li key={hidden + index} className={feedTone(line)}>
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}
