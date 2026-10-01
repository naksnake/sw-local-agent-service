import { useEffect, useMemo, useState } from "react";

import { asApiError } from "../api/http";
import {
  type Breakdown,
  type CodingApi,
  type CodingTask,
  type ExportTarget,
  type Isolation,
  type LanguageChoice,
  type LanguageId,
  LANGUAGES,
  type Readiness,
  type SkillSummary,
  type ToolchainResolution,
  labelOf,
  reviewSentence,
} from "./api";

/** How often the Review step asks again whether the coding model is ready. */
const READINESS_POLL_MS = 5_000;

// The three-step wizard from CLAUDE.md §9: Plan → Setup → Review, ending in a sentence
// that says what will happen and one verb button. Copy: docs/ui/new-coding-task.md.

interface Props {
  api: CodingApi;
  onStarted: (task: CodingTask) => void;
  onCancel: () => void;
}

type Step = 1 | 2 | 3;

const STEP_TITLES: Record<Step, string> = { 1: "Plan", 2: "Setup", 3: "Review" };

const field =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 " +
  "shadow-sm focus:border-slate-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900 " +
  "dark:text-slate-100";
const primary =
  "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 " +
  "dark:bg-slate-100 dark:text-slate-900";
const secondary =
  "rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-700 dark:border-slate-700 " +
  "dark:text-slate-300";

export function NewCodingTaskWizard({ api, onStarted, onCancel }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [plan, setPlan] = useState("");
  const [filename, setFilename] = useState("plan.md");
  const [detected, setDetected] = useState<LanguageId[]>([]);
  const [breakdown, setBreakdown] = useState<Breakdown | null>(null);
  const [remotes, setRemotes] = useState<string[]>([]);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [resolutions, setResolutions] = useState<ToolchainResolution[]>([]);
  const [starting, setStarting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  /** The coding model's state, asked on the Review step and again every few seconds until ready. */
  const [readiness, setReadiness] = useState<Readiness | null>(null);

  useEffect(() => {
    void api.listRemotes().then(setRemotes);
    void api.listSkills().then(setSkills);
  }, [api]);

  useEffect(() => {
    if (step !== 3) {
      return;
    }
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ask = () => {
      void api
        .readiness()
        .then((state) => {
          if (!live) {
            return;
          }
          setReadiness(state);
          if (!state.ready) {
            timer = setTimeout(ask, READINESS_POLL_MS);
          }
        })
        .catch(() => {
          if (live) {
            timer = setTimeout(ask, READINESS_POLL_MS);
          }
        });
    };
    ask();
    return () => {
      live = false;
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [api, step]);

  useEffect(() => {
    if (plan.trim() === "") {
      setDetected([]);
      return;
    }
    void api.detectLanguages(plan).then(setDetected);
  }, [api, plan]);

  useEffect(() => {
    if (breakdown === null || step !== 3) {
      return;
    }
    void api.resolveToolchains(breakdown.languages).then(setResolutions);
  }, [api, breakdown, step]);

  const sentence = useMemo(
    () => (breakdown && resolutions.length > 0 ? reviewSentence(breakdown, resolutions) : ""),
    [breakdown, resolutions],
  );

  async function goToSetup() {
    const proposed = await api.propose(plan, filename);
    setBreakdown((current) =>
      current === null
        ? proposed
        : {
            ...proposed,
            languages: current.languages,
            isolation: current.isolation,
            skills: current.skills,
            crossCheck: current.crossCheck,
            exportTarget: current.exportTarget,
            remoteRef: current.remoteRef,
          },
    );
    setStep(2);
  }

  function update(patch: Partial<Breakdown>) {
    setBreakdown((current) => (current === null ? current : { ...current, ...patch }));
  }

  function toggleLanguage(language: LanguageId) {
    if (breakdown === null) {
      return;
    }
    const present = breakdown.languages.some((c) => c.language === language);
    const languages: LanguageChoice[] = present
      ? breakdown.languages.filter((c) => c.language !== language)
      : [...breakdown.languages, { language, version: "" }];
    update({ languages });
  }

  function setVersion(language: LanguageId, version: string) {
    if (breakdown === null) {
      return;
    }
    update({
      languages: breakdown.languages.map((c) => (c.language === language ? { ...c, version } : c)),
    });
  }

  async function start() {
    if (breakdown === null) {
      return;
    }
    setStarting(true);
    setProblem(null);
    try {
      onStarted(await api.start(breakdown, plan, filename));
    } catch (error: unknown) {
      // The service's own three parts when it answered (the coder is not ready, the plan was
      // refused); the "api not answering" sentences otherwise.
      const parts = asApiError(error).describe({
        whatHappened: "The task didn't start.",
        likelyCause: "The api service didn't answer.",
        whatToDo: "Try again; if it repeats, run `slas logs api` on the host.",
      });
      setProblem(`${parts.whatHappened} ${parts.likelyCause} ${parts.whatToDo}`.replace(/\s+/g, " ").trim());
    } finally {
      setStarting(false);
    }
  }

  const canLeaveSetup =
    breakdown !== null &&
    breakdown.languages.length > 0 &&
    (breakdown.exportTarget !== "remote" || breakdown.remoteRef !== null);

  return (
    <section aria-labelledby="wizard-heading" className="space-y-6">
      <header>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Step {step} of 3 — {STEP_TITLES[step]}
        </p>
        <h2 id="wizard-heading" className="text-xl font-semibold tracking-tight">
          New coding task
        </h2>
      </header>

      {step === 1 && (
        <div className="space-y-4">
          <label className="block text-sm font-medium">
            Plan
            <textarea
              aria-label="Plan"
              className={`${field} min-h-48 font-mono`}
              placeholder="Drop or paste plan.md here. A title line and a list of tasks is enough."
              value={plan}
              onChange={(event) => setPlan(event.target.value)}
            />
          </label>
          <label className="block text-sm font-medium">
            Or choose a file
            <input
              aria-label="Plan file"
              className="mt-1 block text-sm"
              type="file"
              accept=".md,.txt"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (file) {
                  setFilename(file.name);
                  setPlan(await file.text());
                }
              }}
            />
          </label>
          <p className="text-sm text-slate-600 dark:text-slate-400" data-testid="detected">
            {plan.trim() === ""
              ? "Languages are detected from the plan once you add one."
              : detected.length === 0
                ? "No language we recognise is mentioned yet; you can pick them in the next step."
                : `Languages detected: ${detected.map(labelOf).join(", ")}.`}
          </p>
          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={onCancel}>
              Cancel
            </button>
            <button
              type="button"
              className={primary}
              disabled={plan.trim() === ""}
              onClick={() => void goToSetup()}
            >
              Next: Setup
            </button>
          </div>
        </div>
      )}

      {step === 2 && breakdown !== null && (
        <div className="space-y-6">
          <fieldset>
            <legend className="text-sm font-medium">Languages</legend>
            <p className="text-sm text-slate-600 dark:text-slate-400">
              Pick the languages the task uses. Leave a version empty and the agent uses the newest
              bundled toolchain and says which one.
            </p>
            <ul className="mt-2 space-y-2">
              {LANGUAGES.map((info) => {
                const choice = breakdown.languages.find((c) => c.language === info.id);
                return (
                  <li key={info.id} className="flex items-center gap-3">
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={choice !== undefined}
                        onChange={() => toggleLanguage(info.id)}
                      />
                      {info.label}
                    </label>
                    {choice !== undefined && (
                      <input
                        aria-label={`${info.label} version`}
                        className={`${field} mt-0 w-40`}
                        placeholder="newest bundled"
                        value={choice.version}
                        onChange={(event) => setVersion(info.id, event.target.value)}
                      />
                    )}
                  </li>
                );
              })}
            </ul>
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Isolation</legend>
            {(["auto", "gvisor"] as Isolation[]).map((value) => (
              <label key={value} className="mr-4 inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="isolation"
                  checked={breakdown.isolation === value}
                  onChange={() => update({ isolation: value })}
                />
                {value === "auto"
                  ? "gVisor if this host has it, otherwise hardened runc"
                  : "gVisor only (the task waits if gVisor is missing)"}
              </label>
            ))}
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Skills</legend>
            {skills.length === 0 ? (
              <p className="text-sm text-slate-600 dark:text-slate-400">
                No skill is enabled for the Coding Agent. Enable one on the Skills page.
              </p>
            ) : (
              skills.map((skill) => (
                <label key={skill.id} className="mr-4 inline-flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={breakdown.skills.includes(skill.id)}
                    onChange={() =>
                      update({
                        skills: breakdown.skills.includes(skill.id)
                          ? breakdown.skills.filter((s) => s !== skill.id)
                          : [...breakdown.skills, skill.id],
                      })
                    }
                  />
                  {skill.name}
                </label>
              ))
            )}
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Cross-check</legend>
            <label className="inline-flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={breakdown.crossCheck}
                onChange={() => update({ crossCheck: !breakdown.crossCheck })}
              />
              Three voters review the final diff. Their concerns are shown to you; you still decide.
            </label>
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Export</legend>
            {(["zip", "remote", "bundle"] as ExportTarget[]).map((value) => (
              <label key={value} className="mr-4 inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="export"
                  checked={breakdown.exportTarget === value}
                  onChange={() => update({ exportTarget: value })}
                />
                {value === "zip"
                  ? "ZIP file (always available)"
                  : value === "remote"
                    ? "Push a branch to a Git remote you have added"
                    : "Git bundle for another site"}
              </label>
            ))}
            {breakdown.exportTarget === "remote" &&
              (remotes.length === 0 ? (
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
                  You have no saved remote. Add one under Settings → Git remotes; the sandbox itself
                  never holds a credential.
                </p>
              ) : (
                <label className="mt-2 block text-sm">
                  Remote
                  <select
                    aria-label="Remote"
                    className={field}
                    value={breakdown.remoteRef ?? ""}
                    onChange={(event) => update({ remoteRef: event.target.value || null })}
                  >
                    <option value="">Choose a remote</option>
                    {remotes.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
          </fieldset>

          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={() => setStep(1)}>
              Back
            </button>
            <button
              type="button"
              className={primary}
              disabled={!canLeaveSetup}
              onClick={() => setStep(3)}
            >
              Next: Review
            </button>
          </div>
        </div>
      )}

      {step === 3 && breakdown !== null && (
        <div className="space-y-6">
          <label className="block text-sm font-medium">
            Task name
            <input
              aria-label="Task name"
              className={field}
              value={breakdown.title}
              onChange={(event) => update({ title: event.target.value })}
            />
          </label>

          <div>
            <h3 className="text-sm font-medium">Proposed steps</h3>
            <p className="text-sm text-slate-600 dark:text-slate-400">
              Edit, add or remove tasks. The agent does them in this order.
            </p>
            <ol className="mt-2 space-y-2">
              {breakdown.tasks.map((task, index) => (
                <li key={task.n} className="flex items-center gap-2">
                  <span className="w-6 text-sm text-slate-500">{index + 1}.</span>
                  <input
                    aria-label={`Task ${index + 1}`}
                    className={`${field} mt-0`}
                    value={task.title}
                    onChange={(event) =>
                      update({
                        tasks: breakdown.tasks.map((t, i) =>
                          i === index ? { ...t, title: event.target.value } : t,
                        ),
                      })
                    }
                  />
                  <button
                    type="button"
                    className={secondary}
                    aria-label={`Remove task ${index + 1}`}
                    disabled={breakdown.tasks.length === 1}
                    onClick={() =>
                      update({
                        tasks: breakdown.tasks
                          .filter((_, i) => i !== index)
                          .map((t, i) => ({ ...t, n: i + 1 })),
                      })
                    }
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ol>
            <button
              type="button"
              className={`${secondary} mt-2`}
              onClick={() =>
                update({
                  tasks: [...breakdown.tasks, { n: breakdown.tasks.length + 1, title: "New task" }],
                })
              }
            >
              Add a task
            </button>
          </div>

          <div>
            <h3 className="text-sm font-medium">Toolchain</h3>
            <ul className="mt-1 space-y-1" data-testid="resolutions">
              {resolutions.map((r) => (
                <li key={r.language} className="text-sm text-slate-700 dark:text-slate-300">
                  {r.sentence}
                </li>
              ))}
            </ul>
          </div>

          {sentence !== "" && (
            <p className="rounded-md bg-slate-100 p-3 text-sm dark:bg-slate-800" data-testid="sentence">
              {sentence}
            </p>
          )}
          {readiness !== null && (
            <p
              role="status"
              data-testid="readiness"
              className={readiness.ready ? "text-sm text-slate-700 dark:text-slate-300" : "text-sm text-amber-800 dark:text-amber-300"}
            >
              {readiness.sentence}
            </p>
          )}
          {problem !== null && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300">
              {problem}
            </p>
          )}

          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={() => setStep(2)}>
              Back
            </button>
            <button
              type="button"
              className={primary}
              disabled={starting || breakdown.title.trim() === "" || breakdown.tasks.some((t) => t.title.trim() === "")}
              onClick={() => void start()}
            >
              {starting ? "Starting…" : "Start task"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
