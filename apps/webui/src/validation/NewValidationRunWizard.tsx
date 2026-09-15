import { useEffect, useState } from "react";

import type { PlanPreview, RunView, SuiteView, TargetView, ValidationApi } from "./api";

// The three-step wizard from CLAUDE.md §9: Suite → Target → Review & approve, ending in a
// sentence that says what will happen and one verb button. Copy: docs/ui/new-validation-run.md.

interface Props {
  api: ValidationApi;
  onStarted: (run: RunView) => void;
  onCancel: () => void;
}

type Step = 1 | 2 | 3;

const STEP_TITLES: Record<Step, string> = { 1: "Suite", 2: "Target", 3: "Review & approve" };

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

export function NewValidationRunWizard({ api, onStarted, onCancel }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [text, setText] = useState("");
  const [filename, setFilename] = useState("suite.md");
  const [suite, setSuite] = useState<SuiteView | null>(null);
  const [targets, setTargets] = useState<TargetView[]>([]);
  const [target, setTarget] = useState<string | null>(null);
  const [preview, setPreview] = useState<PlanPreview | null>(null);
  const [starting, setStarting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (text.trim() === "") {
      setSuite(null);
      return;
    }
    void api.parseSuite(text, filename).then(setSuite);
  }, [api, text, filename]);

  useEffect(() => {
    void api.listTargets().then(setTargets);
  }, [api]);

  useEffect(() => {
    if (step !== 3 || suite === null || target === null) {
      return;
    }
    void api.preview(suite, target).then(setPreview);
  }, [api, step, suite, target]);

  const unapproved = suite?.items.filter((i) => i.destructive && !i.approved) ?? [];
  const canLeaveSuite = suite !== null && suite.problem === null && suite.items.length > 0 && unapproved.length === 0;

  async function start() {
    if (suite === null || target === null) {
      return;
    }
    setStarting(true);
    setProblem(null);
    try {
      onStarted(await api.start(suite, target));
    } catch (error: unknown) {
      setProblem(
        "The run didn't start. The api service didn't answer. Try again; if it repeats, run " +
          "`slas logs api` on the host.",
      );
      console.error(error);
    } finally {
      setStarting(false);
    }
  }

  return (
    <section aria-labelledby="validation-wizard-heading" className="space-y-6">
      <header>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Step {step} of 3 — {STEP_TITLES[step]}
        </p>
        <h2 id="validation-wizard-heading" className="text-xl font-semibold tracking-tight">
          New validation run
        </h2>
      </header>

      {step === 1 && (
        <div className="space-y-4">
          <label className="block text-sm font-medium">
            Suite
            <textarea
              aria-label="Suite"
              className={`${field} min-h-48 font-mono`}
              placeholder="Paste suite.md here: a title line and one bullet or table row per item, for example `- DC cycle x25, settle 60 s`."
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </label>
          <label className="block text-sm font-medium">
            Or choose a file
            <input
              aria-label="Suite file"
              className="mt-1 block text-sm"
              type="file"
              accept=".md,.xlsx"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (file) {
                  setFilename(file.name);
                  setText(await file.text());
                }
              }}
            />
          </label>
          {suite === null ? (
            <p className="text-sm text-slate-600 dark:text-slate-400" data-testid="items-note">
              The items are listed here once you add the suite. Destructive items are flagged.
            </p>
          ) : suite.problem !== null ? (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300">
              {suite.problem}
            </p>
          ) : (
            <div>
              <p className="text-sm text-slate-600 dark:text-slate-400" data-testid="items-note">
                {suite.title}: {suite.items.length} {suite.items.length === 1 ? "item" : "items"},{" "}
                {suite.items.reduce((n, i) => n + i.cycles, 0)} cycles in total.
              </p>
              <ul className="mt-2 space-y-1" data-testid="items">
                {suite.items.map((item) => (
                  <li key={item.n} className="text-sm">
                    {item.sentence}
                    {item.destructive && item.approved && (
                      <span className="text-amber-700 dark:text-amber-300">
                        {" "}
                        — destructive; the run will ask for your approval before this step.
                      </span>
                    )}
                    {item.destructive && !item.approved && (
                      <span className="text-red-700 dark:text-red-300">
                        {" "}
                        — destructive and not flagged as approved in the suite; add `approved` to the
                        item or the plan is refused.
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={onCancel}>
              Cancel
            </button>
            <button
              type="button"
              className={primary}
              disabled={!canLeaveSuite}
              onClick={() => setStep(2)}
            >
              Next: Target
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <fieldset>
            <legend className="text-sm font-medium">Pick a free server</legend>
            <p className="text-sm text-slate-600 dark:text-slate-400">
              One run per target at a time. Credentials come from the vault; nothing here shows or
              asks for a password.
            </p>
            <ul className="mt-2 space-y-2">
              {targets.map((candidate) => (
                <li key={candidate.ref}>
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="radio"
                      name="target"
                      disabled={!candidate.free}
                      checked={target === candidate.ref}
                      onChange={() => setTarget(candidate.ref)}
                      aria-label={candidate.ref}
                    />
                    <span>
                      {candidate.ref} <span className="text-slate-500">· {candidate.model}</span>
                      {!candidate.free && candidate.holder !== null && (
                        <span className="block text-slate-500">Busy: {candidate.holder}</span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            {targets.length === 0 && (
              <p className="text-sm text-slate-600 dark:text-slate-400">
                No server is registered yet. Add one under Admin → Targets before starting a run.
              </p>
            )}
          </fieldset>
          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={() => setStep(1)}>
              Back
            </button>
            <button
              type="button"
              className={primary}
              disabled={target === null}
              onClick={() => setStep(3)}
            >
              Next: Review
            </button>
          </div>
        </div>
      )}

      {step === 3 && suite !== null && target !== null && (
        <div className="space-y-6">
          {preview === null ? (
            <p className="text-sm text-slate-600 dark:text-slate-400">Compiling the plan…</p>
          ) : (
            <>
              <p className="rounded-md bg-slate-100 p-3 text-sm dark:bg-slate-800" data-testid="sentence">
                {preview.sentence}
              </p>
              <p className="text-sm" data-testid="cross-check">
                {preview.crossCheck}
              </p>
              {preview.destructive.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium">Steps that will ask for your approval</h3>
                  <ul className="mt-1 list-disc pl-5 text-sm" data-testid="destructive">
                    {preview.destructive.map((title) => (
                      <li key={title}>{title}</li>
                    ))}
                  </ul>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                    The run stops before each of them until you approve it on the Validation page.
                  </p>
                </div>
              )}
              <div>
                <h3 className="text-sm font-medium">Guardrails</h3>
                <ul className="mt-1 list-disc pl-5 text-sm" data-testid="guardrails">
                  {preview.guardrails.map((sentence) => (
                    <li key={sentence}>{sentence}</li>
                  ))}
                </ul>
              </div>
            </>
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
              disabled={starting || preview === null}
              onClick={() => void start()}
            >
              {starting ? "Starting…" : "Approve and start"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
