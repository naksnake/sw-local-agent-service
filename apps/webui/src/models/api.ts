// The Models page's api (docs/api-contract.md, docs/api-contract-round-2.md §3, §3b, §8):
// the registry read from Models/models.yaml on every request; "Add a model" through the
// model fetcher (ADR-0018); roles and voters saved through the model manager. Keys stay
// snake_case as the api sends them. The in-memory fake lives in ./fake.ts.

import type { HttpClient } from "../api/http";
import type { ThreePart } from "../copy/en";

export interface ModelView {
  id: string;
  display_name: string;
  family: string;
  path: string;
  quant: string;
  vram_gib: number;
  context: number;
  roles: string[];
  /** Whether Models/<path>/SHA256SUMS exists on the host. */
  present: boolean;
}

export interface ThreePartWire {
  what_happened: string;
  likely_cause: string;
  what_to_do: string;
}

export interface RegistryView {
  sentence: string;
  models: ModelView[];
  roles: Record<string, string>;
  voters: string[];
  problem: ThreePartWire | null;
}

/** The roles the gateway routes by (CLAUDE.md §7), in the order the page shows them. */
export const ROLES = ["coder", "planner", "triage", "embed", "rerank"] as const;
export type Role = (typeof ROLES)[number];

export type FetchState = "planning" | "downloading" | "importing" | "done" | "failed" | "cancelled";
export const RUNNING_STATES: readonly FetchState[] = ["planning", "downloading", "importing"];

/** One fetch as the model fetcher reports it (contract §3b `FetchRecord`). */
export interface FetchRecordView {
  id: string;
  link: string;
  model_id: string;
  repo: string;
  revision: string;
  display_name: string;
  state: FetchState;
  bytes_done: number;
  bytes_total: number;
  files_done: number;
  files_total: number;
  sentence: string;
  started_at: string;
  finished_at: string | null;
  by: string;
  problem: ThreePartWire | null;
  entry: Record<string, unknown> | null;
}

export function isRunning(record: FetchRecordView): boolean {
  return RUNNING_STATES.includes(record.state);
}

/** `PUT /models/roles`: only the keys given change; a role set to null is unassigned. */
export interface RolesChange {
  roles?: Record<string, string | null>;
  voters?: string[];
}

export interface RolesResult {
  sentence: string;
  roles: Record<string, string>;
  voters: string[];
  models: { id: string; display_name: string; family: string; quant: string; roles: string[]; present: boolean }[];
}

export function problemParts(problem: ThreePartWire): ThreePart {
  return { whatHappened: problem.what_happened, likelyCause: problem.likely_cause, whatToDo: problem.what_to_do };
}

export interface ModelsApi {
  registry(): Promise<RegistryView>;
  /** Every fetch the model fetcher remembers, newest first. */
  fetches(): Promise<FetchRecordView[]>;
  /** Start downloading and importing the model behind a pasted link. */
  startFetch(link: string, id?: string): Promise<FetchRecordView>;
  /** Cancel a running fetch (after the current file) or remove a finished record. */
  cancelFetch(id: string): Promise<string>;
  saveRoles(change: RolesChange): Promise<RolesResult>;
}

export class HttpModelsApi implements ModelsApi {
  constructor(private readonly http: HttpClient) {}

  registry(): Promise<RegistryView> {
    return this.http.get<RegistryView>("/models");
  }

  fetches(): Promise<FetchRecordView[]> {
    return this.http.get<FetchRecordView[]>("/models/fetches");
  }

  startFetch(link: string, id?: string): Promise<FetchRecordView> {
    const trimmed = id?.trim() ?? "";
    return this.http.post<FetchRecordView>("/models/fetches", { link, id: trimmed === "" ? null : trimmed });
  }

  async cancelFetch(id: string): Promise<string> {
    const answer = await this.http.del<{ sentence: string } | undefined>(`/models/fetches/${encodeURIComponent(id)}`);
    return answer?.sentence ?? "";
  }

  saveRoles(change: RolesChange): Promise<RolesResult> {
    return this.http.put<RolesResult>("/models/roles", change);
  }
}
