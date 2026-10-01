import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeCodingApi } from "../coding/api";
import { FakeFactoryApi } from "../factory/api";
import { renderApp } from "../test-utils";
import { FakeValidationApi } from "../validation/api";
import { healthSentence, needsYou, recentResults, runningNow, snapshot } from "./HomePage";

async function fakes() {
  const codingApi = new FakeCodingApi();
  const validationApi = new FakeValidationApi();
  const factoryApi = new FakeFactoryApi();
  return { codingApi, validationApi, factoryApi };
}

async function startOneOfEach(apis: Awaited<ReturnType<typeof fakes>>) {
  const breakdown = await apis.codingApi.propose("# Fan controller\n\n- read the fan table\n", "plan.md");
  await apis.codingApi.start(breakdown, "# Fan controller");
  const suite = await apis.validationApi.parseSuite("# Power cycle suite\n\n- AC cycle x3, settle 30 s\n", "suite.md");
  const [target] = await apis.validationApi.listTargets();
  if (target === undefined) throw new Error("the fake has no target");
  await apis.validationApi.start(suite, target.ref);
  const [ticket] = await apis.factoryApi.listMesTickets();
  const [template] = await apis.factoryApi.listTemplates();
  if (ticket === undefined || template === undefined) throw new Error("the fake has no ticket");
  await apis.factoryApi.start(
    // A unit whose serial ends in -R stays running in the fake; the plain one finishes at once.
    { kind: "mes", station: ticket.station, unitSn: `${ticket.unitSn}-R`, ticketNo: ticket.ticketNo },
    template.id,
    { voters: 3, onFail: "hold_station", exportSop: true, backupStation: true },
  );
}

describe("Home derives everything from the agents' own lists", () => {
  it("is empty and healthy before anything starts", async () => {
    const s = await snapshot(await fakes());
    expect(needsYou(s)).toEqual([]);
    expect(runningNow(s)).toEqual([]);
    expect(recentResults(s)).toEqual([]);
    expect(healthSentence(null)).toBe("Looking at what is running…");
    expect(healthSentence(s)).toBe("Everything is healthy. Nothing is running.");
  });

  it("puts a run that waits for approval under needs-you and counts it in the health line", async () => {
    const apis = await fakes();
    await startOneOfEach(apis);
    const s = await snapshot(apis);
    const attention = needsYou(s);
    expect(attention.map((a) => a.page)).toContain("validation");
    const run = attention.find((a) => a.page === "validation");
    expect(run?.title).toMatch(/^Validation run T-validation-\d+ is waiting for your approval\.$/);
    expect(run?.whatToDo).toBe("Open the run, read the plan, and approve it or take those steps out.");
    const running = runningNow(s);
    expect(running.map((r) => r.page).sort()).toEqual(["coding", "factory", "validation"]);
    expect(running.find((r) => r.page === "validation")?.pill).toBe("Waiting for approval");
    expect(healthSentence(s)).toMatch(/^\d+ items? needs? you\. 3 jobs running\.$/);
  });
});

describe("Home page", () => {
  it("renders needs-you, running-now and the buttons, and opens a page's wizard", async () => {
    const apis = await fakes();
    await startOneOfEach(apis);
    renderApp(apis);
    expect(await screen.findByText(/is waiting for your approval\./)).toBeTruthy();
    const running = screen.getByRole("region", { name: "Running now" });
    await waitFor(() => expect(within(running).getAllByRole("listitem").length).toBe(3));
    expect(within(running).getByText(/Coding task T-coding-\d+ — /)).toBeTruthy();
    expect(within(running).getByText(/Factory ticket T-factory-\d+ — Station station-07, unit SN-GX8-0100-R/)).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/jobs running\.$/);

    fireEvent.click(screen.getByRole("button", { name: "New validation run" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Validation" })).toBeTruthy();
    expect(screen.getByText(/Step 1 of 3/)).toBeTruthy();
  });

  it("shows the two empty states in sentences", async () => {
    renderApp(await fakes());
    expect(await screen.findByText("Nothing is running. Start a task, run or job with the buttons above.")).toBeTruthy();
    expect(screen.getByText("No results yet. Finished tasks, runs and jobs appear here with their outcome.")).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Pages" }).textContent).toContain("Validation");
  });
});
