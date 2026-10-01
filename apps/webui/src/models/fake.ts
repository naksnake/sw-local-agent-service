// The in-memory Models registry: the quickstart profile (config/models.quickstart.yaml) as
// the api would answer it, so the page can be reviewed without a backend. "Add a model"
// moves one quarter forward on every poll and lands as a new card; roles and voters save
// into the same view, with the model manager's refusals as three-part errors.

import { ApiError } from "../api/http";
import { unreachable } from "../session/fake";
import type { FetchRecordView, ModelsApi, ModelView, RegistryView, RolesChange, RolesResult, ThreePartWire } from "./api";
import { isRunning, ROLES } from "./api";

export const QUICKSTART_REGISTRY: RegistryView = {
  sentence:
    "4 models; coder → Qwen3.8-27B, planner → Qwen3.8-27B, triage → DeepSeek-V4 Flash, embed → BGE-M3, rerank → BGE Reranker v2 M3; 2 voters from 2 model families.",
  models: [
    {
      id: "deepseek-v4-flash",
      display_name: "DeepSeek-V4 Flash",
      family: "DeepSeek",
      path: "deepseek-v4-flash",
      quant: "fp8",
      vram_gib: 180,
      context: 131072,
      roles: ["triage", "planner"],
      present: true,
    },
    {
      id: "qwen3.8-27b-fp8",
      display_name: "Qwen3.8-27B",
      family: "Qwen",
      path: "qwen3.8-27b-fp8",
      quant: "fp8",
      vram_gib: 40,
      context: 131072,
      roles: ["coder", "planner"],
      present: true,
    },
    {
      id: "bge-m3",
      display_name: "BGE-M3",
      family: "BAAI",
      path: "bge-m3",
      quant: "bf16",
      vram_gib: 3,
      context: 8192,
      roles: ["embed"],
      present: true,
    },
    {
      id: "bge-reranker-v2-m3",
      display_name: "BGE Reranker v2 M3",
      family: "BAAI",
      path: "bge-reranker-v2-m3",
      quant: "bf16",
      vram_gib: 2,
      context: 8192,
      roles: ["rerank"],
      present: false,
    },
  ],
  roles: {
    coder: "qwen3.8-27b-fp8",
    planner: "qwen3.8-27b-fp8",
    triage: "deepseek-v4-flash",
    embed: "bge-m3",
    rerank: "bge-reranker-v2-m3",
  },
  voters: ["deepseek-v4-flash", "qwen3.8-27b-fp8"],
  problem: null,
};

const GIB = 1024 ** 3;
const ACCEPTED_FORMS =
  "https://huggingface.co/<owner>/<repo>, https://huggingface.co/<owner>/<repo>/tree/<revision>, https://huggingface.co/<owner>/<repo>/commit/<sha>, hf.co/<owner>/<repo>, <owner>/<repo>[@revision]";

const FAMILIES: Record<string, string> = {
  "deepseek-ai": "DeepSeek",
  qwen: "Qwen",
  baai: "BAAI",
  minimaxai: "MiniMax",
  "meta-llama": "Meta",
  mistralai: "Mistral",
};

function refuse(status: number, whatHappened: string, likelyCause: string, whatToDo: string): never {
  throw new ApiError({ status, parts: { whatHappened, likelyCause, whatToDo } });
}

function gib(bytes: number): string {
  return `${(bytes / GIB).toFixed(1)} GiB`;
}

/** The link forms the model fetcher accepts, reduced to owner and repository. */
export function parseFakeLink(link: string): { owner: string; repo: string } {
  const text = link.trim();
  const bare = /^([A-Za-z0-9][\w.-]*)\/([A-Za-z0-9][\w.-]*)(?:@[^\s@]+)?$/.exec(text);
  if (bare !== null && !bare[1]!.includes(".")) {
    return { owner: bare[1]!, repo: bare[2]! };
  }
  const url = /^(?:https?:\/\/)?(?:huggingface\.co|hf\.co)\/([A-Za-z0-9][\w.-]*)\/([A-Za-z0-9][\w.-]*)(?:\/(?:tree|commit)\/[^\s/]+)?\/?$/.exec(text);
  if (url !== null) {
    return { owner: url[1]!, repo: url[2]! };
  }
  return refuse(
    400,
    `The link '${text}' is not one the model fetcher accepts.`,
    "It is neither a hub address nor a bare owner/repo id.",
    `Paste one of: ${ACCEPTED_FORMS}.`,
  );
}

export class FakeModelsApi implements ModelsApi {
  down = false;
  view: RegistryView;
  records: FetchRecordView[] = [];
  /** How many polls a fake download takes from start to done. */
  pollsToFinish = 4;
  private counter = 0;

  constructor(view: RegistryView = QUICKSTART_REGISTRY) {
    this.view = structuredClone(view);
  }

  async registry(): Promise<RegistryView> {
    if (this.down) {
      unreachable();
    }
    return structuredClone(this.view);
  }

  /** The registry file is missing or invalid: no models, a three-part problem. */
  breakRegistry(problem: ThreePartWire): void {
    this.view = { sentence: "", models: [], roles: {}, voters: [], problem };
  }

  // --- Add a model -----------------------------------------------------------------------------

  async fetches(): Promise<FetchRecordView[]> {
    if (this.down) {
      unreachable();
    }
    for (const record of this.records) {
      this.advance(record);
    }
    return structuredClone(this.records);
  }

  async startFetch(link: string, id?: string): Promise<FetchRecordView> {
    if (this.down) {
      unreachable();
    }
    const { owner, repo } = parseFakeLink(link);
    const wanted = id?.trim() ?? "";
    if (wanted !== "" && !/^[a-z0-9][a-z0-9._-]*$/.test(wanted)) {
      refuse(400, `'${wanted}' is not a registry id.`, "An id is lowercase letters, digits, dots, dashes and underscores.", "Use something like qwen3.8-27b-fp8, or leave the field empty.");
    }
    const taken = new Set([...this.view.models.map((m) => m.id), ...this.records.filter(isRunning).map((r) => r.model_id)]);
    let modelId = wanted !== "" ? wanted : repo.toLowerCase().replace(/_/g, "-");
    if (this.records.some((r) => r.model_id === modelId && isRunning(r))) {
      refuse(409, `${modelId} is being fetched already.`, "Another fetch of this model is still running.", "Watch that fetch on the Models page, or cancel it first.");
    }
    if (wanted !== "" && taken.has(modelId)) {
      refuse(409, `${modelId} is already in the model registry.`, "A model with this id was registered earlier.", "Give the new model another registry id, or remove the old entry from Models/models.yaml first.");
    }
    // As the fetcher does: the default id is made unique against the registry (`-2`, `-3`…).
    for (let n = 2; taken.has(modelId); n += 1) {
      modelId = `${repo.toLowerCase().replace(/_/g, "-")}-${n}`;
    }
    this.counter += 1;
    const record: FetchRecordView = {
      id: `f-${this.counter.toString(16).padStart(8, "0")}`,
      link: link.trim(),
      model_id: modelId,
      repo: `${owner}/${repo}`,
      revision: "main",
      display_name: repo,
      state: "planning",
      bytes_done: 0,
      bytes_total: 0,
      files_done: 0,
      files_total: 0,
      sentence: `Asking the hub what ${owner}/${repo} contains…`,
      started_at: new Date().toISOString(),
      finished_at: null,
      by: "pat@slas.local",
      problem: null,
      entry: null,
    };
    this.records.unshift(record);
    return structuredClone(record);
  }

  async cancelFetch(id: string): Promise<string> {
    if (this.down) {
      unreachable();
    }
    const record = this.records.find((r) => r.id === id);
    if (record === undefined) {
      refuse(404, `There is no fetch called ${id}.`, "It was removed.", "Reload the Models page.");
    }
    if (isRunning(record)) {
      record.state = "cancelled";
      record.finished_at = new Date().toISOString();
      record.sentence = `The fetch of ${record.display_name} was cancelled after ${gib(record.bytes_done)} of ${gib(record.bytes_total)}; the files stay, and fetching the same link again resumes.`;
      return `The fetch of ${record.display_name} stops after the current file.`;
    }
    this.records = this.records.filter((r) => r.id !== id);
    return `Removed the record of ${record.display_name}; the files stay.`;
  }

  /** One poll's worth of progress: plan → four quarters → import → done, then the card. */
  private advance(record: FetchRecordView): void {
    if (!isRunning(record)) {
      return;
    }
    if (record.state === "planning") {
      record.state = "downloading";
      record.bytes_total = 29 * GIB;
      record.files_total = 9;
      record.sentence = `Downloading ${record.display_name}: 0 B of ${gib(record.bytes_total)}, 0 of 9 files.`;
      return;
    }
    if (record.state === "downloading") {
      const step = Math.ceil(record.bytes_total / this.pollsToFinish);
      record.bytes_done = Math.min(record.bytes_total, record.bytes_done + step);
      record.files_done = Math.min(record.files_total, Math.floor((record.bytes_done / record.bytes_total) * record.files_total));
      if (record.bytes_done < record.bytes_total) {
        record.sentence = `Downloading ${record.display_name}: ${gib(record.bytes_done)} of ${gib(record.bytes_total)}, ${record.files_done} of ${record.files_total} files.`;
        return;
      }
      record.state = "importing";
      record.files_done = record.files_total;
      record.sentence = `Verified ${record.display_name} (${gib(record.bytes_total)}, ${record.files_total} files); registering it as ${record.model_id}…`;
      return;
    }
    // importing → done: the entry lands in the registry with estimates.
    const [owner] = record.repo.split("/");
    const family = FAMILIES[(owner ?? "").toLowerCase()] ?? owner ?? "unknown";
    const name = record.display_name.toLowerCase();
    const quant = name.includes("fp8") ? "fp8" : name.includes("awq") ? "awq4" : "bf16";
    const vram = Math.ceil((record.bytes_total / GIB) * 1.25);
    const model: ModelView = {
      id: record.model_id,
      display_name: record.display_name,
      family,
      path: record.model_id,
      quant,
      vram_gib: vram,
      context: 32768,
      roles: [],
      present: true,
    };
    this.view.models.push(model);
    this.view.sentence = this.sentenceOf();
    record.state = "done";
    record.finished_at = new Date().toISOString();
    record.entry = { ...model };
    record.sentence = `${record.display_name} is here (${gib(record.bytes_total)}, ${record.files_total} files) and registered as ${record.model_id}; give it a role on this page to start it. Its GPU memory is estimated at ${vram} GiB from the file sizes; correct it in the registry if you know better.`;
  }

  // --- roles and voters -------------------------------------------------------------------------

  async saveRoles(change: RolesChange): Promise<RolesResult> {
    if (this.down) {
      unreachable();
    }
    if (change.roles === undefined && change.voters === undefined) {
      refuse(400, "Nothing to change.", "The request named neither a role nor the voters.", "Pick a model for a role or tick the voters, then save.");
    }
    const changed: string[] = [];
    const roles = { ...this.view.roles };
    for (const [role, modelId] of Object.entries(change.roles ?? {})) {
      if (!(ROLES as readonly string[]).includes(role)) {
        refuse(400, `${role} is not a role.`, `The roles are ${ROLES.join(", ")}.`, "Pick a role from the Models page.");
      }
      if (modelId === null) {
        if (role in roles) {
          delete roles[role];
          changed.push(`${role} is no longer served`);
        }
        continue;
      }
      const model = this.present(modelId);
      roles[role] = modelId;
      if (!model.roles.includes(role)) {
        model.roles.push(role);
      }
      changed.push(`${role} → ${model.display_name}`);
    }
    let voters = this.view.voters;
    if (change.voters !== undefined) {
      if (new Set(change.voters).size !== change.voters.length) {
        refuse(400, "A model is listed twice among the voters.", "Voters are distinct models, from different families where possible.", "Tick each model once.");
      }
      const names = change.voters.map((v) => this.present(v).display_name);
      voters = [...change.voters];
      changed.push(names.length === 0 ? "no voters (cross-checks are flagged)" : `voters: ${names.join(", ")}`);
    }
    this.view.roles = roles;
    this.view.voters = voters;
    this.view.sentence = this.sentenceOf();
    const families = new Set(voters.map((v) => this.view.models.find((m) => m.id === v)?.family ?? v)).size;
    return {
      sentence: `Saved: ${changed.join("; ") || "nothing changed"}. ${voters.length} ${voters.length === 1 ? "voter" : "voters"} from ${families} ${families === 1 ? "family" : "families"}. To match the registry: start what changed.`,
      roles: { ...roles },
      voters: [...voters],
      models: this.view.models.map((m) => ({ id: m.id, display_name: m.display_name, family: m.family, quant: m.quant, roles: [...m.roles], present: m.present })),
    };
  }

  private present(modelId: string): ModelView {
    const model = this.view.models.find((m) => m.id === modelId);
    if (model === undefined) {
      return refuse(400, `There is no model called ${modelId} in the registry.`, "The id is mistyped, or the entry was removed from Models/models.yaml.", "Pick a model from the Models page.");
    }
    if (!model.present) {
      return refuse(
        400,
        `The weights of ${model.display_name} are not here yet.`,
        `Models/${model.path}/SHA256SUMS does not exist, so an instance could not start.`,
        "Add the model from this page (or run ./install.sh --models) first, then assign it.",
      );
    }
    return model;
  }

  private sentenceOf(): string {
    const nameOf = (id: string) => this.view.models.find((m) => m.id === id)?.display_name ?? id;
    const served = Object.entries(this.view.roles)
      .map(([role, id]) => `${role} → ${nameOf(id)}`)
      .join(", ");
    const families = new Set(this.view.voters.map((v) => this.view.models.find((m) => m.id === v)?.family ?? v)).size;
    return `${this.view.models.length} models; ${served || "no roles assigned"}; ${this.view.voters.length} voters from ${families} model ${families === 1 ? "family" : "families"}.`;
  }
}
