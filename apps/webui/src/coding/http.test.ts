import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { expectRequestedWith, fetchWith, json, threePart } from "../api/testing";
import type { Breakdown } from "./api";
import { HttpCodingApi } from "./http";

const BREAKDOWN: Breakdown = {
  title: "Fan controller",
  tasks: [
    { n: 1, title: "Parse config.yaml" },
    { n: 2, title: "Add a pytest test" },
  ],
  languages: [
    { language: "python", version: "" },
    { language: "rust", version: "1.99" },
  ],
  isolation: "auto",
  skills: ["lint-and-test"],
  crossCheck: true,
  exportTarget: "remote",
  remoteRef: "gitlab-firmware",
  maxIterations: 6,
};

const TASK_WIRE = {
  ticket_id: "T-coding-0001",
  title: "Fan controller",
  state: "Running",
  sentence: "T-coding-0001 is running: step 2 of 7.",
  steps: [
    { n: 1, title: "Toolchain: Python 3.12.6.", status: "done" },
    { n: 2, title: "Open an isolated sandbox", status: "running" },
    { n: 3, title: "Task 1: Parse config.yaml", status: "not-a-status" },
  ],
  feed: ["Toolchain: Python 3.12.6.", "Opening an isolated sandbox…"],
};

describe("HttpCodingApi (contract §5 Coding, §8)", () => {
  it("detects languages with a POST and keeps only the ones the wizard knows", async () => {
    const fake = fetchWith(() => ({ languages: ["python", "cobol", "config"] }));
    const api = new HttpCodingApi(fake.http);
    expect(await api.detectLanguages("- add fan_ctl.py")).toEqual(["python", "config"]);
    const call = fake.only();
    expect(call.method).toBe("POST");
    expect(call.url).toBe("/api/v1/coding/languages/detect");
    expect(call.body).toEqual({ plan: "- add fan_ctl.py" });
    expectRequestedWith(call);
  });

  it("proposes a breakdown from the wire, mapping snake_case and nulls", async () => {
    const fake = fetchWith(() => ({
      title: "Fan controller",
      tasks: [{ n: 1, title: "Parse config.yaml" }],
      languages: [{ language: "python", version: null }, { language: "fortran", version: "77" }],
      isolation: "gvisor",
      skills: [],
      cross_check: false,
      export_target: "bundle",
      remote_ref: null,
      max_iterations: 4,
    }));
    const breakdown = await new HttpCodingApi(fake.http).propose("# Fan controller", "plan.md");
    expect(breakdown).toEqual({
      title: "Fan controller",
      tasks: [{ n: 1, title: "Parse config.yaml" }],
      languages: [{ language: "python", version: "" }],
      isolation: "gvisor",
      skills: [],
      crossCheck: false,
      exportTarget: "bundle",
      remoteRef: null,
      maxIterations: 4,
    });
    expect(fake.only().body).toEqual({ plan: "# Fan controller", filename: "plan.md" });
  });

  it("resolves toolchains, sending an empty version as null and dropping the image", async () => {
    const fake = fetchWith(() => [
      { language: "python", label: "Python", requested: null, version: "3.12.6", honoured: true, sentence: "Python: newest.", image: "slas/sandbox-python:3.12.6" },
      { language: "rust", label: "Rust", requested: "1.99", version: "1.80.1", honoured: false, sentence: "Rust 1.99 isn't in the bundle.", image: "x" },
    ]);
    const resolutions = await new HttpCodingApi(fake.http).resolveToolchains(BREAKDOWN.languages);
    expect(fake.only().body).toEqual({
      choices: [
        { language: "python", version: null },
        { language: "rust", version: "1.99" },
      ],
    });
    expect(resolutions).toEqual([
      { language: "python", label: "Python", requested: null, version: "3.12.6", honoured: true, sentence: "Python: newest." },
      { language: "rust", label: "Rust", requested: "1.99", version: "1.80.1", honoured: false, sentence: "Rust 1.99 isn't in the bundle." },
    ]);
  });

  it("lists remotes by name and skills by id and name with GETs", async () => {
    const fake = fetchWith((call) => (call.url.endsWith("/remotes") ? { remotes: ["gitlab-firmware"] } : { skills: [{ id: "lint-and-test", name: "Lint and test" }] }));
    const api = new HttpCodingApi(fake.http);
    expect(await api.listRemotes()).toEqual(["gitlab-firmware"]);
    expect(await api.listSkills()).toEqual([{ id: "lint-and-test", name: "Lint and test" }]);
    expect(fake.calls.map((c) => [c.method, c.url])).toEqual([
      ["GET", "/api/v1/coding/remotes"],
      ["GET", "/api/v1/coding/skills"],
    ]);
    for (const call of fake.calls) {
      expectRequestedWith(call);
    }
  });

  it("starts a task with the snake_case breakdown, the plan and the filename, and maps the CodingTask", async () => {
    const fake = fetchWith(() => TASK_WIRE);
    const task = await new HttpCodingApi(fake.http).start(BREAKDOWN, "# Fan controller", "fan-plan.md");
    const call = fake.only();
    expect(call.method).toBe("POST");
    expect(call.url).toBe("/api/v1/coding/tasks");
    expectRequestedWith(call);
    expect(call.body).toEqual({
      breakdown: {
        title: "Fan controller",
        tasks: [
          { n: 1, title: "Parse config.yaml" },
          { n: 2, title: "Add a pytest test" },
        ],
        languages: [
          { language: "python", version: null },
          { language: "rust", version: "1.99" },
        ],
        isolation: "auto",
        skills: ["lint-and-test"],
        cross_check: true,
        export_target: "remote",
        remote_ref: "gitlab-firmware",
        max_iterations: 6,
      },
      plan: "# Fan controller",
      filename: "fan-plan.md",
    });
    expect(task).toEqual({
      ticketId: "T-coding-0001",
      title: "Fan controller",
      state: "Running",
      sentence: "T-coding-0001 is running: step 2 of 7.",
      steps: [
        { n: 1, title: "Toolchain: Python 3.12.6.", status: "done" },
        { n: 2, title: "Open an isolated sandbox", status: "running" },
        { n: 3, title: "Task 1: Parse config.yaml", status: "pending" },
      ],
      feed: ["Toolchain: Python 3.12.6.", "Opening an isolated sandbox…"],
    });
  });

  it("lists tasks and reads one task; a body with missing fields renders as empty, not a crash", async () => {
    const fake = fetchWith((call) => (call.url.endsWith("/tasks") ? [TASK_WIRE, { ticket_id: "T-coding-0002" }] : TASK_WIRE));
    const api = new HttpCodingApi(fake.http);
    const tasks = await api.listTasks();
    expect(tasks[1]).toEqual({ ticketId: "T-coding-0002", title: "", state: "", sentence: "", steps: [], feed: [] });
    expect((await api.getTask("T-coding/0001")).ticketId).toBe("T-coding-0001");
    expect(fake.calls[1]?.url).toBe("/api/v1/coding/tasks/T-coding%2F0001");
  });

  it("surfaces a three-part refusal as an ApiError with the parts", async () => {
    const fake = fetchWith(() => threePart(400, "The plan has no tasks.", "Only a title line was found.", "Add a bullet per task."));
    const error = (await new HttpCodingApi(fake.http).propose("# Title", "plan.md").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.parts).toEqual({ whatHappened: "The plan has no tasks.", likelyCause: "Only a title line was found.", whatToDo: "Add a bullet per task." });
    const html = fetchWith(() => new Response("<html>502</html>", { status: 502 }));
    const unreachable = (await new HttpCodingApi(html.http).listTasks().catch((e: unknown) => e)) as ApiError;
    expect(unreachable.unreachable).toBe(true);
    expect(json(200, {}).status).toBe(200);
  });
});
