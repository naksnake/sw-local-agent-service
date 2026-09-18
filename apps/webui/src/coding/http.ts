// The Coding page over HTTP (docs/api-contract-round-2.md §5 "Coding", §8): browser routes
// under /api/v1/coding, snake_case on the wire, the page's camelCase views out. Nothing here
// carries a credential: a remote is only ever its name (INV-5, INV-14).

import type { HttpClient } from "../api/http";
import { asRecord, bool, num, oneOf, recordList, seg, str, strList, type Wire } from "../api/wire";
import {
  type Breakdown,
  type CodingApi,
  type CodingTask,
  type ExportTarget,
  type Isolation,
  LANGUAGES,
  type LanguageChoice,
  type LanguageId,
  type Readiness,
  type SkillSummary,
  type StepStatus,
  type TaskStep,
  type ToolchainResolution,
} from "./api";

const LANGUAGE_IDS: readonly LanguageId[] = LANGUAGES.map((l) => l.id);
const STEP_STATUSES: readonly StepStatus[] = ["pending", "running", "done", "failed", "skipped"];
const ISOLATIONS: readonly Isolation[] = ["auto", "gvisor"];
const EXPORTS: readonly ExportTarget[] = ["zip", "remote", "bundle"];

function isLanguageId(value: unknown): value is LanguageId {
  return typeof value === "string" && (LANGUAGE_IDS as readonly string[]).includes(value);
}

/** `{"language", "version"}` — an empty version travels as null ("pick the newest"). */
function choiceToWire(choice: LanguageChoice): Wire {
  const version = choice.version.trim();
  return { language: choice.language, version: version === "" ? null : version };
}

function choiceFromWire(wire: Wire): LanguageChoice | null {
  return isLanguageId(wire["language"]) ? { language: wire["language"], version: str(wire["version"]) } : null;
}

export function breakdownToWire(breakdown: Breakdown): Wire {
  return {
    title: breakdown.title,
    tasks: breakdown.tasks.map((t) => ({ n: t.n, title: t.title })),
    languages: breakdown.languages.map(choiceToWire),
    isolation: breakdown.isolation,
    skills: breakdown.skills,
    cross_check: breakdown.crossCheck,
    export_target: breakdown.exportTarget,
    remote_ref: breakdown.remoteRef,
    max_iterations: breakdown.maxIterations,
  };
}

export function breakdownFromWire(value: unknown): Breakdown {
  const w = asRecord(value);
  const languages = recordList(w["languages"])
    .map(choiceFromWire)
    .filter((c): c is LanguageChoice => c !== null);
  return {
    title: str(w["title"]),
    tasks: recordList(w["tasks"]).map((t, index) => ({ n: num(t["n"], index + 1), title: str(t["title"]) })),
    languages,
    isolation: oneOf(w["isolation"], ISOLATIONS, "auto"),
    skills: strList(w["skills"]),
    crossCheck: bool(w["cross_check"], true),
    // The contract names it export_target; the Python model calls the same field `export`.
    exportTarget: oneOf(w["export_target"] ?? w["export"], EXPORTS, "zip"),
    remoteRef: typeof w["remote_ref"] === "string" && w["remote_ref"] !== "" ? w["remote_ref"] : null,
    maxIterations: num(w["max_iterations"], 6),
  };
}

export function resolutionFromWire(value: unknown): ToolchainResolution | null {
  const w = asRecord(value);
  if (!isLanguageId(w["language"])) {
    return null;
  }
  const language = w["language"];
  return {
    language,
    label: str(w["label"], LANGUAGES.find((l) => l.id === language)?.label ?? language),
    requested: typeof w["requested"] === "string" && w["requested"] !== "" ? w["requested"] : null,
    version: str(w["version"]),
    honoured: bool(w["honoured"], true),
    sentence: str(w["sentence"]),
  };
}

function stepFromWire(wire: Wire, index: number): TaskStep {
  return { n: num(wire["n"], index + 1), title: str(wire["title"]), status: oneOf(wire["status"], STEP_STATUSES, "pending") };
}

export function taskFromWire(value: unknown): CodingTask {
  const w = asRecord(value);
  return {
    ticketId: str(w["ticket_id"]),
    title: str(w["title"]),
    state: str(w["state"]),
    sentence: str(w["sentence"]),
    steps: recordList(w["steps"]).map(stepFromWire),
    feed: strList(w["feed"]),
  };
}

export class HttpCodingApi implements CodingApi {
  constructor(private readonly http: HttpClient) {}

  async readiness(): Promise<Readiness> {
    const w = asRecord(await this.http.get<unknown>("/coding/readiness"));
    return { ready: bool(w["ready"], false), sentence: str(w["sentence"]) };
  }

  async detectLanguages(plan: string): Promise<LanguageId[]> {
    const answer = await this.http.post<unknown>("/coding/languages/detect", { plan });
    return strList(asRecord(answer)["languages"]).filter(isLanguageId);
  }

  async propose(plan: string, filename: string): Promise<Breakdown> {
    return breakdownFromWire(await this.http.post<unknown>("/coding/propose", { plan, filename }));
  }

  async resolveToolchains(choices: LanguageChoice[]): Promise<ToolchainResolution[]> {
    const answer = await this.http.post<unknown>("/coding/toolchains/resolve", { choices: choices.map(choiceToWire) });
    return recordList(answer)
      .map(resolutionFromWire)
      .filter((r): r is ToolchainResolution => r !== null);
  }

  async listRemotes(): Promise<string[]> {
    const answer = await this.http.get<unknown>("/coding/remotes");
    return strList(asRecord(answer)["remotes"]);
  }

  async listSkills(): Promise<SkillSummary[]> {
    const answer = await this.http.get<unknown>("/coding/skills");
    return recordList(asRecord(answer)["skills"]).map((s) => ({ id: str(s["id"]), name: str(s["name"], str(s["id"])) }));
  }

  async start(breakdown: Breakdown, plan: string, filename = "plan.md"): Promise<CodingTask> {
    return taskFromWire(await this.http.post<unknown>("/coding/tasks", { breakdown: breakdownToWire(breakdown), plan, filename }));
  }

  async listTasks(): Promise<CodingTask[]> {
    return recordList(await this.http.get<unknown>("/coding/tasks")).map(taskFromWire);
  }

  async remove(ticketId: string): Promise<string> {
    const answer = await this.http.del<unknown>(`/coding/tasks/${seg(ticketId)}`);
    return str(asRecord(answer)["sentence"], `${ticketId} was removed.`);
  }

  /** One task, for a page that follows a ticket; not part of `CodingApi` yet. */
  async getTask(ticketId: string): Promise<CodingTask> {
    return taskFromWire(await this.http.get<unknown>(`/coding/tasks/${seg(ticketId)}`));
  }
}
