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

export type ControlVerb = "pause" | "resume" | "abort" | "status";

/** Mirrors `slas_station_runner.protocol.ControlState` plus where the operator watches. */
export interface ControlView {
  station: string;
  paused: boolean;
  aborted: boolean;
  by: string;
  sentence: string;
  /** "vnc://127.0.0.1:5901 (relayed over mTLS to https://station-07:8443)"; null when the station has VNC off. */
  watchUrl: string | null;
  /** Why watching is not possible, in three parts, when watchUrl is null. */
  watchProblem: string | null;
}

export interface FactoryApi {
  listMesTickets(): Promise<MesTicketView[]>;
  parseLabel(text: string): Promise<TriggerView | string>;
  listStations(): Promise<StationView[]>;
  listTemplates(): Promise<TemplateView[]>;
  start(trigger: TriggerView, templateId: string, rules: JobRules): Promise<FactoryJob>;
  decide(ticketId: string, verdict: "PASS" | "FAIL", by: string, note: string): Promise<FactoryJob>;
  listJobs(): Promise<FactoryJob[]>;
  /** Watch and take over (CLAUDE.md §5.2): pause at the next step boundary, resume, abort. */
  control(ticketId: string, verb: ControlVerb, by: string): Promise<ControlView>;
}

// --- Admin → Stations (P10) -------------------------------------------------------------------

export type WindowMatch = "contains" | "exact" | "prefix" | "regex";

export interface RetentionView {
  keepDays: number;
  keepFailedDays: number;
  maxPerJob: number;
}

export interface ScreenTuningView {
  windowMatch: WindowMatch;
  actionSettleS: number;
  waitTimeoutScale: number;
  maxActionsPerSecond: number;
}

/** Mirrors `slas_factory_executor.stations.StationRecord`; never a key or a certificate. */
export interface StationRecordView {
  name: string;
  description: string;
  allowedPrograms: string[];
  enrolled: boolean;
  /** "station-07: enrolled 2026-09-14 10:00, runner at https://…, certificate SHA256:…" or "…: not enrolled yet." */
  sentence: string;
  runnerUrl: string | null;
  certFingerprint: string | null;
  vncEnabled: boolean;
  vncPort: number;
  screen: ScreenTuningView;
  retention: RetentionView;
}

export interface IssuedCodeView {
  station: string;
  code: string;
  expiresAt: string;
  /** "Enter this code on station-07 within 15 minutes: XXXX-XXXX-XXXX. It works once; issuing a new code cancels it." */
  sentence: string;
}

export interface StationsAdminApi {
  listStationRecords(): Promise<StationRecordView[]>;
  addStation(name: string, description: string): Promise<StationRecordView | string>;
  issueCode(name: string, by: string): Promise<IssuedCodeView>;
  revoke(name: string): Promise<StationRecordView>;
  removeStation(name: string): Promise<void>;
  saveTuning(name: string, screen: ScreenTuningView, retention: RetentionView, vncEnabled: boolean): Promise<StationRecordView>;
}

export function tuningSentence(screen: ScreenTuningView): string {
  return (
    `Windows matched by ${screen.windowMatch}; ${screen.actionSettleS} s settle after each action; ` +
    `wait timeouts ×${screen.waitTimeoutScale}; at most ${screen.maxActionsPerSecond} actions per second.`
  );
}

export function retentionSentence(r: RetentionView): string {
  return `Screenshots are kept ${r.keepDays} days (${r.keepFailedDays} days for failed or held jobs), at most ${r.maxPerJob} per job.`;
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
    const running = trigger.unitSn.endsWith("-R");
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
      if (running && title.startsWith("Wait")) {
        return { n, title, status: "running", sentence: "Waiting for BurnIn to report the test complete.", screenshots: [TINY_PNG] };
      }
      if (running && n > titles.findIndex((t) => t.startsWith("Wait")) + 1) {
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
      state: running ? "Running" : fails ? "Needs review" : "Done",
      sentence: running
        ? `${done} of ${steps.length} steps done.`
        : fails
          ? `${done} of ${steps.length} steps done. Verdict: FAIL; the station is held for the line lead.`
          : `${done} of ${steps.length} steps done. Verdict: PASS (3 of 3 voters).`,
      verdict: running ? null : fails ? "FAIL" : "PASS",
      verdictSentence: steps.find((s) => s.title.startsWith("Decide"))?.sentence ?? "",
      held: fails,
      decidedBy: running ? "" : fails ? "the deterministic gate" : "3 of 3 voters",
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

  /** Per station: paused/aborted state, as the runner's `ControlState` would report it. */
  readonly controls: Record<string, { paused: boolean; aborted: boolean; by: string }> = {};
  /** Stations whose record has VNC on (the fake's station-08 has it off). */
  vncStations = new Set(["station-07"]);

  async control(ticketId: string, verb: ControlVerb, by: string): Promise<ControlView> {
    const job = this.jobs.find((j) => j.ticketId === ticketId);
    if (!job) {
      throw new Error(`There is no job ${ticketId}.`);
    }
    const state = this.controls[job.station] ?? { paused: false, aborted: false, by: "" };
    if (verb === "pause") {
      Object.assign(state, { paused: true, aborted: false, by });
    } else if (verb === "resume") {
      Object.assign(state, { paused: false, aborted: false, by });
    } else if (verb === "abort") {
      Object.assign(state, { paused: false, aborted: true, by });
      job.state = "Failed";
      job.sentence = `Stopped: ${by} took over ${job.station}.`;
      for (const cell of job.steps) {
        if (cell.status === "running") {
          cell.status = "failed";
          cell.sentence = `Stopped: ${by} took over ${job.station}.`;
        }
      }
    }
    this.controls[job.station] = state;
    const sentence = state.aborted
      ? `${state.by} aborted the run on ${job.station}.`
      : state.paused
        ? `${state.by} has taken over ${job.station}; the runner sends no input until it is resumed.`
        : `The runner drives ${job.station}.`;
    const vnc = this.vncStations.has(job.station);
    return {
      station: job.station,
      paused: state.paused,
      aborted: state.aborted,
      by: state.by,
      sentence,
      watchUrl: vnc ? `vnc://127.0.0.1:5901 (relayed over mTLS to https://${job.station}:8443)` : null,
      watchProblem: vnc
        ? null
        : `VNC is not enabled on ${job.station}. The station record has VNC off. Enable it under Admin → Stations and re-enrol.`,
    };
  }
}

const DEFAULT_SCREEN: ScreenTuningView = { windowMatch: "contains", actionSettleS: 0, waitTimeoutScale: 1, maxActionsPerSecond: 10 };
const DEFAULT_RETENTION: RetentionView = { keepDays: 30, keepFailedDays: 180, maxPerJob: 400 };
const STATION_NAME = /^[a-z][a-z0-9-]{1,62}$/;
const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

function fakeCode(): string {
  const group = () => Array.from({ length: 4 }, (_, i) => CODE_ALPHABET[(i * 7 + 3) % CODE_ALPHABET.length]).join("");
  return `${group()}-${group()}-${group()}`;
}

export class FakeStationsAdminApi implements StationsAdminApi {
  records: StationRecordView[] = [
    {
      name: "station-07",
      description: "Final test, line 2",
      allowedPrograms: ["fixture-ctl", "burnin-ctl", "sensors-ctl", "evlog", "station-ctl"],
      enrolled: true,
      sentence: "station-07: enrolled 2026-09-14 10:00, runner at https://station-07.factory.internal:8443, certificate SHA256:3F2A…9C1D.",
      runnerUrl: "https://station-07.factory.internal:8443",
      certFingerprint: "SHA256:3F2A…9C1D",
      vncEnabled: true,
      vncPort: 5900,
      screen: { ...DEFAULT_SCREEN, windowMatch: "prefix", actionSettleS: 0.3 },
      retention: { ...DEFAULT_RETENTION },
    },
    {
      name: "station-08",
      description: "",
      allowedPrograms: ["fixture-ctl", "burnin-ctl"],
      enrolled: false,
      sentence: "station-08: not enrolled yet.",
      runnerUrl: null,
      certFingerprint: null,
      vncEnabled: false,
      vncPort: 5900,
      screen: { ...DEFAULT_SCREEN },
      retention: { ...DEFAULT_RETENTION },
    },
  ];
  issued: IssuedCodeView[] = [];

  async listStationRecords(): Promise<StationRecordView[]> {
    return this.records.map((r) => ({ ...r }));
  }

  async addStation(name: string, description: string): Promise<StationRecordView | string> {
    if (!STATION_NAME.test(name)) {
      return (
        `"${name}" is not a station name. Names are lowercase letters, digits and dashes, starting with a letter, ` +
        `like station-09. Change the name and add the station again.`
      );
    }
    if (this.records.some((r) => r.name === name)) {
      return `There is already a station called ${name}. Every station has one record; pick another name or issue a code for the existing one.`;
    }
    const record: StationRecordView = {
      name,
      description,
      allowedPrograms: [],
      enrolled: false,
      sentence: `${name}: not enrolled yet.`,
      runnerUrl: null,
      certFingerprint: null,
      vncEnabled: true,
      vncPort: 5900,
      screen: { ...DEFAULT_SCREEN },
      retention: { ...DEFAULT_RETENTION },
    };
    this.records.push(record);
    return { ...record };
  }

  async issueCode(name: string, by: string): Promise<IssuedCodeView> {
    if (!this.records.some((r) => r.name === name)) {
      throw new Error(`There is no station called ${name}.`);
    }
    void by;
    const code = fakeCode();
    const issued: IssuedCodeView = {
      station: name,
      code,
      expiresAt: "2026-09-14T10:15:00Z",
      sentence: `Enter this code on ${name} within 15 minutes: ${code}. It works once; issuing a new code cancels it.`,
    };
    this.issued.push(issued);
    return issued;
  }

  async revoke(name: string): Promise<StationRecordView> {
    const record = this.records.find((r) => r.name === name);
    if (!record) {
      throw new Error(`There is no station called ${name}.`);
    }
    Object.assign(record, { enrolled: false, runnerUrl: null, certFingerprint: null, sentence: `${name}: not enrolled yet.` });
    return { ...record };
  }

  async removeStation(name: string): Promise<void> {
    this.records = this.records.filter((r) => r.name !== name);
  }

  async saveTuning(name: string, screen: ScreenTuningView, retention: RetentionView, vncEnabled: boolean): Promise<StationRecordView> {
    const record = this.records.find((r) => r.name === name);
    if (!record) {
      throw new Error(`There is no station called ${name}.`);
    }
    Object.assign(record, { screen: { ...screen }, retention: { ...retention }, vncEnabled });
    return { ...record };
  }
}
