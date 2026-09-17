// The in-memory Models registry: the quickstart profile (config/models.quickstart.yaml) as
// the api would answer it, so the page can be reviewed without a backend.

import { unreachable } from "../session/fake";
import type { ModelsApi, RegistryView, ThreePartWire } from "./api";

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

export class FakeModelsApi implements ModelsApi {
  down = false;
  view: RegistryView;

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
}
