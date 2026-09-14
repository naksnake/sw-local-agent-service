// The Factory page's view of the API (CLAUDE.md §9, §10.3). Types mirror the Python schemas in
// slas_factory_executor and slas_orchestrator.factory; the sentences the fake produces are the
// ones the executor produces, so the UI copy is exercised end to end. `FakeFactoryApi` stands in
// until apps/api exists. Nothing here reaches a station: the fake replays a scripted loop.

export type TriggerKind = "mes" | "label" | "manual";

export interface MesTicketView {
  ticketNo: string;
  station: string;
  unitSn: string;
  requestedBy: string;
}

export interface TriggerView {
  kind: TriggerKind;
  ticketNo: string;
  station: string;
  unitSn: string;
}

export interface StationView {
  name: string;
  free: boolean;
  /** "station-08 is leased to T-factory-0007 (mes) until …" when busy. */
  holder: string | null;
}

export interface TemplateView {
  id: string;
  name: string;
  description: string;
  steps: string[];
  skills: string[];
}

export interface JobRules {
  /** Fixed by CLAUDE.md §5.3: PASS needs every voter. Shown, not editable. */
  voters: 3;
  onFail: "hold_station";
  exportSop: true;
  backupStation: boolean;
}

export type CellStatus = "waiting" | "running" | "ok" | "failed" | "skipped";

export interface StepCellView {
  n: number;
  title: string;
  status: CellStatus;
  sentence: string;
  /** Data URIs in the fake; paths under Factory/Jobs/<ticket>/screens in the real API. */
  screenshots: string[];
}

export type Verdict = "PASS" | "FAIL" | "line_lead";

export interface FactoryJob {
  ticketId: string;
  title: string;
  station: string;
  unitSn: string;
  mesTicketNo: string;
  state: string;
  sentence: string;
  verdict: Verdict | null;
  verdictSentence: string;
  held: boolean;
  decidedBy: string;
  steps: StepCellView[];
  /** The child ticket drafted for the line lead, when the unit did not pass. */
  draftTicketId: string | null;
  backupPath: string | null;
}

export interface FactoryApi {
  listMesTickets(): Promise<MesTicketView[]>;
  parseLabel(text: string): Promise<TriggerView | string>;
  listStations(): Promise<StationView[]>;
  listTemplates(): Promise<TemplateView[]>;
  start(trigger: TriggerView, templateId: string, rules: JobRules): Promise<FactoryJob>;
  decide(ticketId: string, verdict: "PASS" | "FAIL", by: string, note: string): Promise<FactoryJob>;
  listJobs(): Promise<FactoryJob[]>;
}

export const STATUS_WORD: Record<CellStatus, string> = {
  waiting: "waiting",
  running: "running",
  ok: "done",
  failed: "needs you",
  skipped: "skipped",
};

/** The wizard's closing sentence: what will happen, in one breath (same shape as the Python). */
export function reviewSentence(trigger: TriggerView, template: TemplateView, rules: JobRules): string {
  const skills = template.skills.length > 0 ? `, using the ${template.skills.join(", ")} skill` : "";
  return (
    `${template.name} for unit ${trigger.unitSn} on ${trigger.station}: ${template.steps.length + 1} ` +
    `steps${skills}. PASS needs ${rules.voters} of ${rules.voters} voters; anything else holds the ` +
    `station for the line lead.` +
    (rules.backupStation ? " The station state is backed up." : " The station state is not backed up.")
  );
}

// --- fake -------------------------------------------------------------------------------

// A 1x1 transparent PNG, the same bytes the screen fake writes.
const TINY_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNgAAIAAAUAAdlmxEAAAAAASUVORK5CYII=";

const LABEL_SN = /\b(?:sn|serial)\s*[:=]?\s*([A-Za-z0-9-]{4,})/i;
const LABEL_STATION = /\bstation\s*[:=]?\s*([a-z][a-z0-9-]*)/i;

const FINAL_TEST: TemplateView = {
  id: "final-test-9-steps",
  name: "Final test, 9 steps",
  description:
    "Power the unit on, log in to the station and start BurnIn, wait for the test, read the result, " +
    "the sensors and the event log, back up the station, decide with 3 voters.",
  steps: [
    "Lease the station and bind the unit",
    "Power the unit on through the fixture",
    "Log in to the station and start BurnIn",
    "Wait for BurnIn to report the test complete",
    "Read the BurnIn result",
    "Read the sensors and compare with the limits",
    "Check that the event log is empty",
    "Back up the station state",
    "Decide PASS or FAIL",
  ],
  skills: ["station-login-burnin"],
};

export class FakeFactoryApi implements FactoryApi {
  mesTickets: MesTicketView[] = [
    { ticketNo: "MES-88131", station: "station-07", unitSn: "SN-GX8-0100", requestedBy: "mes" },
    { ticketNo: "MES-88132", station: "station-07", unitSn: "SN-GX8-0101-F", requestedBy: "mes" },
  ];
  stations: StationView[] = [
    { name: "station-07", free: true, holder: null },
    { name: "station-08", free: false, holder: "station-08 is leased to T-factory-0007 (mes) until 2026-09-14 16:00." },
  ];
  templates: TemplateView[] = [FINAL_TEST];
  readonly jobs: FactoryJob[] = [];
  private counter = 0;

  async listMesTickets(): Promise<MesTicketView[]> {
    return [...this.mesTickets];
  }

  async parseLabel(text: string): Promise<TriggerView | string> {
    const sn = LABEL_SN.exec(text);
    const station = LABEL_STATION.exec(text);
    if (!sn?.[1] || !station?.[1]) {
      return (
        "The label names no unit and station. A label scan or manual entry must carry the serial " +
        "number and the station, for example `SN SN-GX8-0100 station station-07`. Scan the label " +
        "again or type both values."
      );
    }
    return { kind: "label", ticketNo: `manual-${sn[1].toLowerCase()}`, station: station[1].toLowerCase(), unitSn: sn[1] };
  }

  async listStations(): Promise<StationView[]> {
    return this.stations.map((s) => ({ ...s }));
  }

  async listTemplates(): Promise<TemplateView[]> {
    return [...this.templates];
  }

  async start(trigger: TriggerView, templateId: string, rules: JobRules): Promise<FactoryJob> {
    const template = this.templates.find((t) => t.id === templateId) ?? FINAL_TEST;
    this.counter += 1;
    const ticketId = `T-factory-${String(this.counter).padStart(4, "0")}`;
    const fails = trigger.unitSn.endsWith("-F");
    const titles = [...template.steps, `Release ${trigger.station}`];
    const steps: StepCellView[] = titles.map((title, index) => {
      const n = index + 1;
      const isVerdict = title.startsWith("Decide");
      const isRelease = title.startsWith("Release");
      const isLogin = title.startsWith("Log in");
      const shots = isLogin ? Array.from({ length: 6 }, () => TINY_PNG) : title.startsWith("Wait") ? [TINY_PNG, TINY_PNG] : [];
      if (fails && isVerdict) {
        return {
          n,
          title,
          status: "failed",
          sentence:
            `FAIL: Unit ${trigger.unitSn} failed the final test on ${trigger.station}: BurnIn reported FAIL ` +
            `(gpu-memory). The unit stays on and ${trigger.station} is held; a ticket is drafted for the line lead.`,
          screenshots: [],
        };
      }
      if (fails && isRelease) {
        return { n, title, status: "waiting", sentence: "", screenshots: [] };
      }
      return {
        n,
        title,
        status: "ok",
        sentence: isVerdict ? "PASS: 3 of 3 voters say PASS. 3 of 3 agree with the conclusion." : `${title}: done.`,
        screenshots: shots,
      };
    });
    const done = steps.filter((s) => s.status === "ok" || s.status === "failed").length;
    const job: FactoryJob = {
      ticketId,
      title: `Final test of ${trigger.unitSn} on ${trigger.station}`,
      station: trigger.station,
      unitSn: trigger.unitSn,
      mesTicketNo: trigger.ticketNo,
      state: fails ? "Needs review" : "Done",
      sentence: fails
        ? `${done} of ${steps.length} steps done. Verdict: FAIL; the station is held for the line lead.`
        : `${done} of ${steps.length} steps done. Verdict: PASS (3 of 3 voters).`,
      verdict: fails ? "FAIL" : "PASS",
      verdictSentence: steps.find((s) => s.title.startsWith("Decide"))?.sentence ?? "",
      held: fails,
      decidedBy: fails ? "the deterministic gate" : "3 of 3 voters",
      steps,
      draftTicketId: fails ? `T-factory-${String(this.counter + 1).padStart(4, "0")}` : null,
      backupPath: rules.backupStation ? `Backups/stations/${trigger.station}/${ticketId}` : null,
    };
    if (fails) {
      this.counter += 1;
      this.stations = this.stations.map((s) =>
        s.name === trigger.station
          ? { ...s, free: false, holder: `${s.name} is leased to ${ticketId} (${trigger.kind === "mes" ? "mes" : "you"}) until the line lead decides.` }
          : s,
      );
    }
    this.mesTickets = this.mesTickets.filter((t) => t.ticketNo !== trigger.ticketNo);
    this.jobs.unshift(job);
    return job;
  }

  async decide(ticketId: string, verdict: "PASS" | "FAIL", by: string, note: string): Promise<FactoryJob> {
    const job = this.jobs.find((j) => j.ticketId === ticketId);
    if (!job) {
      throw new Error(`There is no job ${ticketId}.`);
    }
    job.verdict = verdict;
    job.held = false;
    job.decidedBy = `${by} (line lead)`;
    job.verdictSentence = `${verdict}: decided by ${by}. ${note}`.trim();
    job.sentence = `${job.steps.filter((s) => s.status !== "waiting").length} of ${job.steps.length} steps done. Verdict: ${verdict} (${by}, line lead).`;
    job.state = "Done";
    this.stations = this.stations.map((s) => (s.name === job.station ? { ...s, free: true, holder: null } : s));
    return job;
  }

  async listJobs(): Promise<FactoryJob[]> {
    return [...this.jobs];
  }
}
