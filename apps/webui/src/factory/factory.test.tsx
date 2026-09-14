import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeFactoryApi, reviewSentence } from "./api";
import { FactoryPage } from "./FactoryPage";

describe("New factory job wizard", () => {
  it("walks Trigger → Test loop → Rules and shows the step map, the screenshot strip and PASS", async () => {
    const api = new FakeFactoryApi();
    render(<FactoryPage api={api} user="lee" />);
    expect(await screen.findByText(/No factory job yet/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "New factory job" }));
    expect(screen.getByText("Step 1 of 3 — Trigger")).toBeTruthy();
    const next = screen.getByRole("button", { name: "Next: Test loop" }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);
    expect(screen.getByTestId("trigger-note").textContent).toBe(
      "Pick a production ticket, scan a label, or enter the unit by hand.",
    );

    fireEvent.click(await screen.findByLabelText("MES-88131"));
    expect(screen.getByTestId("trigger-note").textContent).toBe(
      "Unit SN-GX8-0100 on station-07, from MES ticket MES-88131.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Next: Test loop" }));

    expect(await screen.findByText("Step 2 of 3 — Test loop")).toBeTruthy();
    expect((screen.getByLabelText("Final test, 9 steps") as HTMLInputElement).checked).toBe(true);
    const steps = within(screen.getByTestId("template-steps")).getAllByRole("listitem");
    expect(steps).toHaveLength(10);
    expect(steps[8]?.textContent).toBe("Decide PASS or FAIL");
    expect(screen.getByTestId("skills-used").textContent).toBe(
      "Skills used for the GUI steps: station-login-burnin. Every GUI step is screenshot before and after.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Next: Rules" }));

    expect(await screen.findByText("Step 3 of 3 — Rules")).toBeTruthy();
    expect(screen.getByText(/PASS needs all three; a split vote goes to the line lead/)).toBeTruthy();
    expect(screen.getByTestId("sentence").textContent).toBe(
      "Final test, 9 steps for unit SN-GX8-0100 on station-07: 10 steps, using the station-login-burnin " +
        "skill. PASS needs 3 of 3 voters; anything else holds the station for the line lead. The station " +
        "state is backed up.",
    );
    fireEvent.click(screen.getByLabelText("Back up the station state"));
    expect(screen.getByTestId("sentence").textContent).toContain("The station state is not backed up.");
    fireEvent.click(screen.getByLabelText("Back up the station state"));
    fireEvent.click(screen.getByRole("button", { name: "Start job" }));

    const card = await screen.findByLabelText("T-factory-0001");
    expect(within(card).getByText("10 of 10 steps done. Verdict: PASS (3 of 3 voters).")).toBeTruthy();
    const map = within(card).getByLabelText("T-factory-0001 step map");
    expect(within(map).getAllByRole("listitem")).toHaveLength(10);
    expect(within(map).getByLabelText("Step 9: done").getAttribute("title")).toBe(
      "PASS: 3 of 3 voters say PASS. 3 of 3 agree with the conclusion.",
    );
    const strip = within(card).getByLabelText("T-factory-0001 screenshots");
    expect(within(strip).getAllByRole("img")).toHaveLength(8);
    expect(within(strip).getAllByRole("img")[0]?.getAttribute("alt")).toBe("Step 3, screenshot 1");
    expect(within(card).getByTestId("T-factory-0001-verdict").textContent).toBe(
      "PASS: 3 of 3 voters say PASS. 3 of 3 agree with the conclusion.",
    );
    expect(within(card).getByText("Station backup: Backups/stations/station-07/T-factory-0001")).toBeTruthy();
    expect(within(card).queryByRole("button", { name: /Decide PASS/ })).toBeNull();
    expect(api.mesTickets.map((t) => t.ticketNo)).toEqual(["MES-88132"]);
  });

  it("holds the station on a failed unit until the line lead decides", async () => {
    const api = new FakeFactoryApi();
    render(<FactoryPage api={api} user="lee" />);
    fireEvent.click(await screen.findByRole("button", { name: "New factory job" }));
    fireEvent.click(await screen.findByLabelText("MES-88132"));
    fireEvent.click(screen.getByRole("button", { name: "Next: Test loop" }));
    fireEvent.click(await screen.findByRole("button", { name: "Next: Rules" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start job" }));

    const card = await screen.findByLabelText("T-factory-0001");
    expect(within(card).getByText("9 of 10 steps done. Verdict: FAIL; the station is held for the line lead.")).toBeTruthy();
    const map = within(card).getByLabelText("T-factory-0001 step map");
    expect(within(map).getByLabelText("Step 9: needs you")).toBeTruthy();
    expect(within(map).getByLabelText("Step 10: waiting")).toBeTruthy();
    expect(within(card).getByTestId("T-factory-0001-verdict").textContent).toBe(
      "FAIL: Unit SN-GX8-0101-F failed the final test on station-07: BurnIn reported FAIL (gpu-memory). " +
        "The unit stays on and station-07 is held; a ticket is drafted for the line lead.",
    );
    expect(within(card).getByRole("button", { name: "Review ticket T-factory-0002" })).toBeTruthy();
    expect(within(card).getByText(/station-07 is held and the unit stays on until you decide/)).toBeTruthy();
    expect((await api.listStations()).find((s) => s.name === "station-07")?.free).toBe(false);

    fireEvent.change(within(card).getByLabelText("Decision note T-factory-0001"), {
      target: { value: "Reseated the GPU riser; retest passed by hand." },
    });
    fireEvent.click(within(card).getByRole("button", { name: "Decide PASS T-factory-0001" }));
    await waitFor(() =>
      expect(within(card).getByTestId("T-factory-0001-verdict").textContent).toBe(
        "PASS: decided by lee. Reseated the GPU riser; retest passed by hand.",
      ),
    );
    expect(within(card).getByText("9 of 10 steps done. Verdict: PASS (lee, line lead).")).toBeTruthy();
    expect(within(card).queryByRole("button", { name: /Decide FAIL/ })).toBeNull();
    expect((await api.listStations()).find((s) => s.name === "station-07")?.free).toBe(true);
  });

  it("reads labels, refuses a busy station and explains a bad label in three parts", async () => {
    const api = new FakeFactoryApi();
    expect(await api.parseLabel("no useful text")).toContain("names no unit and station.");
    const parsed = await api.parseLabel("SN SN-GX8-0300 station station-08");
    expect(parsed).toEqual({ kind: "label", ticketNo: "manual-sn-gx8-0300", station: "station-08", unitSn: "SN-GX8-0300" });

    render(<FactoryPage api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "New factory job" }));
    fireEvent.change(await screen.findByLabelText("Label"), { target: { value: "garbage" } });
    fireEvent.click(screen.getByRole("button", { name: "Read label" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Scan the label again or type both values.");

    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "SN SN-GX8-0300 station station-08" } });
    fireEvent.click(screen.getByRole("button", { name: "Read label" }));
    await waitFor(() =>
      expect(screen.getByTestId("trigger-note").textContent).toContain(
        "Unit SN-GX8-0300 on station-08: the station is busy. station-08 is leased to T-factory-0007 (mes)",
      ),
    );
    expect((screen.getByRole("button", { name: "Next: Test loop" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Serial number"), { target: { value: "SN-GX8-0400" } });
    fireEvent.change(screen.getByLabelText("Station"), { target: { value: "station-07" } });
    fireEvent.click(screen.getByRole("button", { name: "Use these" }));
    expect(screen.getByTestId("trigger-note").textContent).toBe(
      "Unit SN-GX8-0400 on station-07, from a manual entry.",
    );
    expect((screen.getByRole("button", { name: "Next: Test loop" }) as HTMLButtonElement).disabled).toBe(false);
    const [template] = await api.listTemplates();
    expect(
      reviewSentence(
        { kind: "manual", ticketNo: "manual-x", station: "station-07", unitSn: "SN-GX8-0400" },
        template!,
        { voters: 3, onFail: "hold_station", exportSop: true, backupStation: false },
      ),
    ).toContain("The station state is not backed up.");
  });
});

describe("Watch and take over", () => {
  it("shows the VNC sentence, pauses at the next step boundary, resumes and aborts", async () => {
    const api = new FakeFactoryApi();
    await api.start({ kind: "mes", ticketNo: "MES-1", station: "station-07", unitSn: "SN-GX8-0200-R" }, "final-test-9-steps", {
      voters: 3,
      onFail: "hold_station",
      exportSop: true,
      backupStation: true,
    });
    render(<FactoryPage api={api} user="lee" />);
    const card = await screen.findByLabelText("T-factory-0001");
    expect(within(card).getByText(/3 of 10 steps done\.$/)).toBeTruthy();
    expect(within(card).getByText(/You can watch station-07 live and take it over at any point/)).toBeTruthy();

    fireEvent.click(within(card).getByRole("button", { name: "Watch station T-factory-0001" }));
    expect((await screen.findByTestId("T-factory-0001-control")).textContent).toBe("The runner drives station-07.");
    expect(screen.getByTestId("T-factory-0001-watch").textContent).toBe(
      "Watch the station at vnc://127.0.0.1:5901 (relayed over mTLS to https://station-07:8443). Read-only until you take over.",
    );

    fireEvent.click(within(card).getByRole("button", { name: "Take over T-factory-0001" }));
    await waitFor(() =>
      expect(screen.getByTestId("T-factory-0001-control").textContent).toBe(
        "lee has taken over station-07; the runner sends no input until it is resumed.",
      ),
    );
    expect(within(card).queryByRole("button", { name: "Take over T-factory-0001" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Resume T-factory-0001" }));
    await waitFor(() => expect(screen.getByTestId("T-factory-0001-control").textContent).toBe("The runner drives station-07."));

    fireEvent.click(within(card).getByRole("button", { name: "Abort T-factory-0001" }));
    await waitFor(() => expect(screen.getByText("Stopped: lee took over station-07.", { selector: "p" })).toBeTruthy());
    expect(api.jobs[0]?.state).toBe("Failed");
  });

  it("says when a station has VNC off instead of showing a dead link", async () => {
    const api = new FakeFactoryApi();
    api.stations = [{ name: "station-08", free: true, holder: null }];
    await api.start({ kind: "manual", ticketNo: "manual-1", station: "station-08", unitSn: "SN-1-R" }, "final-test-9-steps", {
      voters: 3,
      onFail: "hold_station",
      exportSop: true,
      backupStation: false,
    });
    render(<FactoryPage api={api} user="lee" />);
    const card = await screen.findByLabelText("T-factory-0001");
    fireEvent.click(within(card).getByRole("button", { name: "Watch station T-factory-0001" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "VNC is not enabled on station-08. The station record has VNC off. Enable it under Admin → Stations and re-enrol.",
    );
  });
});
