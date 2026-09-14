import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeCodingApi, reviewSentence, toolchainSentence } from "./api";
import { CodingPage } from "./CodingPage";

const PLAN = `# Fan controller

- Parse \`config.yaml\` into a dataclass in \`fan_ctl.py\`.
- Add a \`pytest\` test for the parser.
`;

describe("New coding task wizard", () => {
  it("walks Plan → Setup → Review and ends in a sentence and a verb button", async () => {
    const api = new FakeCodingApi();
    render(<CodingPage api={api} />);
    expect(await screen.findByText(/No coding task yet/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "New coding task" }));
    expect(screen.getByText("Step 1 of 3 — Plan")).toBeTruthy();
    const next = screen.getByRole("button", { name: "Next: Setup" }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Plan"), { target: { value: PLAN } });
    await waitFor(() =>
      expect(screen.getByTestId("detected").textContent).toBe(
        "Languages detected: Python, YAML/JSON config.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next: Setup" }));
    expect(await screen.findByText("Step 2 of 3 — Setup")).toBeTruthy();

    // Languages only, version optional: pin Rust 1.99, which the bundle does not have.
    fireEvent.click(screen.getByLabelText("Rust"));
    fireEvent.change(screen.getByLabelText("Rust version"), { target: { value: "1.99" } });
    expect((screen.getByLabelText("Python version") as HTMLInputElement).placeholder).toBe(
      "newest bundled",
    );

    // Export to a remote is impossible without a saved remote, and the copy says where to go.
    fireEvent.click(screen.getByLabelText("Push a branch to a Git remote you have added"));
    expect(screen.getByText(/Add one under Settings → Git remotes/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next: Review" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("ZIP file (always available)"));
    fireEvent.click(screen.getByRole("button", { name: "Next: Review" }));

    expect(await screen.findByText("Step 3 of 3 — Review")).toBeTruthy();
    const resolutions = await screen.findByTestId("resolutions");
    await waitFor(() => expect(within(resolutions).getAllByRole("listitem")).toHaveLength(3));
    expect(resolutions.textContent).toContain(
      "Rust 1.99 isn't in the offline toolchain bundle, so the newest bundled 1.80.1 is used instead.",
    );
    expect(resolutions.textContent).toContain(
      "Python: no version pinned, so the newest bundled python 3.12.6 is used.",
    );
    expect((await screen.findByTestId("sentence")).textContent).toBe(
      "The agent will work in an isolated sandbox with Python 3.12.6, YAML/JSON config 1.35.1, " +
        "Rust 1.80.1, do 2 tasks, commit on its own branch, cross-check the result with 3 " +
        "voters, and export a ZIP. Rust 1.99 isn't in the offline toolchain bundle, so the " +
        "newest bundled 1.80.1 is used instead.",
    );

    // The proposed steps are editable.
    fireEvent.change(screen.getByLabelText("Task 2"), {
      target: { value: "Add a pytest test for the parser and the CLI." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start task" }));

    // The ticket appears with its plan checklist; the first feed line is the toolchain choice.
    const card = await screen.findByLabelText("T-coding-0001");
    expect(within(card).getByText(/T-coding-0001 is running/)).toBeTruthy();
    const activity = within(card).getByLabelText("T-coding-0001 activity");
    expect(within(activity).getAllByRole("listitem")[0]?.textContent).toBe(
      "Toolchain: Python 3.12.6, YAML/JSON config 1.35.1 and Rust 1.80.1. Rust 1.99 isn't in " +
        "the offline toolchain bundle, so the newest bundled 1.80.1 is used instead.",
    );
    expect(within(card).getByText(/Task 2: Add a pytest test for the parser and the CLI\./)).toBeTruthy();
    expect(within(card).getByText(/Cross-check the final diff with 3 voters/)).toBeTruthy();
    expect(api.tasks[0]?.title).toBe("Fan controller");
  });

  it("keeps pinned versions the bundle has and says so", async () => {
    const api = new FakeCodingApi();
    const [exact, prefix] = await api.resolveToolchains([
      { language: "python", version: "3.11.10" },
      { language: "python", version: "3.11" },
    ]);
    expect(exact?.sentence).toBe("Python 3.11.10 pinned; the bundle has it exactly, using 3.11.10.");
    expect(prefix?.sentence).toBe(
      "Python 3.11 pinned; the bundle has it as the newest 3.11.x, using 3.11.10.",
    );
    expect(toolchainSentence([])).toBe("No language was chosen.");
    expect(toolchainSentence(exact ? [exact] : [])).toBe("Toolchain: Python 3.11.10.");
    const breakdown = await api.propose("just prose", "fan-plan.md");
    expect(breakdown.title).toBe("fan plan");
    expect(breakdown.tasks).toEqual([{ n: 1, title: "fan plan" }]);
    expect(breakdown.languages).toEqual([{ language: "shell", version: "" }]);
    expect(
      reviewSentence({ ...breakdown, crossCheck: false, exportTarget: "bundle" }, exact ? [exact] : []),
    ).toBe(
      "The agent will work in an isolated sandbox with Python 3.11.10, do 1 task, commit on its " +
        "own branch, skip the cross-check, as you asked, and export a Git bundle.",
    );
  });
});
