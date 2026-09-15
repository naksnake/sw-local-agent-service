import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeValidationApi } from "./api";
import { ValidationPage } from "./ValidationPage";

const SUITE = `# GX8 DC cycling

- DC cycle x25, settle 60 s
- Read SEL
`;

describe("New validation run wizard", () => {
  it("walks Suite → Target → Review & approve and shows the LED cycle map with the finding", async () => {
    const api = new FakeValidationApi();
    render(<ValidationPage api={api} user="lee" />);
    expect(await screen.findByText(/No validation run yet/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "New validation run" }));
    expect(screen.getByText("Step 1 of 3 — Suite")).toBeTruthy();
    const next = screen.getByRole("button", { name: "Next: Target" }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);
    expect(screen.getByTestId("items-note").textContent).toBe(
      "The items are listed here once you add the suite. Destructive items are flagged.",
    );

    fireEvent.change(screen.getByLabelText("Suite"), { target: { value: SUITE } });
    await waitFor(() =>
      expect(screen.getByTestId("items-note").textContent).toBe(
        "GX8 DC cycling: 2 items, 26 cycles in total.",
      ),
    );
    const items = within(screen.getByTestId("items")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["1. DC cycle ×25", "2. Read SEL"]);
    fireEvent.click(screen.getByRole("button", { name: "Next: Target" }));

    // Target: busy servers cannot be picked, and the copy says why; no credential anywhere.
    expect(await screen.findByText("Step 2 of 3 — Target")).toBeTruthy();
    expect(screen.getByText(/Credentials come from the vault/)).toBeTruthy();
    const busy = (await screen.findByLabelText("lab-gx8-02")) as HTMLInputElement;
    expect(busy.disabled).toBe(true);
    expect(screen.getByText(/Busy: lab-gx8-02 is leased to T-validation-0007 \(lee\)/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next: Review" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("lab-gx8-01"));
    fireEvent.click(screen.getByRole("button", { name: "Next: Review" }));

    // Review & approve: the sentence, the cross-check, the guardrails, one verb button.
    expect(await screen.findByText("Step 3 of 3 — Review & approve")).toBeTruthy();
    expect((await screen.findByTestId("sentence")).textContent).toBe(
      "GX8 DC cycling on lab-gx8-01: 25 power cycles and 2 suite items, 31 steps. Nothing destructive.",
    );
    expect(screen.getByTestId("cross-check").textContent).toBe(
      "3 of 3 voters agree the plan stays within the guardrails. Your approval starts it.",
    );
    expect(within(screen.getByTestId("guardrails")).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.queryByTestId("destructive")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Approve and start" }));

    // The run card: LED map with the planted finding at cycle 14, console with fence markers,
    // one finding with its owner and a Review ticket button.
    const card = await screen.findByLabelText("T-validation-0001");
    expect(within(card).getByText("25 of 25 cycles done: 12 with findings.")).toBeTruthy();
    const map = within(card).getByLabelText("T-validation-0001 cycle map");
    expect(within(map).getAllByRole("listitem")).toHaveLength(25);
    expect(within(map).getByLabelText("Cycle 13: ok")).toBeTruthy();
    expect(within(map).getByLabelText("Cycle 14: finding").getAttribute("title")).toBe(
      "Cycle 14 (DC): booted; 1 change against the baseline during DC cycle 14: " +
        "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14.",
    );
    expect(within(card).getByLabelText("T-validation-0001 console").textContent).toContain(
      "--- slas fence T-validation-0001 cycle 14 dc ---",
    );
    const findings = within(card).getByLabelText("T-validation-0001 findings");
    expect(within(findings).getAllByRole("listitem")[0]?.textContent).toBe(
      "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14. " +
        "Owner: EE. Review ticket",
    );
    expect(within(card).getByRole("button", { name: "Review ticket T-validation-0002" })).toBeTruthy();
  });

  it("blocks an AC-cycle plan until the steps are approved and refuses one without the flag", async () => {
    const api = new FakeValidationApi();
    const refused = await api.parseSuite("- AC cycle x2\n", "ac.md");
    expect(refused.items[0]).toMatchObject({ destructive: true, approved: false });

    render(<ValidationPage api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "New validation run" }));
    fireEvent.change(screen.getByLabelText("Suite"), { target: { value: "# AC check\n- AC cycle x2\n" } });
    expect(await screen.findByText(/not flagged as approved in the suite/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next: Target" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Suite"), {
      target: { value: "# AC check\n- AC cycle x2, approved\n" },
    });
    expect(await screen.findByText(/the run will ask for your approval before this step/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next: Target" }));
    fireEvent.click(await screen.findByLabelText("lab-gx4-01"));
    fireEvent.click(screen.getByRole("button", { name: "Next: Review" }));
    expect((await screen.findByTestId("sentence")).textContent).toBe(
      "AC check on lab-gx4-01: 2 power cycles and 1 suite items, 7 steps. 2 destructive steps need your approval before the run starts.",
    );
    expect(within(screen.getByTestId("destructive")).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "AC cycle 1 of 2",
      "AC cycle 2 of 2",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Approve and start" }));

    const card = await screen.findByLabelText("T-validation-0001");
    expect(within(card).getByText(/waits for your approval of 2 steps before anything touches lab-gx4-01/)).toBeTruthy();
    expect(within(card).getByText(/Nothing has touched lab-gx4-01 yet\. 2 steps need your approval/)).toBeTruthy();
    const map = within(card).getByLabelText("T-validation-0001 cycle map");
    expect(within(map).getAllByLabelText(/waiting/)).toHaveLength(2);
    fireEvent.click(within(card).getByRole("button", { name: "Approve these steps and continue" }));
    await waitFor(() => expect(within(card).getByText("2 of 2 cycles done.")).toBeTruthy());
    expect(within(map).getAllByLabelText(/: ok/)).toHaveLength(2);
    expect(api.runs[0]?.console[0]).toBe("--- approvals recorded by you ---");
  });

  it("explains an unreadable suite in three parts", async () => {
    const api = new FakeValidationApi();
    const empty = await api.parseSuite("   ", "suite.md");
    expect(empty.problem).toBe(
      "suite.md is empty. A suite needs a title and at least one item. Add the items and upload again.",
    );
    const unknown = await api.parseSuite("- Dance the macarena\n", "suite.md");
    expect(unknown.problem).toContain("Step 1 (Dance the macarena) names no known action.");
    const table = await api.parseSuite(
      "| Step | Action | Parameters | Cycles | Approved |\n|---|---|---|---|---|\n| Flash BMC | Flash the BMC | image=bmc-1.13 | | yes |\n",
      "fw.md",
    );
    expect(table.items[0]).toMatchObject({ title: "Flash BMC", destructive: true, approved: true, cycles: 1 });
    const noHeader = await api.parseSuite("| Foo |\n|---|\n| bar |\n", "t.md");
    expect(noHeader.problem).toContain("without a Step or Action column");
  });
});
