// The Coding page's view of the API (CLAUDE.md §9, §10.1). Types mirror the Python
// schemas in slas_orchestrator.coding; the sentences the fake produces are the same ones
// the toolchain resolver produces, so the UI copy is exercised end to end in tests.
// `HttpCodingApi` (./http.ts) is what production uses; `FakeCodingApi` serves `pnpm dev`.

export type LanguageId =
  | "python"
  | "c"
  | "cpp"
  | "rust"
  | "shell"
  | "go"
  | "typescript"
  | "config";

export interface LanguageInfo {
  id: LanguageId;
  label: string;
  tool: string;
}

export const LANGUAGES: readonly LanguageInfo[] = [
  { id: "python", label: "Python", tool: "python" },
  { id: "c", label: "C", tool: "gcc" },
  { id: "cpp", label: "C++", tool: "g++" },
  { id: "rust", label: "Rust", tool: "rustc" },
  { id: "shell", label: "Shell", tool: "bash" },
  { id: "go", label: "Go", tool: "go" },
  { id: "typescript", label: "TypeScript", tool: "typescript" },
  { id: "config", label: "YAML/JSON config", tool: "yamllint" },
];

export interface LanguageChoice {
  language: LanguageId;
  /** Empty means "the agent picks the newest bundled toolchain and says so". */
  version: string;
}

export interface ToolchainResolution {
  language: LanguageId;
  label: string;
  requested: string | null;
  version: string;
  honoured: boolean;
  sentence: string;
}

export interface TaskItem {
  n: number;
  title: string;
}

export type Isolation = "auto" | "gvisor";
export type ExportTarget = "zip" | "remote" | "bundle";

export interface Breakdown {
  title: string;
  tasks: TaskItem[];
  languages: LanguageChoice[];
  isolation: Isolation;
  skills: string[];
  crossCheck: boolean;
  exportTarget: ExportTarget;
  /** The name of a saved remote, never a URI. */
  remoteRef: string | null;
  maxIterations: number;
}

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface TaskStep {
  n: number;
  title: string;
  status: StepStatus;
}

export interface CodingTask {
  ticketId: string;
  title: string;
  state: string;
  sentence: string;
  steps: TaskStep[];
  /** The activity feed, oldest first. The first line is the toolchain choice. */
  feed: string[];
}

export interface SkillSummary {
  id: string;
  name: string;
}

export interface CodingApi {
  detectLanguages(plan: string): Promise<LanguageId[]>;
  propose(plan: string, filename: string): Promise<Breakdown>;
  resolveToolchains(choices: LanguageChoice[]): Promise<ToolchainResolution[]>;
  listRemotes(): Promise<string[]>;
  listSkills(): Promise<SkillSummary[]>;
  /** `filename` is the plan file's name (default plan.md); the api records it on the ticket. */
  start(breakdown: Breakdown, plan: string, filename?: string): Promise<CodingTask>;
  listTasks(): Promise<CodingTask[]>;
}

export function labelOf(language: LanguageId): string {
  return LANGUAGES.find((l) => l.id === language)?.label ?? language;
}

/** The wizard's closing sentence: what will happen, in one breath (same as the Python). */
export function reviewSentence(
  breakdown: Breakdown,
  resolutions: readonly ToolchainResolution[],
): string {
  const tools = resolutions.map((r) => `${r.label} ${r.version}`).join(", ");
  const count = breakdown.tasks.length;
  const check = breakdown.crossCheck
    ? "cross-check the result with 3 voters"
    : "skip the cross-check, as you asked";
  const exportText =
    breakdown.exportTarget === "zip"
      ? "export a ZIP"
      : breakdown.exportTarget === "remote"
        ? `push a branch to ${breakdown.remoteRef ?? "a remote"} for review`
        : "export a Git bundle";
  const head =
    `The agent will work in an isolated sandbox with ${tools}, do ${count} ` +
    `${count === 1 ? "task" : "tasks"}, commit on its own branch, ${check}, and ${exportText}.`;
  const fallbacks = resolutions.filter((r) => !r.honoured).map((r) => r.sentence);
  return fallbacks.length > 0 ? `${head} ${fallbacks.join(" ")}` : head;
}

export function toolchainSentence(resolutions: readonly ToolchainResolution[]): string {
  if (resolutions.length === 0) {
    return "No language was chosen.";
  }
  const parts = resolutions.map((r) => `${r.label} ${r.version}`);
  const listed =
    parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  const fallbacks = resolutions.filter((r) => !r.honoured).map((r) => r.sentence);
  const head = `Toolchain: ${listed}.`;
  return fallbacks.length > 0 ? `${head} ${fallbacks.join(" ")}` : head;
}

// --- fake -------------------------------------------------------------------------------

const EXTENSIONS: Record<string, LanguageId> = {
  ".py": "python",
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".hpp": "cpp",
  ".rs": "rust",
  ".sh": "shell",
  ".go": "go",
  ".ts": "typescript",
  ".tsx": "typescript",
  ".yaml": "config",
  ".yml": "config",
  ".json": "config",
};

// The same keyword hints as slas_sandbox_manager.toolchains.LANGUAGES.
const KEYWORDS: Record<LanguageId, string[]> = {
  python: ["pytest", "pyproject", "pip ", "uv ", "def ", "import "],
  c: ["#include <stdio.h>", "gcc ", "makefile"],
  cpp: ["#include <iostream>", "g++ ", "cmake", "std::"],
  rust: ["cargo ", "cargo.toml", "fn main", "rustc"],
  shell: ["#!/bin/bash", "#!/usr/bin/env bash", "shellcheck", "set -euo"],
  go: ["go mod", "package main", "go test", "func main"],
  typescript: ["tsc", "package.json", "npm ", "pnpm ", "node "],
  config: ["yamllint", "jsonschema", "$schema"],
};

function versionKey(version: string): number[] {
  return version.split(".").map((part) => Number.parseInt(part, 10));
}

function compareVersions(a: string, b: string): number {
  const ka = versionKey(a);
  const kb = versionKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i += 1) {
    const diff = (ka[i] ?? 0) - (kb[i] ?? 0);
    if (diff !== 0) {
      return diff;
    }
  }
  return 0;
}

export class FakeCodingApi implements CodingApi {
  readonly manifest: Record<LanguageId, string[]> = {
    python: ["3.11.10", "3.12.6"],
    c: ["13.2.0"],
    cpp: ["13.2.0"],
    rust: ["1.80.1"],
    shell: ["5.2.21"],
    go: ["1.23.1"],
    typescript: ["5.9.3"],
    config: ["1.35.1"],
  };
  remotes: string[] = [];
  skills: SkillSummary[] = [{ id: "lint-and-test", name: "Lint and test" }];
  readonly tasks: CodingTask[] = [];
  private counter = 0;

  async detectLanguages(plan: string): Promise<LanguageId[]> {
    const scores = new Map<LanguageId, number>();
    for (const match of plan.matchAll(/^```\s*([a-z+]+)/gim)) {
      const info = LANGUAGES.find(
        (l) => l.id === match[1]?.toLowerCase() || l.label.toLowerCase() === match[1]?.toLowerCase(),
      );
      if (info) {
        scores.set(info.id, (scores.get(info.id) ?? 0) + 3);
      }
    }
    for (const match of plan.matchAll(/[\w./-]+(\.[a-z]{1,4})\b/gi)) {
      const language = EXTENSIONS[match[1]?.toLowerCase() ?? ""];
      if (language) {
        scores.set(language, (scores.get(language) ?? 0) + 2);
      }
    }
    const lowered = plan.toLowerCase();
    for (const [language, keywords] of Object.entries(KEYWORDS) as [LanguageId, string[]][]) {
      for (const keyword of keywords) {
        if (lowered.includes(keyword)) {
          scores.set(language, (scores.get(language) ?? 0) + 1);
        }
      }
    }
    const order = LANGUAGES.map((l) => l.id);
    return [...scores.entries()]
      .sort((a, b) => b[1] - a[1] || order.indexOf(a[0]) - order.indexOf(b[0]))
      .map(([language]) => language);
  }

  async propose(plan: string, filename: string): Promise<Breakdown> {
    const heading = /^#\s+(.+?)\s*$/m.exec(plan);
    const title = heading?.[1] ?? filename.replace(/\.md$/, "").replace(/-/g, " ");
    const tasks = [...plan.matchAll(/^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$/gm)]
      .map((m, index) => ({ n: index + 1, title: m[1] ?? "" }))
      .filter((t) => t.title.length > 0);
    const detected = await this.detectLanguages(plan);
    const languages: LanguageId[] = detected.length > 0 ? detected : ["shell"];
    return {
      title,
      tasks: tasks.length > 0 ? tasks : [{ n: 1, title }],
      languages: languages.map((language) => ({ language, version: "" })),
      isolation: "auto",
      skills: [],
      crossCheck: true,
      exportTarget: "zip",
      remoteRef: null,
      maxIterations: 6,
    };
  }

  async resolveToolchains(choices: LanguageChoice[]): Promise<ToolchainResolution[]> {
    return choices.map((choice) => {
      const info = LANGUAGES.find((l) => l.id === choice.language);
      const label = info?.label ?? choice.language;
      const tool = info?.tool ?? choice.language;
      const versions = [...this.manifest[choice.language]].sort(compareVersions);
      const newest = versions[versions.length - 1] ?? "";
      const wanted = choice.version.trim();
      if (wanted === "") {
        return {
          language: choice.language,
          label,
          requested: null,
          version: newest,
          honoured: true,
          sentence: `${label}: no version pinned, so the newest bundled ${tool} ${newest} is used.`,
        };
      }
      const exact = versions.find((v) => v === wanted);
      const prefix = versions.filter((v) => v.startsWith(`${wanted}.`));
      if (exact !== undefined || prefix.length > 0) {
        const chosen = exact ?? prefix[prefix.length - 1] ?? newest;
        const how = exact !== undefined ? "exactly" : `as the newest ${wanted}.x`;
        return {
          language: choice.language,
          label,
          requested: wanted,
          version: chosen,
          honoured: true,
          sentence: `${label} ${wanted} pinned; the bundle has it ${how}, using ${chosen}.`,
        };
      }
      return {
        language: choice.language,
        label,
        requested: wanted,
        version: newest,
        honoured: false,
        sentence:
          `${label} ${wanted} isn't in the offline toolchain bundle, so the newest bundled ` +
          `${newest} is used instead.`,
      };
    });
  }

  async listRemotes(): Promise<string[]> {
    return [...this.remotes];
  }

  async listSkills(): Promise<SkillSummary[]> {
    return [...this.skills];
  }

  async start(breakdown: Breakdown, _plan: string): Promise<CodingTask> {
    this.counter += 1;
    const ticketId = `T-coding-${String(this.counter).padStart(4, "0")}`;
    const resolutions = await this.resolveToolchains(breakdown.languages);
    const steps: TaskStep[] = [
      { n: 1, title: toolchainSentence(resolutions), status: "done" },
      { n: 2, title: "Open an isolated sandbox", status: "running" },
      ...breakdown.tasks.map((t, index) => ({
        n: index + 3,
        title: `Task ${t.n}: ${t.title}`,
        status: "pending" as StepStatus,
      })),
      { n: breakdown.tasks.length + 3, title: "Commit the changes on the agent's branch", status: "pending" },
      { n: breakdown.tasks.length + 4, title: "Export a ZIP of the project", status: "pending" },
    ];
    if (breakdown.crossCheck) {
      steps.push({
        n: steps.length + 1,
        title: "Cross-check the final diff with 3 voters",
        status: "pending",
      });
    }
    const task: CodingTask = {
      ticketId,
      title: breakdown.title,
      state: "Running",
      sentence: `${ticketId} is running: step 2 of ${steps.length}.`,
      steps,
      feed: [toolchainSentence(resolutions), "Opening an isolated sandbox…"],
    };
    this.tasks.unshift(task);
    return task;
  }

  async listTasks(): Promise<CodingTask[]> {
    return [...this.tasks];
  }
}
