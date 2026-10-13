// The Skills page's calls (docs/api-contract-round-2.md "Skills", proxied by the api under
// /api/v1/skills). Skills are data (INV-12): the page sends YAML text and shows what the
// orchestrator's importer says about it; nothing here interprets a skill. The in-memory fake
// lives in ./fake.ts so a production bundle never carries it.

import type { HttpClient } from "../api/http";

export type SkillAgent = "coding" | "validation" | "factory";
export type SkillRisk = "safe" | "caution" | "destructive";

export const AGENT_LABEL: Record<SkillAgent, string> = {
  coding: "Coding Agent",
  validation: "Validation Agent",
  factory: "Factory Agent",
};

export interface SkillRow {
  id: string;
  name: string;
  version: string;
  risk: SkillRisk;
  agents: SkillAgent[];
  requires: string[];
  /** Per agent the author allows: on here or not. */
  enabled: Partial<Record<SkillAgent, boolean>>;
  sentence: string;
}

export interface SkillInput {
  name: string;
  type: string;
  required: boolean;
}

export interface SkillDetail extends SkillRow {
  description: string;
  /** One line per step, e.g. "run: ruff check .". */
  steps: string[];
  inputs: SkillInput[];
  imported_by: string | null;
  imported_at: string | null;
  /** The skill file as it exports, to edit or download. */
  yaml: string;
}

export interface SkillReport {
  skill: { id: string; name: string; version: string };
  risk: SkillRisk;
  sentence: string;
  approval_sentence: string | null;
}

/** What Check answers: everything an import would say, and nothing stored. */
export interface SkillCheck extends SkillReport {
  /** The version already in the library that saving would replace, or null. */
  replaces: string | null;
  steps: string[];
}

export interface SkillImported extends SkillReport {
  /** "Off for every agent." or why a replacement switched it off. */
  state_sentence: string;
}

export interface SkillExample {
  id: string;
  name: string;
  description: string;
  agents: SkillAgent[];
  requires: string[];
  risk: SkillRisk;
  installed: boolean;
  yaml: string;
}

export interface SkillsApi {
  list(): Promise<SkillRow[]>;
  examples(): Promise<SkillExample[]>;
  detail(id: string): Promise<SkillDetail>;
  check(yaml: string): Promise<SkillCheck>;
  save(yaml: string): Promise<SkillImported>;
  setEnabled(id: string, agent: SkillAgent, on: boolean): Promise<SkillRow>;
  exportFile(id: string): Promise<{ yaml: string; content_hash: string }>;
  /** Delete the skill; answers the sentence to show. */
  remove(id: string): Promise<string>;
}

const path = (id: string) => `/skills/${encodeURIComponent(id)}`;

export class HttpSkillsApi implements SkillsApi {
  constructor(private readonly http: HttpClient) {}

  list(): Promise<SkillRow[]> {
    return this.http.get<SkillRow[]>("/skills");
  }

  examples(): Promise<SkillExample[]> {
    return this.http.get<SkillExample[]>("/skills/examples");
  }

  detail(id: string): Promise<SkillDetail> {
    return this.http.get<SkillDetail>(path(id));
  }

  check(yaml: string): Promise<SkillCheck> {
    return this.http.post<SkillCheck>("/skills/validate", { yaml });
  }

  save(yaml: string): Promise<SkillImported> {
    return this.http.post<SkillImported>("/skills/import", { yaml });
  }

  setEnabled(id: string, agent: SkillAgent, on: boolean): Promise<SkillRow> {
    return this.http.post<SkillRow>(`${path(id)}/${on ? "enable" : "disable"}`, { agent });
  }

  exportFile(id: string): Promise<{ yaml: string; content_hash: string }> {
    return this.http.get<{ yaml: string; content_hash: string }>(`${path(id)}/export`);
  }

  async remove(id: string): Promise<string> {
    return (await this.http.del<{ sentence: string }>(path(id))).sentence;
  }
}

/** A blank skill to start from: one safe `run` step for the Coding Agent. */
export const BLANK_SKILL = `skill:
  id: my-checks
  name: "My project checks"
  version: 1.0.0
  description: "What this skill makes sure of, in one sentence."
  agents: [coding]
  requires: [files]
  steps:
    - run: { command: [python, -m, pytest, -q], expect_exit: 0, timeout_s: 600 }
  on_failure: stop
`;
