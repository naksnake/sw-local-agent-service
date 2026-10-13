// In-memory Skills for `pnpm dev`, the smoke test and the component tests. It speaks the
// orchestrator's sentences (services/agent-core-orchestrator/slas_orchestrator/service/skills.py
// and slas_skills.importer) so the page is tested with the words the api sends. It reads
// only the few fields the page shows; the real importer is the one that validates a skill.

import { fail, unreachable } from "../session/fake";
import {
  AGENT_LABEL,
  type SkillAgent,
  type SkillCheck,
  type SkillDetail,
  type SkillExample,
  type SkillImported,
  type SkillRisk,
  type SkillRow,
  type SkillsApi,
} from "./api";

const PYTHON_LINT_AND_TEST = `# Python lint and tests — skill python-lint-and-test v1.0.0 for SW Local Agent Service
skill:
  id: python-lint-and-test
  name: "Python lint and tests"
  version: 1.0.0
  description: "Every coding task must pass ruff and pytest in the sandbox before it is done."
  agents: [coding]
  requires: [files]
  steps:
    - run: { command: [ruff, check, .], expect_exit: 0, timeout_s: 120 }
    - run: { command: [python, -m, pytest, -q], expect_exit: 0, timeout_s: 600 }
  on_failure: stop
`;

const SEL_COLLECT_CLEAR = `# Collect and clear the BMC event log — skill sel-collect-clear v1.2.0 for SW Local Agent Service
skill:
  id: sel-collect-clear
  name: "Collect and clear the BMC event log"
  version: 1.2.0
  agents: [validation, factory]
  requires: [redfish]
  inputs:
    target: { type: target_ref, required: true }
  steps:
    - redfish: { id: sel, target: "{{ target }}", action: get_sel }
    - copy: { from: "{{ steps.sel.file }}", to: logs/sel-before.json }
    - assert: { condition: "{{ steps.sel.count }} < 4000", message: "SEL nearly full before clearing" }
  outputs:
    sel_file: { from: sel }
  on_failure: stop
`;

const PRIMITIVES = new Set([
  "focus_window",
  "click",
  "double_click",
  "right_click",
  "type",
  "key",
  "scroll",
  "wait_for",
  "screenshot",
  "assert_visible",
  "run",
  "ssh",
  "copy",
  "redfish",
  "sel_snapshot",
  "inventory_snapshot",
  "wait",
  "set",
  "assert",
  "if",
  "foreach",
]);

interface Parsed {
  id: string;
  name: string;
  version: string;
  description: string;
  agents: SkillAgent[];
  requires: string[];
  steps: string[];
  risk: SkillRisk;
}

function field(text: string, name: string): string {
  const match = new RegExp(`^\\s*${name}:\\s*"?([^"\\n]*?)"?\\s*$`, "m").exec(text);
  return match?.[1]?.trim() ?? "";
}

function list(text: string, name: string): string[] {
  const match = new RegExp(`^\\s*${name}:\\s*\\[([^\\]]*)\\]`, "m").exec(text);
  return (match?.[1] ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

function stepLine(primitive: string, body: string): string {
  const command = /command:\s*\[([^\]]*)\]/.exec(body);
  if (command !== null) {
    return `${primitive}: ${(command[1] ?? "").split(",").map((a) => a.trim().replace(/^"|"$/g, "")).join(" ")}`;
  }
  const action = /action:\s*([\w]+)/.exec(body);
  if (action !== null) {
    const target = /target:\s*"?([^",}]+)"?/.exec(body);
    return `${primitive}: ${action[1] ?? ""} on ${target?.[1]?.trim() ?? "the target"}`;
  }
  return `${primitive}: ${body.trim()}`;
}

/** Just enough of a skill file for the page: the real checks are the orchestrator's. */
export function parseSkillText(text: string): Parsed {
  if (!/^skill:\s*$/m.test(text)) {
    fail(400, {
      whatHappened: "The skill file is not valid YAML.",
      likelyCause: "It has no top-level `skill:` key.",
      whatToDo: "Fix the file and import it again; the format is CLAUDE.md §6.1.",
    });
  }
  const id = field(text, "id");
  const version = field(text, "version");
  if (!/^[a-z][a-z0-9_-]*$/.test(id) || !/^\d+\.\d+\.\d+$/.test(version)) {
    fail(400, {
      whatHappened: "the skill file is not a valid skill: id and version are missing or malformed.",
      likelyCause: "A skill needs an id like my-checks and a version like 1.0.0.",
      whatToDo: "Fix the file and import it again; the whitelist is CLAUDE.md §6.2.",
    });
  }
  const steps: string[] = [];
  let risk: SkillRisk = "safe";
  for (const match of text.matchAll(/^\s*-\s+([a-z_]+):\s*\{(.*)\}\s*$/gm)) {
    const primitive = match[1] ?? "";
    if (!PRIMITIVES.has(primitive)) {
      fail(400, {
        whatHappened: `the skill file uses \`${primitive}\`, which is not a skill primitive.`,
        likelyCause: "Skills may only use the whitelisted verbs; there is no shell, eval, python or download.",
        whatToDo: "Fix the file and import it again; the whitelist is CLAUDE.md §6.2.",
      });
    }
    const body = match[2] ?? "";
    if (primitive === "redfish" && /action:\s*(power_on|power_off|force_off|graceful_restart)/.test(body)) {
      risk = "destructive";
    } else if (["run", "ssh", "copy"].includes(primitive) && risk === "safe") {
      risk = "caution";
    }
    steps.push(stepLine(primitive, body));
  }
  const agents = list(text, "agents").filter((a): a is SkillAgent => a in AGENT_LABEL);
  return {
    id,
    name: field(text, "name") || id,
    version,
    description: field(text, "description"),
    agents: agents.length > 0 ? agents : ["coding"],
    requires: list(text, "requires"),
    steps,
    risk,
  };
}

interface Stored {
  parsed: Parsed;
  yaml: string;
  importedBy: string;
  importedAt: string;
  enabled: Partial<Record<SkillAgent, string>>;
}

function stateSentence(stored: Stored): string {
  const on = Object.keys(stored.enabled) as SkillAgent[];
  if (on.length === 0) {
    return "Off for every agent.";
  }
  const date = new Date(stored.importedAt).toLocaleDateString("en-GB", { day: "2-digit", month: "long", year: "numeric" });
  return `On for ${on
    .sort()
    .map((agent) => `the ${AGENT_LABEL[agent]} since ${date}`)
    .join("; ")}.`;
}

function reportSentence(parsed: Parsed): string {
  const count = parsed.steps.length;
  return `${parsed.name} v${parsed.version}: ${count} ${count === 1 ? "step" : "steps"}, needs ${parsed.requires.join(", ") || "no capabilities"}, risk ${parsed.risk}.`;
}

export class FakeSkillsApi implements SkillsApi {
  /** Set to make every call fail as if the orchestrator were down. */
  down = false;
  private library = new Map<string, Stored>();

  constructor(
    private readonly who: () => string = () => "pat@slas.local",
    private readonly isAdmin: () => boolean = () => false,
  ) {}

  private guard(): void {
    if (this.down) {
      unreachable();
    }
  }

  private stored(id: string): Stored {
    const stored = this.library.get(id);
    if (stored === undefined) {
      fail(404, {
        whatHappened: `There is no skill ${id} in this installation's library.`,
        likelyCause: "It was never imported here, or it was removed.",
        whatToDo: "Import it under Skills, or pick one from the list.",
      });
    }
    return stored;
  }

  private row(stored: Stored): SkillRow {
    const { parsed } = stored;
    return {
      id: parsed.id,
      name: parsed.name,
      version: parsed.version,
      risk: parsed.risk,
      agents: [...parsed.agents],
      requires: [...parsed.requires],
      enabled: Object.fromEntries(parsed.agents.map((agent) => [agent, agent in stored.enabled])),
      sentence: stateSentence(stored),
    };
  }

  async list(): Promise<SkillRow[]> {
    this.guard();
    return [...this.library.values()].sort((a, b) => a.parsed.id.localeCompare(b.parsed.id)).map((s) => this.row(s));
  }

  async examples(): Promise<SkillExample[]> {
    this.guard();
    return [PYTHON_LINT_AND_TEST, SEL_COLLECT_CLEAR].map((yaml) => {
      const parsed = parseSkillText(yaml);
      return {
        id: parsed.id,
        name: parsed.name,
        description: parsed.description,
        agents: parsed.agents,
        requires: parsed.requires,
        risk: parsed.risk,
        installed: this.library.has(parsed.id),
        yaml,
      };
    });
  }

  async detail(id: string): Promise<SkillDetail> {
    this.guard();
    const stored = this.stored(id);
    return {
      ...this.row(stored),
      description: stored.parsed.description,
      steps: [...stored.parsed.steps],
      inputs: [],
      imported_by: stored.importedBy,
      imported_at: stored.importedAt,
      yaml: stored.yaml,
    };
  }

  async check(yaml: string): Promise<SkillCheck> {
    this.guard();
    const parsed = parseSkillText(yaml);
    return {
      skill: { id: parsed.id, name: parsed.name, version: parsed.version },
      risk: parsed.risk,
      sentence: reportSentence(parsed),
      approval_sentence:
        parsed.risk === "destructive" ? "This skill contains destructive steps and will ask for your approval every time it runs." : null,
      replaces: this.library.get(parsed.id)?.parsed.version ?? null,
      steps: [...parsed.steps],
    };
  }

  async save(yaml: string): Promise<SkillImported> {
    const checked = await this.check(yaml);
    const parsed = parseSkillText(yaml);
    const previous = this.library.get(parsed.id);
    const stored: Stored = {
      parsed,
      yaml,
      importedBy: this.who(),
      importedAt: new Date().toISOString(),
      enabled: previous !== undefined && previous.parsed.risk === parsed.risk ? { ...previous.enabled } : {},
    };
    this.library.set(parsed.id, stored);
    return { ...checked, state_sentence: stateSentence(stored) };
  }

  async setEnabled(id: string, agent: SkillAgent, on: boolean): Promise<SkillRow> {
    this.guard();
    const stored = this.stored(id);
    if (on && !stored.parsed.agents.includes(agent)) {
      fail(400, {
        whatHappened: `${stored.parsed.name} can't be turned on for the ${AGENT_LABEL[agent]}.`,
        likelyCause: `Its author allows only ${stored.parsed.agents.map((a) => `the ${AGENT_LABEL[a]}`).join(", ")}.`,
        whatToDo: "Ask the author for a version that lists that agent, or use it where it is allowed.",
      });
    }
    if (on) {
      stored.enabled[agent] = new Date().toISOString();
    } else {
      delete stored.enabled[agent];
    }
    return this.row(stored);
  }

  async exportFile(id: string): Promise<{ yaml: string; content_hash: string }> {
    this.guard();
    return { yaml: this.stored(id).yaml, content_hash: "0".repeat(64) };
  }

  async remove(id: string): Promise<string> {
    this.guard();
    const stored = this.stored(id);
    if (stored.importedBy !== this.who() && !this.isAdmin()) {
      fail(403, {
        whatHappened: `You can't delete ${stored.parsed.name}.`,
        likelyCause: `It was imported by ${stored.importedBy}; only they or an administrator may delete it.`,
        whatToDo: "Ask them, or switch it off for your agent instead.",
      });
    }
    this.library.delete(id);
    const agents = stored.parsed.agents.map((a) => `the ${AGENT_LABEL[a]}`).join(", ");
    return `${stored.parsed.name} v${stored.parsed.version} was deleted; ${agents} can no longer use it. Tickets that already ran it keep their record.`;
  }
}
