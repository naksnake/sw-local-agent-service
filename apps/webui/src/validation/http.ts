// The Validation page over HTTP (docs/api-contract-round-2.md §5 "Validation", §8): browser
// routes under /api/v1/validation. A suite goes up as text (.md) or base64 (.xlsx) and comes
// back as a SuiteView the client carries back unchanged on preview and start. Targets are
// opaque references; no credential is ever on this wire (INV-5).

import type { HttpClient } from "../api/http";
import { asRecord, bool, nullableStr, num, oneOf, recordList, seg, str, strList, type Wire } from "../api/wire";
import { agents } from "../copy/en";
import type {
  CycleCellView,
  CycleStatus,
  FindingView,
  PlanPreview,
  RunView,
  SuiteItemView,
  SuiteView,
  TargetView,
  ValidationApi,
} from "./api";

const CYCLE_STATUSES: readonly CycleStatus[] = ["waiting", "running", "ok", "finding", "failed", "skipped"];
const XLSX = /\.xlsx$/i;
/** `[Issue] … | [Owner] EE` (CLAUDE.md §10.2), with an optional ticket id anywhere in the line. */
const ISSUE_OWNER = /^\s*\[Issue\]\s*(.+?)\s*\|\s*\[Owner\]\s*([^\s|]+)\s*(?:\|\s*(.*))?$/;
const TICKET_ID = /\bT-[a-z]+-\d+\b/;

function paramsFromWire(value: unknown): Record<string, string> | undefined {
  const w = asRecord(value);
  const entries = Object.entries(w).filter((entry): entry is [string, string] => typeof entry[1] === "string");
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

function itemFromWire(wire: Wire, index: number): SuiteItemView {
  const destructive = bool(wire["destructive"]);
  const params = paramsFromWire(wire["params"]);
  return {
    n: num(wire["n"], index + 1),
    title: str(wire["title"]),
    cycles: num(wire["cycles"], 1),
    destructive,
    // Not in the contract's SuiteView, but on the Python SuiteItem; absent means not flagged.
    approved: bool(wire["approved"], false),
    sentence: str(wire["sentence"]),
    ...(typeof wire["action"] === "string" ? { action: wire["action"] } : {}),
    ...(params !== undefined ? { params } : {}),
  };
}

export function suiteFromWire(value: unknown): SuiteView {
  const w = asRecord(value);
  return {
    title: str(w["title"]),
    items: recordList(w["items"]).map(itemFromWire),
    problem: nullableStr(w["problem"]),
    ...(typeof w["source"] === "string" ? { source: w["source"] } : {}),
    ...(typeof w["sentence"] === "string" ? { sentence: w["sentence"] } : {}),
  };
}

/** The SuiteView as the api sent it, so preview and start compile exactly what was shown. */
export function suiteToWire(suite: SuiteView): Wire {
  return {
    source: suite.source ?? "",
    title: suite.title,
    items: suite.items.map((item) => ({
      n: item.n,
      title: item.title,
      action: item.action ?? "",
      params: item.params ?? {},
      cycles: item.cycles,
      destructive: item.destructive,
      approved: item.approved,
      sentence: item.sentence,
    })),
    sentence: suite.sentence ?? "",
    problem: suite.problem,
  };
}

export function targetFromWire(wire: Wire): TargetView {
  return {
    ref: str(wire["ref"]),
    model: str(wire["model"]),
    free: bool(wire["free"]),
    holder: nullableStr(wire["holder"]),
    armed: bool(wire["armed"]),
    sentence: str(wire["sentence"]),
  };
}

function cellFromWire(wire: Wire, index: number): CycleCellView {
  return {
    n: num(wire["n"], index + 1),
    kind: str(wire["kind"]),
    status: oneOf(wire["status"], CYCLE_STATUSES, "waiting"),
    sentence: str(wire["sentence"]),
  };
}

/** The api sends each finding as one sentence; owner and ticket are read out of it when present. */
export function findingFromWire(value: unknown): FindingView {
  if (typeof value !== "string") {
    const w = asRecord(value);
    return { sentence: str(w["sentence"]), owner: str(w["owner"]), ticketId: str(w["ticket_id"]) };
  }
  const match = ISSUE_OWNER.exec(value);
  const ticket = TICKET_ID.exec(match?.[3] ?? value)?.[0] ?? "";
  if (match) {
    return { sentence: match[1] ?? value, owner: match[2] ?? "", ticketId: ticket };
  }
  return { sentence: value, owner: "", ticketId: ticket };
}

export function runFromWire(value: unknown): RunView {
  const w = asRecord(value);
  return {
    ticketId: str(w["ticket_id"]),
    title: str(w["title"]),
    target: str(w["target"]),
    state: str(w["state"]),
    sentence: str(w["sentence"]),
    cycles: recordList(w["cells"]).map(cellFromWire),
    console: strList(w["console_tail"]),
    findings: Array.isArray(w["findings"]) ? w["findings"].map(findingFromWire) : [],
    approvalsPending: strList(w["pending_approvals"]),
    votes: strList(w["votes"]),
  };
}

function isCycleItem(item: SuiteItemView): boolean {
  return /cycl|reboot/i.test(item.title) || /cycl|reboot/i.test(item.action ?? "");
}

export function previewFromWire(value: unknown, suite: SuiteView): PlanPreview {
  const w = asRecord(value);
  const steps = recordList(w["steps"]);
  const crossCheck = asRecord(w["cross_check"]);
  return {
    sentence: str(w["sentence"]),
    stepCount: steps.length,
    // Not on the wire: the power cycles are the cycle items' counts, as the fake computes them.
    cycleCount: suite.items.filter(isCycleItem).reduce((n, item) => n + item.cycles, 0),
    destructive: strList(w["destructive_steps"]),
    guardrails: strList(w["guardrails"]),
    crossCheck: w["cross_check"] === null || w["cross_check"] === undefined ? agents.notCrossChecked : str(crossCheck["sentence"]),
  };
}

export class HttpValidationApi implements ValidationApi {
  constructor(private readonly http: HttpClient) {}

  /** `.md` travels as `text`; for `.xlsx` the caller passes the file's base64 and it travels as `content_base64`. */
  async parseSuite(text: string, filename: string): Promise<SuiteView> {
    const body = XLSX.test(filename) ? { filename, content_base64: text } : { filename, text };
    return suiteFromWire(await this.http.post<unknown>("/validation/suites/parse", body));
  }

  async listTargets(): Promise<TargetView[]> {
    return recordList(await this.http.get<unknown>("/validation/targets")).map(targetFromWire);
  }

  async preview(suite: SuiteView, target: string): Promise<PlanPreview> {
    const answer = await this.http.post<unknown>("/validation/preview", { suite: suiteToWire(suite), target });
    return previewFromWire(answer, suite);
  }

  async start(suite: SuiteView, target: string): Promise<RunView> {
    return runFromWire(await this.http.post<unknown>("/validation/runs", { suite: suiteToWire(suite), target }));
  }

  /** The acting person comes from the session; `decidedBy` is the page's label and does not travel. */
  async approve(ticketId: string, _decidedBy: string): Promise<RunView> {
    return runFromWire(await this.http.post<unknown>(`/validation/runs/${seg(ticketId)}/approve`, {}));
  }

  async listRuns(): Promise<RunView[]> {
    return recordList(await this.http.get<unknown>("/validation/runs")).map(runFromWire);
  }

  /** One run, for a page that follows a ticket; not part of `ValidationApi` yet. */
  async getRun(ticketId: string): Promise<RunView> {
    return runFromWire(await this.http.get<unknown>(`/validation/runs/${seg(ticketId)}`));
  }
}
