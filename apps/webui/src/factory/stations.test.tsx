import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeGitApi } from "../git/api";
import { renderApp } from "../test-utils";
import { FakeStationsAdminApi } from "./api";
import { StationsAdmin } from "./StationsAdmin";

describe("Admin → Stations", () => {
  it("lists stations with their sentences, issues a one-time code, revokes and adds", async () => {
    const api = new FakeStationsAdminApi();
    render(<StationsAdmin api={api} user="admin" />);
    expect((await screen.findByTestId("station-07-sentence")).textContent).toBe(
      "station-07: enrolled 2026-09-14 10:00, runner at https://station-07.factory.internal:8443, certificate SHA256:3F2A…9C1D.",
    );
    expect(screen.getByTestId("station-08-sentence").textContent).toBe("station-08: not enrolled yet.");
    const seven = screen.getByLabelText("station-07");
    expect(seven.textContent).toContain("Windows matched by prefix; 0.3 s settle after each action; wait timeouts ×1; at most 10 actions per second.");
    expect(seven.textContent).toContain("Screenshots are kept 30 days (180 days for failed or held jobs), at most 400 per job.");
    expect(seven.textContent).toContain("The operator can watch and take over through VNC (port 5900).");
    expect(screen.getByLabelText("station-08").textContent).toContain("VNC is off: the operator cannot watch or take over this station.");

    fireEvent.click(screen.getByRole("button", { name: "Issue code station-08" }));
    const code = await screen.findByTestId("station-08-code");
    expect(code.textContent).toMatch(
      /^Enter this code on station-08 within 15 minutes: [A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\. It works once; issuing a new code cancels it\.$/,
    );
    expect(api.issued).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Revoke station-07" }));
    expect((await screen.findByTestId("station-07-notice")).textContent).toBe(
      "station-07 is no longer enrolled: its certificate and batch key are refused from now on. Issue a new code to enrol it again.",
    );
    expect(screen.getByTestId("station-07-sentence").textContent).toBe("station-07: not enrolled yet.");
    expect(within(screen.getByLabelText("station-07")).getByRole("button", { name: "Issue code station-07" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Station name"), { target: { value: "Station 09" } });
    fireEvent.click(screen.getByRole("button", { name: "Add station" }));
    expect((await screen.findByRole("alert")).textContent).toContain('"station 09" is not a station name.');
    fireEvent.change(screen.getByLabelText("Station name"), { target: { value: "station-09" } });
    fireEvent.change(screen.getByLabelText("Station description"), { target: { value: "Final test, line 3" } });
    fireEvent.click(screen.getByRole("button", { name: "Add station" }));
    expect((await screen.findByTestId("station-09-notice")).textContent).toBe(
      "station-09 is added. Issue its code, then enter the code on the station.",
    );
    expect(screen.getByLabelText("station-09").textContent).toContain("Final test, line 3");
    fireEvent.click(screen.getByRole("button", { name: "Remove station-09" }));
    await waitFor(() => expect(screen.queryByLabelText("station-09")).toBeNull());
  });

  it("tunes window matching, timing and retention per station", async () => {
    const api = new FakeStationsAdminApi();
    render(<StationsAdmin api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "Tune station-08" }));
    fireEvent.change(screen.getByLabelText("Window match station-08"), { target: { value: "exact" } });
    fireEvent.change(screen.getByLabelText("Settle station-08"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("Timeout scale station-08"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Keep days station-08"), { target: { value: "14" } });
    fireEvent.click(screen.getByLabelText("VNC station-08"));
    fireEvent.click(screen.getByRole("button", { name: "Save tuning station-08" }));
    expect((await screen.findByTestId("station-08-notice")).textContent).toBe(
      "station-08 saved. Windows matched by exact; 0.5 s settle after each action; wait timeouts ×2; at most 10 actions per second. " +
        "Screenshots are kept 14 days (180 days for failed or held jobs), at most 400 per job. " +
        "The station picks the change up on its next enrolment or restart.",
    );
    expect(api.records[1]?.screen.windowMatch).toBe("exact");
    expect(api.records[1]?.vncEnabled).toBe(true);
    expect(screen.getByLabelText("station-08").textContent).toContain("The operator can watch and take over through VNC");
  });

  it("is reachable from Admin next to Git hosts", async () => {
    renderApp({ gitApi: new FakeGitApi(), stationsApi: new FakeStationsAdminApi() });
    fireEvent.click(screen.getByRole("button", { name: "Admin" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Git hosts" })).toBeTruthy();
    fireEvent.click(within(screen.getByLabelText("Admin sections")).getByRole("button", { name: "Stations" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Stations" })).toBeTruthy();
  });
});
