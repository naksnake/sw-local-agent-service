import { useEffect, useState } from "react";

import type { CodingApi, CodingTask } from "../coding/api";
import type { FactoryApi, FactoryJob } from "../factory/api";
import type { RunView, ValidationApi } from "../validation/api";
import type { HomeListsApi } from "./api";

// Home (docs/ui/home.md, from docs/ui-demo/slas-ui-demo.html): what is running now, and
// what needs you. Everything here is derived from the three agents' own lists; Home never
// holds state of its own. The lists come from `lists` (the api's list-only calls) when it is
// given, otherwise from the agent APIs; the buttons need the agent APIs either way.

export type AgentPage = "coding" | "validation" | "factory";

export interface Snapshot {
  tasks: CodingTask[];
  runs: RunView[];
  jobs: FactoryJob[];
}

export interface Attention {
  key: string;
  page: AgentPage;
  title: string;
  whatHappened: string;
  likelyCause: string;
  whatToDo: string;
  button: string;
}

export interface RunningItem {
  key: string;
  page: AgentPage;
  title: string;
  sentence: string;
  done: number;
  total: number;
  pill: "Running" | "Waiting for approval" | "Starting";
}

export interface ResultItem {
  key: string;
  page: AgentPage;
  run: string;
  result: string;
  agent: "Coding" | "Validation" | "Factory";
}

const RUNNING_STATES = new Set(["Running", "Planned", "Approved", "Open", "Analysing"]);

export async function snapshot(apis: {
  lists?: HomeListsApi;
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  factoryApi?: FactoryApi;
}): Promise<Snapshot> {
  const [tasks, runs, jobs] = await Promise.all([
    apis.lists?.listTasks() ?? apis.codingApi?.listTasks() ?? Promise.resolve([]),
    apis.lists?.listRuns() ?? apis.validationApi?.listRuns() ?? Promise.resolve([]),
    apis.lists?.listJobs() ?? apis.factoryApi?.listJobs() ?? Promise.resolve([]),
  ]);
  return { tasks, runs, jobs };
}

export function needsYou(s: Snapshot): Attention[] {
  const items: Attention[] = [];
  for (const run of s.runs) {
    if (run.approvalsPending.length > 0) {
      const n = run.approvalsPending.length;
      items.push({
        key: run.ticketId,
        page: "validation",
        title: `Validation run ${run.ticketId} is waiting for your approval.`,
        whatHappened: `${n} destructive ${n === 1 ? "step needs" : "steps need"} a per-run approval before anything touches ${run.target}.`,
        likelyCause: "The suite includes power cycles or another destructive step.",
        whatToDo: "Open the run, read the plan, and approve it or take those steps out.",
        button: "Open run",
      });
    }
  }
  for (const task of s.tasks) {
    if (/stopped|needs review|failed/i.test(task.state)) {
      items.push({
        key: task.ticketId,
        page: "coding",
        title: `Coding task ${task.title} stopped.`,
        whatHappened: task.sentence,
        likelyCause:
          "The plan asks for something the repository does not contain, or a test cannot pass as written.",
        whatToDo: "Open the task, read the last feed lines, then edit the plan or attach what is missing.",
        button: "Open task",
      });
    }
  }
  for (const job of s.jobs) {
    if (job.held) {
      items.push({
        key: job.ticketId,
        page: "factory",
        title: `Unit ${job.unitSn} on ${job.station} did not pass.`,
        whatHappened: `${job.ticketId} holds the station; the line lead decides.`,
        likelyCause: "The unit failed the test loop, or the voters did not agree.",
        whatToDo: "Open the job and record PASS or FAIL with a note.",
        button: "Open job",
      });
    }
  }
  return items;
}

function progress(steps: { status: string }[]): { done: number; total: number } {
  return {
    done: steps.filter((step) => step.status === "done" || step.status === "ok").length,
    total: steps.length,
  };
}

export function runningNow(s: Snapshot): RunningItem[] {
  const items: RunningItem[] = [];
  for (const task of s.tasks) {
    if (RUNNING_STATES.has(task.state)) {
      items.push({
        key: task.ticketId,
        page: "coding",
        title: `Coding task ${task.ticketId} — ${task.title}`,
        sentence: task.sentence,
        ...progress(task.steps),
        pill: "Running",
      });
    }
  }
  for (const run of s.runs) {
    if (RUNNING_STATES.has(run.state)) {
      const waiting = run.approvalsPending.length > 0;
      items.push({
        key: run.ticketId,
        page: "validation",
        title: `Validation run ${run.ticketId} — ${run.title}, ${run.target}`,
        sentence: run.sentence,
        ...progress(run.cycles),
        pill: waiting ? "Waiting for approval" : run.state === "Running" ? "Running" : "Starting",
      });
    }
  }
  for (const job of s.jobs) {
    if (RUNNING_STATES.has(job.state) && !job.held) {
      items.push({
        key: job.ticketId,
        page: "factory",
        title: `Factory ticket ${job.ticketId} — Station ${job.station}, unit ${job.unitSn}`,
        sentence: job.sentence,
        ...progress(job.steps),
        pill: "Running",
      });
    }
  }
  return items;
}

export function recentResults(s: Snapshot): ResultItem[] {
  const items: ResultItem[] = [];
  for (const task of s.tasks) {
    if (!RUNNING_STATES.has(task.state)) {
      items.push({
        key: task.ticketId,
        page: "coding",
        run: `${task.ticketId} — ${task.title}`,
        result: task.sentence,
        agent: "Coding",
      });
    }
  }
  for (const run of s.runs) {
    if (!RUNNING_STATES.has(run.state)) {
      items.push({
        key: run.ticketId,
        page: "validation",
        run: `${run.ticketId} — ${run.title}, ${run.target}`,
        result: run.sentence,
        agent: "Validation",
      });
    }
  }
  for (const job of s.jobs) {
    if (!RUNNING_STATES.has(job.state) || job.held) {
      items.push({
        key: job.ticketId,
        page: "factory",
        run: `${job.ticketId} — ${job.title}`,
        result: job.verdictSentence || job.sentence,
        agent: "Factory",
      });
    }
  }
  return items;
}

/** The top bar's health sentence, from the same snapshot (docs/ui/home.md). */
export function healthSentence(s: Snapshot | null): string {
  if (s === null) {
    return "Looking at what is running…";
  }
  const running = runningNow(s).length;
  const attention = needsYou(s).length;
  const jobs = running === 0 ? "Nothing is running." : `${running} ${running === 1 ? "job" : "jobs"} running.`;
  if (attention > 0) {
    return `${attention} ${attention === 1 ? "item needs" : "items need"} you. ${jobs}`;
  }
  return `Everything is healthy. ${jobs}`;
}

interface Props {
  /** The api's list-only calls; when given, the lists come from here. */
  lists?: HomeListsApi;
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  factoryApi?: FactoryApi;
  /** "Signed in as Pat Lin. You can …" (docs/ui/sign-in.md, Home); absent when nobody is signed in. */
  welcome?: string;
  /** Go to an agent's page; `wizard` opens its New … wizard on arrival. */
  onOpen: (page: AgentPage, wizard: boolean) => void;
  /** Called with every fresh snapshot so the shell can phrase the health line. */
  onSnapshot?: (s: Snapshot) => void;
}

export function HomePage({ lists, codingApi, validationApi, factoryApi, welcome, onOpen, onSnapshot }: Props) {
  const [s, setS] = useState<Snapshot | null>(null);
  useEffect(() => {
    let live = true;
    void snapshot({
      ...(lists !== undefined ? { lists } : {}),
      ...(codingApi !== undefined ? { codingApi } : {}),
      ...(validationApi !== undefined ? { validationApi } : {}),
      ...(factoryApi !== undefined ? { factoryApi } : {}),
    })
      .then((snap) => {
        if (live) {
          setS(snap);
          onSnapshot?.(snap);
        }
      })
      .catch(() => {
        // A list that did not answer leaves Home empty rather than blank; the health line
        // keeps "Looking at what is running…" and the next visit tries again.
      });
    return () => {
      live = false;
    };
  }, [lists, codingApi, validationApi, factoryApi, onSnapshot]);

  const attention = s === null ? [] : needsYou(s);
  const running = s === null ? [] : runningNow(s);
  const results = s === null ? [] : recentResults(s);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Home</h1>
          <p className="lede">What is running now, and what needs you.</p>
        </div>
        <div className="row wrap">
          {codingApi !== undefined && (
            <button type="button" className="btn" onClick={() => onOpen("coding", true)}>
              New coding task
            </button>
          )}
          {factoryApi !== undefined && (
            <button type="button" className="btn" onClick={() => onOpen("factory", true)}>
              New factory job
            </button>
          )}
          {validationApi !== undefined && (
            <button type="button" className="btn primary" onClick={() => onOpen("validation", true)}>
              New validation run
            </button>
          )}
        </div>
      </div>

      <div className="stack">
        {welcome !== undefined && (
          <p className="sentence" data-testid="welcome">
            {welcome}
          </p>
        )}
        {s === null && <p className="muted">Looking at what is running…</p>}

        {attention.map((item) => (
          <section key={item.key} className="notice" aria-label={item.title}>
            <strong>{item.title}</strong>
            <dl>
              <dt>What happened</dt>
              <dd>{item.whatHappened}</dd>
              <dt>Likely cause</dt>
              <dd>{item.likelyCause}</dd>
              <dt>What to do</dt>
              <dd>{item.whatToDo}</dd>
            </dl>
            <div className="row" style={{ marginTop: 10 }}>
              <button type="button" className="btn small" onClick={() => onOpen(item.page, false)}>
                {item.button}
              </button>
            </div>
          </section>
        ))}

        <section className="panel tight" aria-labelledby="running-now">
          <h2 id="running-now">Running now</h2>
          {s !== null && running.length === 0 && (
            <p className="muted">Nothing is running. Start a task, run or job with the buttons above.</p>
          )}
          {running.length > 0 && (
            <ul className="plain jobs">
              {running.map((item) => (
                <li key={item.key}>
                  <button type="button" onClick={() => onOpen(item.page, false)} aria-label={`Open ${item.key}`}>
                    <div>
                      <div className="title">{item.title}</div>
                      <div className="sub">{item.sentence}</div>
                    </div>
                    <div className="row">
                      <div
                        className="bar"
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={item.total}
                        aria-valuenow={item.done}
                        aria-label={`${item.done} of ${item.total} steps done`}
                      >
                        <i style={{ width: `${item.total === 0 ? 0 : Math.round((item.done / item.total) * 100)}%` }} />
                      </div>
                      <span className={"pill " + (item.pill === "Running" ? "warn" : "info")}>{item.pill}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel tight" aria-labelledby="recent-results">
          <h2 id="recent-results">Recent results</h2>
          {s !== null && results.length === 0 && (
            <p className="muted">No results yet. Finished tasks, runs and jobs appear here with their outcome.</p>
          )}
          {results.length > 0 && (
            <table className="list">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Result</th>
                  <th>Agent</th>
                </tr>
              </thead>
              <tbody>
                {results.map((item) => (
                  <tr key={item.key} className="click" onClick={() => onOpen(item.page, false)}>
                    <td>{item.run}</td>
                    <td>{item.result}</td>
                    <td className="muted">{item.agent}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}
