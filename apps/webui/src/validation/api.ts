// The Validation page's view of the API (CLAUDE.md §9, §10.2). Types mirror the Python
// schemas in slas_orchestrator.validation and slas_validation_executor; the sentences the
// fake produces are the ones the compiler, the guardrails and the executor produce, so the
// UI copy is exercised end to end. `HttpValidationApi` (./http.ts) is what production uses;
// `FakeValidationApi` serves `pnpm dev`. Nothing here carries a credential: targets are opaque
// references, the vault holds the rest.

import { agents } from "../copy/en";

export interface SuiteItemView {
  n: number;
  title: string;
  cycles: number;
  /** Needs a per-run human approval (INV-7): AC cycle, firmware flash, erase, BIOS, RAID. */
  destructive: boolean;
  /** The suite author flagged the destructive step as approved; without it the plan is refused. */
  approved: boolean;
  sentence: string;
  /** The primitive the compiler chose (api only); carried back unchanged on preview and start. */
  action?: string;
  params?: Record<string, string>;
}

export interface SuiteView {
  title: string;
  items: SuiteItemView[];
  /** A three-part sentence when the suite could not be read; items is then empty. */
  problem: string | null;
  /** The file the api parsed and its own one-line summary (api only); carried back unchanged. */
  source?: string;
  sentence?: string;
}

export interface TargetView {
  ref: string;
  model: string;
  free: boolean;
  /** "lab-gx8-02 is leased to T-validation-0007 (lee) until …" when busy. */
  holder: string | null;
  /** Armed for destructive steps by an administrator (api only). */
  armed?: boolean;
  sentence?: string;
}

export interface PlanPreview {
  sentence: string;
  stepCount: number;
  cycleCount: number;
  /** Titles of the steps that will ask for approval when the run reaches them. */
  destructive: string[];
  guardrails: string[];
  /** The Consensus Router's sentence on the plan; input to your decision, never the decision. */
  crossCheck: string;
}

export type CycleStatus = "waiting" | "running" | "ok" | "finding" | "failed" | "skipped";

export interface CycleCellView {
  n: number;
  kind: string;
  status: CycleStatus;
  sentence: string;
}

export interface FindingView {
  sentence: string;
  /** Empty when the api has not routed the finding yet. */
  owner: string;
  /** The child bug ticket; empty when none has been spawned yet. */
  ticketId: string;
}

export interface RunView {
  ticketId: string;
  title: string;
  target: string;
  state: string;
  sentence: string;
  cycles: CycleCellView[];
  console: string[];
  findings: FindingView[];
  /** Step titles still waiting for a person; empty once the run may act. */
  approvalsPending: string[];
  /** The Consensus Router's sentences on the plan and the analysis (api only). */
  votes?: string[];
}

export interface ValidationApi {
  parseSuite(text: string, filename: string): Promise<SuiteView>;
  listTargets(): Promise<TargetView[]>;
  preview(suite: SuiteView, target: string): Promise<PlanPreview>;
  start(suite: SuiteView, target: string): Promise<RunView>;
  approve(ticketId: string, decidedBy: string): Promise<RunView>;
  listRuns(): Promise<RunView[]>;
}

export const STATUS_WORD: Record<CycleStatus, string> = {
  waiting: "waiting",
  running: "running",
  ok: "ok",
  finding: "finding",
  failed: "did not boot",
  skipped: "skipped",
};

// The same sentences as slas_validation_executor.guardrails.Guardrails.sentences().
export const GUARDRAIL_SENTENCES: readonly string[] = [
  "At most 100 power cycles per run.",
  "At least 10 s settle after a warm or DC cycle, 30 s after AC.",
  "A boot that takes longer than 15 minutes counts as failed.",
  "3 boot failures in a row abort the run.",
  "One run per target at a time.",
  "A run stops after 72 hours.",
  "Needs your approval every run: ac_cycle, firmware_flash, secure_erase, bios_reset, raid_reconfigure.",
];

// --- fake -------------------------------------------------------------------------------

const CYCLES = /(?:x|×)\s*(\d+)|(\d+)\s*(?:cycles?|times)/i;
const DESTRUCTIVE = /\bac\b|\bpdu\b|power (?:cord|cable)|flash|erase|sanitize|bios (?:reset|default)|\braid\b/i;
const APPROVED = /\bapproved\b/i;
const KNOWN = /cycl|reboot|baseline|\bsel\b|event log|inventory|lnksta|link|firmware|flash|erase|bios|raid|collect|logs|stress|burn|fio|nvqual|mft|smart|perftest|memtester|dcgmi|ipmitool/i;

function cleanTitle(text: string): string {
  return text
    .replace(CYCLES, "")
    .replace(/settle\s*\d+\s*s/i, "")
    .replace(APPROVED, "")
    .replace(/\s+/g, " ")
    .replace(/^[\s,;:-]+|[\s,;:-]+$/g, "");
}

function itemFrom(n: number, raw: string, approvedCell: string): SuiteItemView | null {
  const title = cleanTitle(raw);
  if (title === "") {
    return null;
  }
  const match = CYCLES.exec(raw);
  const cycles = match ? Number.parseInt(match[1] ?? match[2] ?? "1", 10) : 1;
  const destructive = DESTRUCTIVE.test(raw);
  const approved = APPROVED.test(raw) || /^(yes|y|true|approved|x|✓)$/i.test(approvedCell.trim());
  return {
    n,
    title,
    cycles,
    destructive,
    approved,
    sentence: `${n}. ${title}${cycles > 1 ? ` ×${cycles}` : ""}`,
  };
}

export class FakeValidationApi implements ValidationApi {
  targets: TargetView[] = [
    { ref: "lab-gx8-01", model: "SLAS-GX8", free: true, holder: null },
    {
      ref: "lab-gx8-02",
      model: "SLAS-GX8",
      free: false,
      holder: "lab-gx8-02 is leased to T-validation-0007 (lee) until 2026-09-17 08:00.",
    },
    { ref: "lab-gx4-01", model: "SLAS-GX4", free: true, holder: null },
  ];
  /** The fake plants a PCIe degradation at this cycle, like the P7 done-when. */
  plantedCycle = 14;
  readonly runs: RunView[] = [];
  private counter = 0;

  async parseSuite(text: string, filename: string): Promise<SuiteView> {
    const lines = text.split(/\r?\n/);
    const heading = lines.find((l) => l.startsWith("# "))?.slice(2).trim();
    const title = heading ?? filename.replace(/\.(md|xlsx)$/i, "").replace(/[-_]/g, " ");
    if (/\.xlsx$/i.test(filename)) {
      return { title, items: [], problem: agents.xlsxNotInFake(filename) };
    }
    if (text.trim() === "") {
      return {
        title,
        items: [],
        problem: `${filename} is empty. A suite needs a title and at least one item. Add the items and upload again.`,
      };
    }
    const items: SuiteItemView[] = [];
    const tableRows = lines.filter((l) => l.trim().startsWith("|"));
    if (tableRows.length > 0) {
      const rows = tableRows
        .map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()))
        .filter((r) => !r.every((c) => /^[-: ]*$/.test(c)));
      const header = (rows[0] ?? []).map((c) => c.toLowerCase());
      const col = (...names: string[]) => header.findIndex((h) => names.includes(h));
      const step = col("step", "title", "test", "item", "name");
      const action = col("action", "primitive", "do");
      const params = col("parameters", "params", "args", "settings");
      const cycles = col("cycles", "repeat", "count");
      const approved = col("approved", "approval", "ok to run");
      if (step < 0 && action < 0) {
        return {
          title,
          items: [],
          problem: `${filename} has a table without a Step or Action column. The suite table needs at least a Step column. Add the header row and try again.`,
        };
      }
      for (const row of rows.slice(1)) {
        const cell = (i: number) => (i >= 0 ? (row[i] ?? "") : "");
        const raw = [cell(step), cell(action), cell(params)].filter((c) => c !== "").join(" — ");
        const count = cell(cycles);
        const item = itemFrom(items.length + 1, count !== "" ? `${raw} x${count}` : raw, cell(approved));
        if (item) {
          item.title = cleanTitle(cell(step) || cell(action));
          item.sentence = `${item.n}. ${item.title}${item.cycles > 1 ? ` ×${item.cycles}` : ""}`;
          items.push(item);
        }
      }
    } else {
      for (const line of lines) {
        const match = /^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$/.exec(line);
        if (match?.[1]) {
          const item = itemFrom(items.length + 1, match[1], "");
          if (item) {
            items.push(item);
          }
        }
      }
    }
    if (items.length === 0) {
      return {
        title,
        items: [],
        problem: `${filename} has no items. Items are table rows under a Step/Action header, or bullets such as \`- DC cycle x25, settle 60 s\`. Add the items and upload again.`,
      };
    }
    const unknown = items.find((i) => !KNOWN.test(i.title));
    if (unknown) {
      return {
        title,
        items: [],
        problem: `Step ${unknown.n} (${unknown.title}) names no known action. A plan may only use the Validation primitives in plans/primitives/validation.yaml. Reword the step (for example \`DC cycle x25, settle 60 s\`), or remove it.`,
      };
    }
    return { title, items, problem: null };
  }

  async listTargets(): Promise<TargetView[]> {
    return this.targets.map((t) => ({ ...t }));
  }

  async preview(suite: SuiteView, target: string): Promise<PlanPreview> {
    const cycleCount = suite.items.filter((i) => /cycl|reboot/i.test(i.title)).reduce((n, i) => n + i.cycles, 0);
    const others = suite.items.filter((i) => !/cycl|reboot/i.test(i.title)).length;
    const stepCount = 3 + cycleCount + others + 2;
    const destructive = suite.items
      .filter((i) => i.destructive)
      .flatMap((i) =>
        /cycl/i.test(i.title)
          ? Array.from({ length: i.cycles }, (_, k) => `AC cycle ${k + 1} of ${cycleCount}`)
          : [i.title],
      );
    const tail =
      destructive.length === 0
        ? " Nothing destructive."
        : ` ${destructive.length} destructive ${destructive.length === 1 ? "step needs" : "steps need"} your approval before the run starts.`;
    return {
      sentence: `${suite.title} on ${target}: ${cycleCount} power cycles and ${suite.items.length} suite items, ${stepCount} steps.${tail}`,
      stepCount,
      cycleCount,
      destructive,
      guardrails: [...GUARDRAIL_SENTENCES],
      crossCheck: "3 of 3 voters agree the plan stays within the guardrails. Your approval starts it.",
    };
  }

  async start(suite: SuiteView, target: string): Promise<RunView> {
    this.counter += 1;
    const ticketId = `T-validation-${String(this.counter).padStart(4, "0")}`;
    const preview = await this.preview(suite, target);
    const kind = suite.items.some((i) => /\bac\b/i.test(i.title)) ? "ac" : /warm|reboot/i.test(suite.items[0]?.title ?? "") ? "warm" : "dc";
    const run: RunView = {
      ticketId,
      title: suite.title,
      target,
      state: "Planned",
      sentence: `${ticketId} waits for your approval of ${preview.destructive.length} ${preview.destructive.length === 1 ? "step" : "steps"} before anything touches ${target}.`,
      cycles: Array.from({ length: preview.cycleCount }, (_, k) => ({ n: k + 1, kind, status: "waiting" as CycleStatus, sentence: "" })),
      console: [],
      findings: [],
      approvalsPending: preview.destructive,
    };
    if (preview.destructive.length === 0) {
      this.complete(run);
    }
    this.runs.unshift(run);
    return run;
  }

  async approve(ticketId: string, decidedBy: string): Promise<RunView> {
    const run = this.runs.find((r) => r.ticketId === ticketId);
    if (!run) {
      throw new Error(`There is no run ${ticketId}.`);
    }
    run.approvalsPending = [];
    run.console.push(`--- approvals recorded by ${decidedBy} ---`);
    this.complete(run);
    return run;
  }

  /** The fake finishes a run at once, with the planted degradation from `plantedCycle` on. */
  private complete(run: RunView): void {
    const total = run.cycles.length;
    const gpu = "NVIDIA H100 SXM (0000:8a:00.0)";
    let findings = 0;
    run.cycles = run.cycles.map((cell) => {
      const kind = cell.kind.toUpperCase();
      run.console.push(`--- slas fence ${run.ticketId} cycle ${cell.n} ${cell.kind} ---`);
      run.console.push("[    0.000000] Linux version 6.8.0-slas (gcc 13.2.0) #1 SMP");
      if (cell.n >= this.plantedCycle && cell.kind !== "ac") {
        findings += 1;
        return {
          ...cell,
          status: "finding",
          sentence: `Cycle ${cell.n} (${kind}): booted; 1 change against the baseline during ${kind} cycle ${cell.n}: PCIe link width changed on ${gpu}: x16 → x8 during ${kind} cycle ${cell.n}.`,
        };
      }
      return { ...cell, status: "ok", sentence: `Cycle ${cell.n} (${kind}): booted; no change against the baseline.` };
    });
    if (findings > 0) {
      const first = run.cycles.find((c) => c.status === "finding");
      run.findings = [
        {
          sentence: `PCIe link width changed on ${gpu}: x16 → x8 during ${first?.kind.toUpperCase() ?? "DC"} cycle ${first?.n ?? this.plantedCycle}`,
          owner: "EE",
          ticketId: `T-validation-${String(this.counter + 1).padStart(4, "0")}`,
        },
      ];
      this.counter += 1;
      run.state = "Needs review";
      run.sentence = `${total} of ${total} cycles done: ${findings} with findings.`;
    } else {
      run.state = "Done";
      run.sentence = `${total} of ${total} cycles done.`;
    }
  }

  async listRuns(): Promise<RunView[]> {
    return [...this.runs];
  }
}
