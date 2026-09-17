// GET /api/v1/models (docs/api-contract.md): the registry read from Models/models.yaml on
// every request. Read-only in round 1. The in-memory fake lives in ./fake.ts.

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

export function problemParts(problem: ThreePartWire): ThreePart {
  return { whatHappened: problem.what_happened, likelyCause: problem.likely_cause, whatToDo: problem.what_to_do };
}

export interface ModelsApi {
  registry(): Promise<RegistryView>;
}

export class HttpModelsApi implements ModelsApi {
  constructor(private readonly http: HttpClient) {}

  registry(): Promise<RegistryView> {
    return this.http.get<RegistryView>("/models");
  }
}
