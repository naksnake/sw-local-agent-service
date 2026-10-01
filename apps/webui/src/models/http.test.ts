import { describe, expect, it } from "vitest";

import { expectRequestedWith, fetchWith } from "../api/testing";
import { HttpModelsApi } from "./api";

const RECORD = {
  id: "f-0a1b2c3d",
  link: "https://huggingface.co/Qwen/Qwen3.8-27B-FP8",
  model_id: "qwen3.8-27b-fp8",
  repo: "Qwen/Qwen3.8-27B-FP8",
  revision: "main",
  display_name: "Qwen3.8-27B-FP8",
  state: "planning",
  bytes_done: 0,
  bytes_total: 0,
  files_done: 0,
  files_total: 0,
  sentence: "Asking the hub what Qwen/Qwen3.8-27B-FP8 contains…",
  started_at: "2026-09-18T09:00:00Z",
  finished_at: null,
  by: "pat@slas.local",
  problem: null,
  entry: null,
};

describe("HttpModelsApi (contract §3, §3b, §8 /api/v1/models)", () => {
  it("reads the registry and the downloads on their paths", async () => {
    const fake = fetchWith((call) => (call.url.endsWith("/models") ? { sentence: "", models: [], roles: {}, voters: [], problem: null } : [RECORD]));
    const api = new HttpModelsApi(fake.http);
    await api.registry();
    const records = await api.fetches();
    expect(fake.calls.map((c) => [c.method, c.url])).toEqual([
      ["GET", "/api/v1/models"],
      ["GET", "/api/v1/models/fetches"],
    ]);
    expect(records).toEqual([RECORD]);
  });

  it("starts a download with the link and a null id when none was given", async () => {
    const fake = fetchWith(() => RECORD);
    const record = await new HttpModelsApi(fake.http).startFetch("https://huggingface.co/Qwen/Qwen3.8-27B-FP8", "  ");
    const call = fake.only();
    expect(call).toMatchObject({ method: "POST", url: "/api/v1/models/fetches", body: { link: "https://huggingface.co/Qwen/Qwen3.8-27B-FP8", id: null } });
    expectRequestedWith(call);
    expect(record.state).toBe("planning");
    const named = fetchWith(() => RECORD);
    await new HttpModelsApi(named.http).startFetch("Qwen/Qwen3.8-27B-FP8", "my-qwen");
    expect(named.only().body).toEqual({ link: "Qwen/Qwen3.8-27B-FP8", id: "my-qwen" });
  });

  it("cancels or removes by DELETE and returns the sentence", async () => {
    const fake = fetchWith(() => ({ sentence: "Removed the record of Qwen3.8-27B-FP8; the files stay." }));
    const sentence = await new HttpModelsApi(fake.http).cancelFetch("f-0a1b/2c");
    expect(fake.only()).toMatchObject({ method: "DELETE", url: "/api/v1/models/fetches/f-0a1b%2F2c" });
    expectRequestedWith(fake.only());
    expect(sentence).toBe("Removed the record of Qwen3.8-27B-FP8; the files stay.");
  });

  it("saves roles with PUT and only the keys that changed", async () => {
    const fake = fetchWith(() => ({ sentence: "Saved: coder → Qwen3.8-27B.", roles: { coder: "qwen3.8-27b-fp8" }, voters: [], models: [] }));
    const result = await new HttpModelsApi(fake.http).saveRoles({ roles: { coder: "qwen3.8-27b-fp8", rerank: null } });
    const call = fake.only();
    expect(call).toMatchObject({ method: "PUT", url: "/api/v1/models/roles", body: { roles: { coder: "qwen3.8-27b-fp8", rerank: null } } });
    expectRequestedWith(call);
    expect(result.sentence).toBe("Saved: coder → Qwen3.8-27B.");
  });
});
