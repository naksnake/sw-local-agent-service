import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { expectRequestedWith, fetchWith, threePart } from "../api/testing";
import { HttpFactoryApi, HttpStationsAdminApi } from "./http";

const JOB_WIRE = {
  ticket_id: "T-factory-0001",
  title: "Final test of SN-GX8-0100 on station-07",
  station: "station-07",
  unit_sn: "SN-GX8-0100",
  state: "Running",
  sentence: "3 of 10 steps done.",
  cells: [
    { n: 1, title: "Lease the station and bind the unit", status: "ok", sentence: "Lease the station and bind the unit: done.", screenshot: null },
    { n: 3, title: "Log in to the station and start BurnIn", status: "running", sentence: "", screenshot: "Factory/Jobs/T-factory-0001/screens/003-before.png" },
    { n: 4, title: "Wait", status: "odd", sentence: "", screenshots: ["a.png", "b.png"] },
  ],
  verdict: null,
  votes: [],
  held: false,
};

const RECORD_WIRE = {
  name: "station-07",
  description: "Final test, line 2",
  allowed_programs: ["fixture-ctl", "burnin-ctl"],
  state_files: [],
  versions_command: [],
  screen: { window_match: "prefix", action_settle_s: 0.3, wait_timeout_scale: 1.5, max_actions_per_second: 8, poll_interval_s: 0.5 },
  retention: { keep_days: 30, keep_failed_days: 180, max_per_job: 400 },
  vnc: { enabled: true, port: 5901, command: ["x11vnc"] },
  runner_url: "https://station-07.factory.internal:8443",
  enrolled_at: "2026-09-14T10:00:00Z",
  cert_fingerprint: "SHA256:3F2A…9C1D",
  batch_key_id: "bk-1",
  last_seen: null,
  sentence: "station-07: enrolled 2026-09-14 10:00, runner at https://station-07.factory.internal:8443, certificate SHA256:3F2A…9C1D.",
};

describe("HttpFactoryApi (contract §5 Factory, §8)", () => {
  it("lists MES tickets, stations and templates with GETs, reading step titles from either shape", async () => {
    const fake = fetchWith((call) => {
      if (call.url.endsWith("/mes-tickets")) {
        return [{ ticket_no: "MES-88131", station: "station-07", unit_sn: "SN-GX8-0100", requested_by: "mes", payload: {} }];
      }
      if (call.url.endsWith("/stations")) {
        return [
          { name: "station-07", description: "Final test, line 2", free: true, holder: null, enrolled: true, sentence: "station-07 is free." },
          { name: "station-08", description: "", free: false, holder: "station-08 is leased to T-factory-0007 (mes).", enrolled: false, sentence: "" },
        ];
      }
      return [
        { id: "final-test-9-steps", name: "Final test, 9 steps", steps: ["Lease the station", "Power on"], skills: ["station-login-burnin"], sentence: "Final test, 9 steps: 2 steps." },
        { id: "dump", name: "Dump", description: "From a template dump.", steps: [{ id: "a", primitive: "run", title: "Run a" }], skills: [] },
      ];
    });
    const api = new HttpFactoryApi(fake.http);
    expect(await api.listMesTickets()).toEqual([{ ticketNo: "MES-88131", station: "station-07", unitSn: "SN-GX8-0100", requestedBy: "mes" }]);
    const stations = await api.listStations();
    expect(stations[0]).toEqual({ name: "station-07", free: true, holder: null, description: "Final test, line 2", enrolled: true, sentence: "station-07 is free." });
    expect(stations[1]?.holder).toContain("leased to T-factory-0007");
    const templates = await api.listTemplates();
    expect(templates[0]).toEqual({ id: "final-test-9-steps", name: "Final test, 9 steps", description: "Final test, 9 steps: 2 steps.", steps: ["Lease the station", "Power on"], skills: ["station-login-burnin"] });
    expect(templates[1]).toMatchObject({ description: "From a template dump.", steps: ["Run a"] });
    expect(fake.calls.map((c) => [c.method, c.url])).toEqual([
      ["GET", "/api/v1/factory/mes-tickets"],
      ["GET", "/api/v1/factory/stations"],
      ["GET", "/api/v1/factory/templates"],
    ]);
  });

  it("parses a label into a trigger or returns the problem sentence", async () => {
    const good = fetchWith(() => ({ trigger: { ticket_no: "manual-sn-gx8-0300", station: "station-08", unit_sn: "SN-GX8-0300", requested_by: "label" } }));
    expect(await new HttpFactoryApi(good.http).parseLabel("SN SN-GX8-0300 station station-08")).toEqual({
      kind: "label",
      ticketNo: "manual-sn-gx8-0300",
      station: "station-08",
      unitSn: "SN-GX8-0300",
      requestedBy: "label",
    });
    expect(good.only()).toMatchObject({ method: "POST", url: "/api/v1/factory/labels/parse", body: { text: "SN SN-GX8-0300 station station-08" } });
    expectRequestedWith(good.only());
    const bad = fetchWith(() => ({ problem: "The label names no unit and station." }));
    expect(await new HttpFactoryApi(bad.http).parseLabel("garbage")).toBe("The label names no unit and station.");
  });

  it("starts a job with the MesTicket, template and rules on the wire and maps the JobView", async () => {
    const fake = fetchWith(() => JOB_WIRE);
    const job = await new HttpFactoryApi(fake.http).start(
      { kind: "mes", ticketNo: "MES-88131", station: "station-07", unitSn: "SN-GX8-0100", requestedBy: "mes" },
      "final-test-9-steps",
      { voters: 3, onFail: "hold_station", exportSop: true, backupStation: false },
    );
    const call = fake.only();
    expect(call).toMatchObject({ method: "POST", url: "/api/v1/factory/jobs" });
    expectRequestedWith(call);
    expect(call.body).toEqual({
      trigger: { ticket_no: "MES-88131", station: "station-07", unit_sn: "SN-GX8-0100", requested_by: "mes" },
      template_id: "final-test-9-steps",
      rules: { voters: 3, on_fail: "hold", export_sop: true, backup_station: false },
    });
    expect(job).toEqual({
      ticketId: "T-factory-0001",
      title: "Final test of SN-GX8-0100 on station-07",
      station: "station-07",
      unitSn: "SN-GX8-0100",
      mesTicketNo: "",
      state: "Running",
      sentence: "3 of 10 steps done.",
      verdict: null,
      verdictSentence: "",
      held: false,
      decidedBy: "",
      steps: [
        { n: 1, title: "Lease the station and bind the unit", status: "ok", sentence: "Lease the station and bind the unit: done.", screenshots: [] },
        { n: 3, title: "Log in to the station and start BurnIn", status: "running", sentence: "", screenshots: ["Factory/Jobs/T-factory-0001/screens/003-before.png"] },
        { n: 4, title: "Wait", status: "waiting", sentence: "", screenshots: ["a.png", "b.png"] },
      ],
      draftTicketId: null,
      backupPath: null,
      votes: [],
    });
  });

  it("names the requester for a label or manual trigger and reads the extra JobState fields when sent", async () => {
    const fake = fetchWith(() => ({
      ...JOB_WIRE,
      state: "Needs review",
      mes_ticket_no: "manual-sn-1",
      verdict: "FAIL",
      verdict_sentence: "FAIL: BurnIn reported FAIL (gpu-memory).",
      votes: ["2 of 3 voters say FAIL."],
      held: true,
      decided_by: "the deterministic gate",
      draft_ticket_id: "T-factory-0002",
      backup_path: "Backups/stations/station-07/T-factory-0001",
    }));
    const job = await new HttpFactoryApi(fake.http).start(
      { kind: "manual", ticketNo: "manual-sn-1", station: "station-07", unitSn: "SN-1" },
      "final-test-9-steps",
      { voters: 3, onFail: "hold_station", exportSop: true, backupStation: true },
    );
    expect((fake.only().body as { trigger: { requested_by: string } }).trigger.requested_by).toBe("manual");
    expect(job).toMatchObject({
      mesTicketNo: "manual-sn-1",
      verdict: "FAIL",
      verdictSentence: "FAIL: BurnIn reported FAIL (gpu-memory).",
      votes: ["2 of 3 voters say FAIL."],
      held: true,
      decidedBy: "the deterministic gate",
      draftTicketId: "T-factory-0002",
      backupPath: "Backups/stations/station-07/T-factory-0001",
    });
  });

  it("decides, controls, lists and reads jobs on the contract's paths", async () => {
    const fake = fetchWith((call) => {
      if (call.url.endsWith("/control")) {
        return { sentence: "The runner drives station-07.", watch_url: "vnc://127.0.0.1:5901", watch_problem: null };
      }
      return call.method === "GET" && call.url.endsWith("/jobs") ? [JOB_WIRE] : JOB_WIRE;
    });
    const api = new HttpFactoryApi(fake.http);
    await api.decide("T-factory-0001", "PASS", "lee", "Reseated the riser.");
    expect(fake.calls[0]).toMatchObject({ method: "POST", url: "/api/v1/factory/jobs/T-factory-0001/decide", body: { verdict: "PASS", note: "Reseated the riser." } });
    expectRequestedWith(fake.calls[0] as never);

    const watching = await api.control("T-factory-0001", "status", "lee");
    expect(fake.calls[1]).toMatchObject({ method: "POST", url: "/api/v1/factory/jobs/T-factory-0001/control", body: { verb: "status" } });
    expect(watching).toEqual({ station: "", paused: false, aborted: false, by: "", sentence: "The runner drives station-07.", watchUrl: "vnc://127.0.0.1:5901", watchProblem: null });
    const paused = await api.control("T-factory-0001", "pause", "lee");
    expect(paused).toMatchObject({ paused: true, aborted: false, by: "lee" });
    const aborted = await api.control("T-factory-0001", "abort", "lee");
    expect(aborted).toMatchObject({ paused: false, aborted: true, by: "lee" });

    expect((await api.listJobs()).map((j) => j.ticketId)).toEqual(["T-factory-0001"]);
    expect(fake.calls[4]).toMatchObject({ method: "GET", url: "/api/v1/factory/jobs" });
    expect((await api.getJob("T-factory-0001")).ticketId).toBe("T-factory-0001");
    expect(fake.calls[5]?.url).toBe("/api/v1/factory/jobs/T-factory-0001");
  });

  it("lets a three-part refusal through as an ApiError", async () => {
    const fake = fetchWith(() => threePart(403, "You can't decide a verdict.", "Deciding needs the line lead role.", "Ask the line lead."));
    const error = (await new HttpFactoryApi(fake.http).decide("T-factory-0001", "PASS", "pat", "").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.parts.whatHappened).toBe("You can't decide a verdict.");
  });
});

describe("HttpStationsAdminApi (contract §6 station records, §8 /api/v1/stations)", () => {
  it("lists station records, deriving enrolled from the record and reading vnc, screen and retention", async () => {
    const fake = fetchWith(() => [RECORD_WIRE, { name: "station-08", sentence: "station-08: not enrolled yet." }]);
    const [enrolled, bare] = await new HttpStationsAdminApi(fake.http).listStationRecords();
    expect(fake.only()).toMatchObject({ method: "GET", url: "/api/v1/stations" });
    expect(enrolled).toEqual({
      name: "station-07",
      description: "Final test, line 2",
      allowedPrograms: ["fixture-ctl", "burnin-ctl"],
      enrolled: true,
      sentence: RECORD_WIRE.sentence,
      runnerUrl: "https://station-07.factory.internal:8443",
      certFingerprint: "SHA256:3F2A…9C1D",
      vncEnabled: true,
      vncPort: 5901,
      screen: { windowMatch: "prefix", actionSettleS: 0.3, waitTimeoutScale: 1.5, maxActionsPerSecond: 8 },
      retention: { keepDays: 30, keepFailedDays: 180, maxPerJob: 400 },
    });
    expect(bare).toEqual({
      name: "station-08",
      description: "",
      allowedPrograms: [],
      enrolled: false,
      sentence: "station-08: not enrolled yet.",
      runnerUrl: null,
      certFingerprint: null,
      vncEnabled: false,
      vncPort: 5900,
      screen: { windowMatch: "contains", actionSettleS: 0, waitTimeoutScale: 1, maxActionsPerSecond: 10 },
      retention: { keepDays: 30, keepFailedDays: 180, maxPerJob: 400 },
    });
  });

  it("adds a station, returning the api's refusal as one sentence for the page", async () => {
    const ok = fetchWith(() => ({ ...RECORD_WIRE, name: "station-09", enrolled_at: null, runner_url: null, sentence: "station-09: not enrolled yet." }));
    const record = await new HttpStationsAdminApi(ok.http).addStation("station-09", "Line 3");
    expect(ok.only()).toMatchObject({ method: "POST", url: "/api/v1/stations", body: { name: "station-09", description: "Line 3" } });
    expectRequestedWith(ok.only());
    expect(record).toMatchObject({ name: "station-09", enrolled: false });

    const refused = fetchWith(() => threePart(409, "There is already a station called station-07.", "Every station has one record.", "Pick another name."));
    expect(await new HttpStationsAdminApi(refused.http).addStation("station-07", "")).toBe(
      "There is already a station called station-07. Every station has one record. Pick another name.",
    );
  });

  it("issues a code, revokes, removes and saves tuning on the contract's paths", async () => {
    const fake = fetchWith((call) => {
      if (call.url.endsWith("/code")) {
        return { station: "station-07", code: "ABCD-EFGH-JKLM", expires_at: "2026-09-14T10:15:00Z", sentence: "Enter this code on station-07 within 15 minutes: ABCD-EFGH-JKLM." };
      }
      if (call.url.endsWith("/revoke")) {
        return { ...RECORD_WIRE, enrolled_at: null, runner_url: null, cert_fingerprint: null, sentence: "station-07: not enrolled yet." };
      }
      if (call.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (call.method === "PUT") {
        return { ok: true };
      }
      return [{ ...RECORD_WIRE, vnc: { enabled: false, port: 5900 } }];
    });
    const api = new HttpStationsAdminApi(fake.http);
    const issued = await api.issueCode("station-07", "admin");
    expect(fake.calls[0]).toMatchObject({ method: "POST", url: "/api/v1/stations/station-07/code", body: {} });
    expect(issued).toEqual({ station: "station-07", code: "ABCD-EFGH-JKLM", expiresAt: "2026-09-14T10:15:00Z", sentence: "Enter this code on station-07 within 15 minutes: ABCD-EFGH-JKLM." });

    const revoked = await api.revoke("station-07");
    expect(fake.calls[1]).toMatchObject({ method: "POST", url: "/api/v1/stations/station-07/revoke" });
    expect(revoked).toMatchObject({ enrolled: false, runnerUrl: null, certFingerprint: null });

    await api.removeStation("station-07");
    expect(fake.calls[2]).toMatchObject({ method: "DELETE", url: "/api/v1/stations/station-07" });
    expectRequestedWith(fake.calls[2] as never);

    // An answer that is not a record makes the client read the record back.
    const saved = await api.saveTuning(
      "station-07",
      { windowMatch: "exact", actionSettleS: 0.5, waitTimeoutScale: 2, maxActionsPerSecond: 5 },
      { keepDays: 10, keepFailedDays: 90, maxPerJob: 100 },
      false,
    );
    expect(fake.calls[3]).toMatchObject({
      method: "PUT",
      url: "/api/v1/stations/station-07/tuning",
      body: {
        screen: { window_match: "exact", action_settle_s: 0.5, wait_timeout_scale: 2, max_actions_per_second: 5 },
        retention: { keep_days: 10, keep_failed_days: 90, max_per_job: 100 },
        vnc_enabled: false,
      },
    });
    expectRequestedWith(fake.calls[3] as never);
    expect(fake.calls[4]).toMatchObject({ method: "GET", url: "/api/v1/stations" });
    expect(saved.vncEnabled).toBe(false);
  });
});
