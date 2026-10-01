// The Factory page and Admin → Stations over HTTP (docs/api-contract-round-2.md §5 "Factory",
// §6 factory-executor station records, §8): browser routes under /api/v1/factory and
// /api/v1/stations. Screenshots arrive as paths the api serves; a station record never carries
// a key or a certificate beyond its fingerprint.

import { ApiError, asApiError, type HttpClient } from "../api/http";
import { asRecord, bool, isRecord, nullableStr, num, oneOf, recordList, seg, str, strList, type Wire } from "../api/wire";
import { agents } from "../copy/en";
import type {
  CellStatus,
  ControlVerb,
  ControlView,
  FactoryApi,
  FactoryJob,
  IssuedCodeView,
  JobRules,
  MesTicketView,
  RetentionView,
  ScreenTuningView,
  StationRecordView,
  StationView,
  StationsAdminApi,
  StepCellView,
  TemplateView,
  TriggerView,
  Verdict,
  WindowMatch,
} from "./api";

const CELL_STATUSES: readonly CellStatus[] = ["waiting", "running", "ok", "failed", "skipped"];
const VERDICTS: readonly Verdict[] = ["PASS", "FAIL", "line_lead"];
const WINDOW_MATCHES: readonly WindowMatch[] = ["contains", "exact", "prefix", "regex"];

// --- Factory ---------------------------------------------------------------------------------

export function mesTicketFromWire(wire: Wire): MesTicketView {
  return {
    ticketNo: str(wire["ticket_no"]),
    station: str(wire["station"]),
    unitSn: str(wire["unit_sn"]),
    requestedBy: str(wire["requested_by"]),
  };
}

/** `MesTicket` on the wire; who asked is the MES ticket's requester, else the trigger's kind. */
export function triggerToWire(trigger: TriggerView): Wire {
  return {
    ticket_no: trigger.ticketNo,
    station: trigger.station,
    unit_sn: trigger.unitSn,
    requested_by: trigger.requestedBy ?? (trigger.kind === "mes" ? "mes" : trigger.kind),
  };
}

export function stationFromWire(wire: Wire): StationView {
  return {
    name: str(wire["name"]),
    free: bool(wire["free"]),
    holder: nullableStr(wire["holder"]),
    description: str(wire["description"]),
    enrolled: bool(wire["enrolled"]),
    sentence: str(wire["sentence"]),
  };
}

export function templateFromWire(wire: Wire): TemplateView {
  return {
    id: str(wire["id"]),
    name: str(wire["name"]),
    description: str(wire["description"], str(wire["sentence"])),
    // The contract sends step titles; a TestLoopTemplate dump sends step objects with a title.
    steps: (Array.isArray(wire["steps"]) ? wire["steps"] : []).map((s) => (typeof s === "string" ? s : str(asRecord(s)["title"]))),
    skills: strList(wire["skills"]),
  };
}

function cellFromWire(wire: Wire, index: number): StepCellView {
  const one = nullableStr(wire["screenshot"]);
  const many = strList(wire["screenshots"]);
  return {
    n: num(wire["n"], index + 1),
    title: str(wire["title"]),
    status: oneOf(wire["status"], CELL_STATUSES, "waiting"),
    sentence: str(wire["sentence"]),
    screenshots: many.length > 0 ? many : one !== null && one !== "" ? [one] : [],
  };
}

export function jobFromWire(value: unknown): FactoryJob {
  const w = asRecord(value);
  const verdict = typeof w["verdict"] === "string" ? oneOf(w["verdict"], VERDICTS, "line_lead") : null;
  return {
    ticketId: str(w["ticket_id"]),
    title: str(w["title"]),
    station: str(w["station"]),
    unitSn: str(w["unit_sn"]),
    mesTicketNo: str(w["mes_ticket_no"]),
    state: str(w["state"]),
    sentence: str(w["sentence"]),
    verdict,
    verdictSentence: str(w["verdict_sentence"]),
    held: bool(w["held"]),
    decidedBy: str(w["decided_by"]),
    steps: recordList(w["cells"]).map(cellFromWire),
    draftTicketId: nullableStr(w["draft_ticket_id"]),
    backupPath: nullableStr(w["backup_path"]),
    votes: strList(w["votes"]),
  };
}

export function rulesToWire(rules: JobRules): Wire {
  return {
    voters: rules.voters,
    on_fail: rules.onFail === "hold_station" ? "hold" : rules.onFail,
    export_sop: rules.exportSop,
    backup_station: rules.backupStation,
  };
}

export class HttpFactoryApi implements FactoryApi {
  constructor(private readonly http: HttpClient) {}

  async listMesTickets(): Promise<MesTicketView[]> {
    return recordList(await this.http.get<unknown>("/factory/mes-tickets")).map(mesTicketFromWire);
  }

  /** The trigger the label names, or the api's problem sentence. */
  async parseLabel(text: string): Promise<TriggerView | string> {
    const answer = asRecord(await this.http.post<unknown>("/factory/labels/parse", { text }));
    if (isRecord(answer["trigger"])) {
      const ticket = mesTicketFromWire(answer["trigger"]);
      return { kind: "label", ticketNo: ticket.ticketNo, station: ticket.station, unitSn: ticket.unitSn, requestedBy: ticket.requestedBy };
    }
    return str(answer["problem"], str(answer["sentence"]));
  }

  async listStations(): Promise<StationView[]> {
    return recordList(await this.http.get<unknown>("/factory/stations")).map(stationFromWire);
  }

  async listTemplates(): Promise<TemplateView[]> {
    return recordList(await this.http.get<unknown>("/factory/templates")).map(templateFromWire);
  }

  async start(trigger: TriggerView, templateId: string, rules: JobRules): Promise<FactoryJob> {
    const body = { trigger: triggerToWire(trigger), template_id: templateId, rules: rulesToWire(rules) };
    return jobFromWire(await this.http.post<unknown>("/factory/jobs", body));
  }

  /** The deciding person comes from the session; `by` is the page's label and does not travel. */
  async decide(ticketId: string, verdict: "PASS" | "FAIL", _by: string, note: string): Promise<FactoryJob> {
    return jobFromWire(await this.http.post<unknown>(`/factory/jobs/${seg(ticketId)}/decide`, { verdict, note }));
  }

  async listJobs(): Promise<FactoryJob[]> {
    return recordList(await this.http.get<unknown>("/factory/jobs")).map(jobFromWire);
  }

  /** One job, for a page that follows a ticket; not part of `FactoryApi` yet. */
  async getJob(ticketId: string): Promise<FactoryJob> {
    return jobFromWire(await this.http.get<unknown>(`/factory/jobs/${seg(ticketId)}`));
  }

  /**
   * The api answers `{sentence, watch_url, watch_problem}`; paused/aborted are what the verb
   * asked for, since the runner's ControlState is not on this wire. `status` only looks.
   */
  async control(ticketId: string, verb: ControlVerb, by: string): Promise<ControlView> {
    const answer = asRecord(await this.http.post<unknown>(`/factory/jobs/${seg(ticketId)}/control`, { verb }));
    const control = asRecord(answer["control"]);
    return {
      station: str(control["station"], str(answer["station"])),
      paused: bool(control["paused"], verb === "pause"),
      aborted: bool(control["aborted"], verb === "abort"),
      by: str(control["by"], verb === "status" ? "" : by),
      sentence: str(answer["sentence"]),
      watchUrl: nullableStr(answer["watch_url"]),
      watchProblem: nullableStr(answer["watch_problem"]),
    };
  }
}

// --- Admin → Stations --------------------------------------------------------------------------

function screenFromWire(wire: Wire): ScreenTuningView {
  return {
    windowMatch: oneOf(wire["window_match"], WINDOW_MATCHES, "contains"),
    actionSettleS: num(wire["action_settle_s"], 0),
    waitTimeoutScale: num(wire["wait_timeout_scale"], 1),
    maxActionsPerSecond: num(wire["max_actions_per_second"], 10),
  };
}

function screenToWire(screen: ScreenTuningView): Wire {
  return {
    window_match: screen.windowMatch,
    action_settle_s: screen.actionSettleS,
    wait_timeout_scale: screen.waitTimeoutScale,
    max_actions_per_second: screen.maxActionsPerSecond,
  };
}

function retentionFromWire(wire: Wire): RetentionView {
  return {
    keepDays: num(wire["keep_days"], 30),
    keepFailedDays: num(wire["keep_failed_days"], 180),
    maxPerJob: num(wire["max_per_job"], 400),
  };
}

function retentionToWire(retention: RetentionView): Wire {
  return { keep_days: retention.keepDays, keep_failed_days: retention.keepFailedDays, max_per_job: retention.maxPerJob };
}

export function stationRecordFromWire(value: unknown): StationRecordView {
  const w = asRecord(value);
  const vnc = asRecord(w["vnc"]);
  const runnerUrl = nullableStr(w["runner_url"]);
  // `enrolled` is a property on the Python record (enrolled_at and runner_url both set).
  const enrolled = bool(w["enrolled"], typeof w["enrolled_at"] === "string" && runnerUrl !== null);
  return {
    name: str(w["name"]),
    description: str(w["description"]),
    allowedPrograms: strList(w["allowed_programs"]),
    enrolled,
    sentence: str(w["sentence"]),
    runnerUrl,
    certFingerprint: nullableStr(w["cert_fingerprint"]),
    vncEnabled: bool(vnc["enabled"], bool(w["vnc_enabled"])),
    vncPort: num(vnc["port"], num(w["vnc_port"], 5900)),
    screen: screenFromWire(asRecord(w["screen"])),
    retention: retentionFromWire(asRecord(w["retention"])),
  };
}

function looksLikeRecord(value: unknown): boolean {
  return isRecord(value) && typeof value["name"] === "string";
}

export class HttpStationsAdminApi implements StationsAdminApi {
  constructor(private readonly http: HttpClient) {}

  async listStationRecords(): Promise<StationRecordView[]> {
    return recordList(await this.http.get<unknown>("/stations")).map(stationRecordFromWire);
  }

  /** The new record, or the api's refusal as one sentence the page shows inline. */
  async addStation(name: string, description: string): Promise<StationRecordView | string> {
    try {
      return stationRecordFromWire(await this.http.post<unknown>("/stations", { name, description }));
    } catch (error: unknown) {
      const apiError = asApiError(error);
      if (apiError.status === 401) {
        throw apiError;
      }
      return agents.stationProblem(apiError.parts);
    }
  }

  /** The issuing person comes from the session; `by` is the page's label and does not travel. */
  async issueCode(name: string, _by: string): Promise<IssuedCodeView> {
    const w = asRecord(await this.http.post<unknown>(`/stations/${seg(name)}/code`, {}));
    return { station: str(w["station"], name), code: str(w["code"]), expiresAt: str(w["expires_at"]), sentence: str(w["sentence"]) };
  }

  async revoke(name: string): Promise<StationRecordView> {
    const answer = await this.http.post<unknown>(`/stations/${seg(name)}/revoke`, {});
    return this.recordOrRefetch(answer, name);
  }

  async removeStation(name: string): Promise<void> {
    await this.http.del(`/stations/${seg(name)}`);
  }

  async saveTuning(name: string, screen: ScreenTuningView, retention: RetentionView, vncEnabled: boolean): Promise<StationRecordView> {
    const body = { screen: screenToWire(screen), retention: retentionToWire(retention), vnc_enabled: vncEnabled };
    const answer = await this.http.put<unknown>(`/stations/${seg(name)}/tuning`, body);
    return this.recordOrRefetch(answer, name);
  }

  /** Routes whose answer the contract leaves open: use a record when one came, else read it back. */
  private async recordOrRefetch(answer: unknown, name: string): Promise<StationRecordView> {
    if (looksLikeRecord(answer)) {
      return stationRecordFromWire(answer);
    }
    const record = (await this.listStationRecords()).find((r) => r.name === name);
    if (record === undefined) {
      throw new ApiError({
        status: 404,
        parts: {
          whatHappened: `There is no station called ${name}.`,
          likelyCause: "It was removed while this page was open.",
          whatToDo: "Reload the page.",
        },
      });
    }
    return record;
  }
}
