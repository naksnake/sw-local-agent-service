import { useEffect, useState } from "react";

import {
  type FactoryApi,
  type FactoryJob,
  type JobRules,
  type MesTicketView,
  type StationView,
  type TemplateView,
  type TriggerView,
  reviewSentence,
} from "./api";

// The three-step wizard from CLAUDE.md §9: Trigger → Test loop → Rules, ending in a sentence
// that says what will happen and one verb button. Copy: docs/ui/new-factory-job.md.

interface Props {
  api: FactoryApi;
  onStarted: (job: FactoryJob) => void;
  onCancel: () => void;
}

type Step = 1 | 2 | 3;

const STEP_TITLES: Record<Step, string> = { 1: "Trigger", 2: "Test loop", 3: "Rules" };

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

export function NewFactoryJobWizard({ api, onStarted, onCancel }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [mesTickets, setMesTickets] = useState<MesTicketView[]>([]);
  const [stations, setStations] = useState<StationView[]>([]);
  const [templates, setTemplates] = useState<TemplateView[]>([]);
  const [trigger, setTrigger] = useState<TriggerView | null>(null);
  const [labelText, setLabelText] = useState("");
  const [labelProblem, setLabelProblem] = useState<string | null>(null);
  const [manualSn, setManualSn] = useState("");
  const [manualStation, setManualStation] = useState("");
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [rules, setRules] = useState<JobRules>({ voters: 3, onFail: "hold_station", exportSop: true, backupStation: true });
  const [starting, setStarting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    void api.listMesTickets().then(setMesTickets);
    void api.listStations().then(setStations);
    void api.listTemplates().then((list) => {
      setTemplates(list);
      setTemplateId((current) => current ?? list[0]?.id ?? null);
    });
  }, [api]);

  const template = templates.find((t) => t.id === templateId) ?? null;
  const stationBusy = trigger !== null && stations.some((s) => s.name === trigger.station && !s.free);
  const busyHolder = trigger === null ? null : (stations.find((s) => s.name === trigger.station)?.holder ?? null);

  async function scanLabel() {
    const parsed = await api.parseLabel(labelText);
    if (typeof parsed === "string") {
      setLabelProblem(parsed);
      setTrigger(null);
      return;
    }
    setLabelProblem(null);
    setTrigger(parsed);
  }

  function useManual() {
    if (manualSn.trim() === "" || manualStation.trim() === "") {
      return;
    }
    setTrigger({ kind: "manual", ticketNo: `manual-${manualSn.trim().toLowerCase()}`, station: manualStation.trim().toLowerCase(), unitSn: manualSn.trim() });
  }

  async function start() {
    if (trigger === null || template === null) {
      return;
    }
    setStarting(true);
    setProblem(null);
    try {
      onStarted(await api.start(trigger, template.id, rules));
    } catch (error: unknown) {
      setProblem(
        "The job didn't start. The api service didn't answer. Try again; if it repeats, run " +
          "`slas logs api` on the host.",
      );
      console.error(error);
    } finally {
      setStarting(false);
    }
  }

  return (
    <section aria-labelledby="factory-wizard-heading" className="space-y-6">
      <header>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Step {step} of 3 — {STEP_TITLES[step]}
        </p>
        <h2 id="factory-wizard-heading" className="text-xl font-semibold tracking-tight">
          New factory job
        </h2>
      </header>

      {step === 1 && (
        <div className="space-y-6">
          <fieldset>
            <legend className="text-sm font-medium">Production tickets waiting in the MES</legend>
            {mesTickets.length === 0 ? (
              <p className="text-sm text-slate-600 dark:text-slate-400">
                No production ticket is waiting. Scan a label or enter the unit by hand below.
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {mesTickets.map((ticket) => (
                  <li key={ticket.ticketNo}>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="radio"
                        name="trigger"
                        aria-label={ticket.ticketNo}
                        checked={trigger?.ticketNo === ticket.ticketNo}
                        onChange={() =>
                          setTrigger({
                            kind: "mes",
                            ticketNo: ticket.ticketNo,
                            station: ticket.station,
                            unitSn: ticket.unitSn,
                            requestedBy: ticket.requestedBy,
                          })
                        }
                      />
                      {ticket.ticketNo}: unit {ticket.unitSn} on {ticket.station}
                      <span className="text-slate-500">· from {ticket.requestedBy}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Or scan a label</legend>
            <div className="flex gap-2">
              <input
                aria-label="Label"
                className={field}
                placeholder="Scan or paste the label, for example SN SN-GX8-0100 station station-07"
                value={labelText}
                onChange={(event) => setLabelText(event.target.value)}
              />
              <button type="button" className={`${secondary} mt-1`} onClick={() => void scanLabel()}>
                Read label
              </button>
            </div>
            {labelProblem !== null && (
              <p role="alert" className="mt-1 text-sm text-red-700 dark:text-red-300">
                {labelProblem}
              </p>
            )}
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium">Or enter the unit by hand</legend>
            <div className="flex flex-wrap gap-2">
              <input aria-label="Serial number" className={`${field} w-56`} placeholder="Serial number" value={manualSn} onChange={(e) => setManualSn(e.target.value)} />
              <select aria-label="Station" className={`${field} w-48`} value={manualStation} onChange={(e) => setManualStation(e.target.value)}>
                <option value="">Pick a station</option>
                {stations.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}
                    {s.free ? "" : " (busy)"}
                  </option>
                ))}
              </select>
              <button type="button" className={`${secondary} mt-1`} onClick={useManual}>
                Use these
              </button>
            </div>
          </fieldset>

          <p className="text-sm text-slate-600 dark:text-slate-400" data-testid="trigger-note">
            {trigger === null
              ? "Pick a production ticket, scan a label, or enter the unit by hand."
              : stationBusy
                ? `Unit ${trigger.unitSn} on ${trigger.station}: the station is busy. ${busyHolder ?? ""}`
                : `Unit ${trigger.unitSn} on ${trigger.station}, from ${trigger.kind === "mes" ? `MES ticket ${trigger.ticketNo}` : trigger.kind === "label" ? "a label scan" : "a manual entry"}.`}
          </p>

          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={onCancel}>
              Cancel
            </button>
            <button type="button" className={primary} disabled={trigger === null || stationBusy} onClick={() => setStep(2)}>
              Next: Test loop
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <fieldset>
            <legend className="text-sm font-medium">Test-loop template</legend>
            <ul className="mt-2 space-y-2">
              {templates.map((t) => (
                <li key={t.id}>
                  <label className="flex items-start gap-2 text-sm">
                    <input type="radio" name="template" aria-label={t.name} checked={templateId === t.id} onChange={() => setTemplateId(t.id)} />
                    <span>
                      {t.name}
                      <span className="block text-slate-600 dark:text-slate-400">{t.description}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
          {template !== null && (
            <div>
              <h3 className="text-sm font-medium">Steps</h3>
              <ol className="mt-1 list-decimal pl-5 text-sm" data-testid="template-steps">
                {template.steps.map((title) => (
                  <li key={title}>{title}</li>
                ))}
                <li>Release the station</li>
              </ol>
              <p className="mt-2 text-sm text-slate-600 dark:text-slate-400" data-testid="skills-used">
                {template.skills.length === 0
                  ? "No skill is used; every step is a station command or a reading."
                  : `Skills used for the GUI steps: ${template.skills.join(", ")}. Every GUI step is screenshot before and after.`}
              </p>
            </div>
          )}
          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={() => setStep(1)}>
              Back
            </button>
            <button type="button" className={primary} disabled={template === null} onClick={() => setStep(3)}>
              Next: Rules
            </button>
          </div>
        </div>
      )}

      {step === 3 && trigger !== null && template !== null && (
        <div className="space-y-6">
          <fieldset>
            <legend className="text-sm font-medium">Voters</legend>
            <p className="text-sm">
              Three voters see the result, the sensors and the event log. PASS needs all three; a split vote
              goes to the line lead. The voters never mark a unit PASS or FAIL on their own.
            </p>
          </fieldset>
          <fieldset>
            <legend className="text-sm font-medium">On failure</legend>
            <p className="text-sm">
              The unit stays on, the station stays leased, and a ticket is drafted for the line lead.
            </p>
          </fieldset>
          <fieldset>
            <legend className="text-sm font-medium">Exports</legend>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked readOnly aria-label="Line SOP in English and Chinese" />
              Production line SOP in English and Chinese (always)
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={rules.backupStation}
                aria-label="Back up the station state"
                onChange={() => setRules({ ...rules, backupStation: !rules.backupStation })}
              />
              Back up the station state (config, recent logs, application versions)
            </label>
          </fieldset>
          <p className="rounded-md bg-slate-100 p-3 text-sm dark:bg-slate-800" data-testid="sentence">
            {reviewSentence(trigger, template, rules)}
          </p>
          {problem !== null && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300">
              {problem}
            </p>
          )}
          <div className="flex gap-3">
            <button type="button" className={secondary} onClick={() => setStep(2)}>
              Back
            </button>
            <button type="button" className={primary} disabled={starting} onClick={() => void start()}>
              {starting ? "Starting…" : "Start job"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
