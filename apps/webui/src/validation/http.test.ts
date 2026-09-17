import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { expectRequestedWith, fetchWith, threePart } from "../api/testing";
import { agents } from "../copy/en";
import type { SuiteView } from "./api";
import { findingFromWire, HttpValidationApi } from "./http";

const SUITE_WIRE = {
  source: "suite.md",
  title: "GX8 DC cycling",
  items: [
    { n: 1, title: "DC cycle", action: "dc_cycle", params: { settle_s: "60" }, cycles: 25, destructive: false, approved: false, sentence: "1. DC cycle ×25 (settle_s 60)" },
    { n: 2, title: "AC cycle", action: "ac_cycle", params: {}, cycles: 2, destructive: true, approved: true, sentence: "2. AC cycle ×2" },
    { n: 3, title: "Read SEL", action: "sel_snapshot", cycles: 1, destructive: false, sentence: "3. Read SEL" },
  ],
  sentence: "GX8 DC cycling: 3 items, 28 cycles in total.",
  problem: null,
};

const RUN_WIRE = {
  ticket_id: "T-validation-0001",
  title: "GX8 DC cycling",
  target: "lab-gx8-01",
  state: "Planned",
  sentence: "T-validation-0001 waits for your approval of 2 steps before anything touches lab-gx8-01.",
  pending_approvals: ["AC cycle 1 of 2", "AC cycle 2 of 2"],
  cells: [
    { n: 1, kind: "dc", status: "ok", sentence: "Cycle 1 (DC): booted; no change against the baseline." },
    { n: 2, kind: "dc", status: "finding", sentence: "Cycle 2 (DC): booted; 1 change." },
    { n: 3, kind: "ac", status: "mystery" },
  ],
  console_tail: ["--- slas fence T-validation-0001 cycle 1 dc ---"],
  findings: ["[Issue] PCIe link width changed on GPU3 (0000:8a:00.0) during DC cycle 2 | [Owner] EE | T-validation-0002", "Fan 3 reads 0 rpm"],
  votes: ["3 of 3 voters agree the plan stays within the guardrails."],
};

describe("HttpValidationApi (contract §5 Validation, §8)", () => {
  it("parses a .md suite as text and maps items, keeping what it must send back", async () => {
    const fake = fetchWith(() => SUITE_WIRE);
    const suite = await new HttpValidationApi(fake.http).parseSuite("# GX8 DC cycling\n- DC cycle x25", "suite.md");
    const call = fake.only();
    expect(call.method).toBe("POST");
    expect(call.url).toBe("/api/v1/validation/suites/parse");
    expect(call.body).toEqual({ filename: "suite.md", text: "# GX8 DC cycling\n- DC cycle x25" });
    expectRequestedWith(call);
    expect(suite.title).toBe("GX8 DC cycling");
    expect(suite.source).toBe("suite.md");
    expect(suite.problem).toBeNull();
    expect(suite.items[0]).toEqual({
      n: 1,
      title: "DC cycle",
      cycles: 25,
      destructive: false,
      approved: false,
      sentence: "1. DC cycle ×25 (settle_s 60)",
      action: "dc_cycle",
      params: { settle_s: "60" },
    });
    expect(suite.items[1]).toMatchObject({ destructive: true, approved: true });
    // No `approved` on the wire means the author did not flag it.
    expect(suite.items[2]).toMatchObject({ approved: false, action: "sel_snapshot" });
    expect(suite.items[2]).not.toHaveProperty("params");
  });

  it("sends a .xlsx as content_base64 and passes a problem sentence through", async () => {
    const fake = fetchWith(() => ({ source: "suite.xlsx", title: "suite", items: [], sentence: "", problem: "suite.xlsx has no Step column." }));
    const suite = await new HttpValidationApi(fake.http).parseSuite("UEsDBBQ=", "suite.xlsx");
    expect(fake.only().body).toEqual({ filename: "suite.xlsx", content_base64: "UEsDBBQ=" });
    expect(suite).toEqual({ title: "suite", items: [], problem: "suite.xlsx has no Step column.", source: "suite.xlsx", sentence: "" });
  });

  it("lists targets with holder null when free", async () => {
    const fake = fetchWith(() => [
      { ref: "lab-gx8-01", model: "SLAS-GX8", free: true, holder: null, armed: true, sentence: "lab-gx8-01 is free." },
      { ref: "lab-gx8-02", model: "SLAS-GX8", free: false, holder: "lab-gx8-02 is leased to T-validation-0007 (lee) until 08:00.", armed: false, sentence: "" },
    ]);
    const targets = await new HttpValidationApi(fake.http).listTargets();
    expect(fake.only()).toMatchObject({ method: "GET", url: "/api/v1/validation/targets" });
    expect(targets[0]).toEqual({ ref: "lab-gx8-01", model: "SLAS-GX8", free: true, holder: null, armed: true, sentence: "lab-gx8-01 is free." });
    expect(targets[1]?.holder).toContain("leased to T-validation-0007");
  });

  it("previews with the suite exactly as received, and reads the cross-check sentence", async () => {
    const parse = fetchWith(() => SUITE_WIRE);
    const suite = await new HttpValidationApi(parse.http).parseSuite("x", "suite.md");
    const fake = fetchWith(() => ({
      sentence: "GX8 DC cycling on lab-gx8-01: 27 power cycles and 3 suite items, 33 steps.",
      steps: Array.from({ length: 33 }, (_, i) => ({ id: `s${i}`, title: `Step ${i}`, destructive: i < 2 })),
      destructive_steps: ["AC cycle 1 of 2", "AC cycle 2 of 2"],
      guardrails: ["At most 100 power cycles per run."],
      cross_check: { decision: "plan_approval", rule: "unanimous", votes: [], agreed: true, sentence: "3 of 3 voters agree the plan stays within the guardrails." },
    }));
    const preview = await new HttpValidationApi(fake.http).preview(suite, "lab-gx8-01");
    const call = fake.only();
    expect(call.url).toBe("/api/v1/validation/preview");
    expect(call.body).toEqual({ suite: SUITE_WIRE_ROUNDTRIP(suite), target: "lab-gx8-01" });
    expect(preview).toEqual({
      sentence: "GX8 DC cycling on lab-gx8-01: 27 power cycles and 3 suite items, 33 steps.",
      stepCount: 33,
      cycleCount: 27,
      destructive: ["AC cycle 1 of 2", "AC cycle 2 of 2"],
      guardrails: ["At most 100 power cycles per run."],
      crossCheck: "3 of 3 voters agree the plan stays within the guardrails.",
    });

    const none = fetchWith(() => ({ sentence: "s", steps: [], destructive_steps: [], guardrails: [], cross_check: null }));
    expect((await new HttpValidationApi(none.http).preview(suite, "lab-gx8-01")).crossCheck).toBe(agents.notCrossChecked);
  });

  it("starts, approves and lists runs, mapping cells, console, findings and pending approvals", async () => {
    const fake = fetchWith((call) => (call.url.endsWith("/runs") && call.method === "GET" ? [RUN_WIRE] : RUN_WIRE));
    const api = new HttpValidationApi(fake.http);
    const suite: SuiteView = { title: "t", items: [], problem: null };
    const run = await api.start(suite, "lab-gx8-01");
    expect(fake.calls[0]).toMatchObject({ method: "POST", url: "/api/v1/validation/runs" });
    expect(fake.calls[0]?.body).toEqual({ suite: { source: "", title: "t", items: [], sentence: "", problem: null }, target: "lab-gx8-01" });
    expect(run).toEqual({
      ticketId: "T-validation-0001",
      title: "GX8 DC cycling",
      target: "lab-gx8-01",
      state: "Planned",
      sentence: "T-validation-0001 waits for your approval of 2 steps before anything touches lab-gx8-01.",
      cycles: [
        { n: 1, kind: "dc", status: "ok", sentence: "Cycle 1 (DC): booted; no change against the baseline." },
        { n: 2, kind: "dc", status: "finding", sentence: "Cycle 2 (DC): booted; 1 change." },
        { n: 3, kind: "ac", status: "waiting", sentence: "" },
      ],
      console: ["--- slas fence T-validation-0001 cycle 1 dc ---"],
      findings: [
        { sentence: "PCIe link width changed on GPU3 (0000:8a:00.0) during DC cycle 2", owner: "EE", ticketId: "T-validation-0002" },
        { sentence: "Fan 3 reads 0 rpm", owner: "", ticketId: "" },
      ],
      approvalsPending: ["AC cycle 1 of 2", "AC cycle 2 of 2"],
      votes: ["3 of 3 voters agree the plan stays within the guardrails."],
    });

    await api.approve("T-validation-0001", "lee");
    expect(fake.calls[1]).toMatchObject({ method: "POST", url: "/api/v1/validation/runs/T-validation-0001/approve", body: {} });
    expectRequestedWith(fake.calls[1] as never);

    expect((await api.listRuns()).map((r) => r.ticketId)).toEqual(["T-validation-0001"]);
    expect(fake.calls[2]).toMatchObject({ method: "GET", url: "/api/v1/validation/runs" });
    expectRequestedWith(fake.calls[2] as never);
    expect((await api.getRun("T-validation-0001")).ticketId).toBe("T-validation-0001");
    expect(fake.calls[3]?.url).toBe("/api/v1/validation/runs/T-validation-0001");
  });

  it("reads findings from either the sentence form or a record", () => {
    expect(findingFromWire("[Issue] X | [Owner] TE")).toEqual({ sentence: "X", owner: "TE", ticketId: "" });
    expect(findingFromWire({ sentence: "Y", owner: "EE", ticket_id: "T-validation-0009" })).toEqual({ sentence: "Y", owner: "EE", ticketId: "T-validation-0009" });
    expect(findingFromWire("Plain sentence mentioning T-validation-0003")).toEqual({ sentence: "Plain sentence mentioning T-validation-0003", owner: "", ticketId: "T-validation-0003" });
  });

  it("surfaces a guardrail refusal as an ApiError with the three parts", async () => {
    const fake = fetchWith(() => threePart(409, "The plan asks for 120 power cycles.", "The guardrail allows 100 per run.", "Split the suite."));
    const error = (await new HttpValidationApi(fake.http).preview({ title: "t", items: [], problem: null }, "lab").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.parts.whatToDo).toBe("Split the suite.");
  });
});

/** The suite as the client sends it back: the same fields, every optional one filled. */
function SUITE_WIRE_ROUNDTRIP(suite: SuiteView): unknown {
  return {
    source: "suite.md",
    title: suite.title,
    items: [
      { n: 1, title: "DC cycle", action: "dc_cycle", params: { settle_s: "60" }, cycles: 25, destructive: false, approved: false, sentence: "1. DC cycle ×25 (settle_s 60)" },
      { n: 2, title: "AC cycle", action: "ac_cycle", params: {}, cycles: 2, destructive: true, approved: true, sentence: "2. AC cycle ×2" },
      { n: 3, title: "Read SEL", action: "sel_snapshot", params: {}, cycles: 1, destructive: false, approved: false, sentence: "3. Read SEL" },
    ],
    sentence: "GX8 DC cycling: 3 items, 28 cycles in total.",
    problem: null,
  };
}
