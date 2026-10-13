import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp, signedIn } from "../test-utils";
import { BLANK_SKILL } from "./api";
import { FakeSkillsApi } from "./fake";
import { SkillsPage } from "./SkillsPage";

function alertParts(container: HTMLElement): string[] {
  const alert = within(container).getByRole("alert");
  return [within(alert).getByRole("strong").textContent ?? "", ...within(alert).getAllByRole("definition").map((d) => d.textContent ?? "")];
}

describe("Skills (docs/ui/skills.md)", () => {
  it("starts empty, with the shipped examples to add or start from", async () => {
    render(<SkillsPage api={new FakeSkillsApi()} />);
    expect(await screen.findByText(/No skill yet\. Add one of the examples below/)).toBeTruthy();
    const examples = screen.getByRole("region", { name: "Examples that ship with the platform" });
    const python = within(examples).getByRole("article", { name: "Python lint and tests" });
    expect(within(python).getByText("Every coding task must pass ruff and pytest in the sandbox before it is done.")).toBeTruthy();
    expect(within(python).getByText("For the Coding Agent · needs files")).toBeTruthy();
    expect(within(examples).getByRole("article", { name: "Collect and clear the BMC event log" })).toBeTruthy();
  });

  it("adds an example, switches it on for the Coding Agent, and says so in a sentence", async () => {
    const api = new FakeSkillsApi();
    render(<SkillsPage api={api} />);
    const python = await screen.findByRole("article", { name: "Python lint and tests" });
    fireEvent.click(within(python).getByRole("button", { name: "Add to library" }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "Python lint and tests v1.0.0: 2 steps, needs files, risk caution. Off for every agent. Switch it on below for the agents that should use it.",
    );
    const row = await screen.findByRole("row", { name: "Python lint and tests" });
    expect(within(row).getByText("Caution: runs commands")).toBeTruthy();
    expect(within(await screen.findByRole("article", { name: "Python lint and tests" })).getByRole("button", { name: "In your library" })).toHaveProperty(
      "disabled",
      true,
    );

    const toggle = within(row).getByRole("switch", { name: "Python lint and tests for the Coding Agent" });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByRole("status").textContent).toMatch(/^Python lint and tests: On for the Coding Agent since /));
    expect((within(screen.getByRole("row", { name: "Python lint and tests" })).getByRole("switch") as HTMLInputElement).checked).toBe(true);
  });

  it("writes a new skill: Check reads it without saving, Save puts it in the library", async () => {
    const api = new FakeSkillsApi();
    render(<SkillsPage api={api} />);
    await screen.findByText(/No skill yet/);
    fireEvent.click(screen.getByRole("button", { name: "New skill" }));
    const editor = screen.getByRole("region", { name: "New skill" });
    const file = within(editor).getByLabelText("Skill file") as HTMLTextAreaElement;
    expect(file.value).toBe(BLANK_SKILL);

    fireEvent.change(file, { target: { value: "not a skill" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Check" }));
    await waitFor(() => expect(alertParts(editor)[0]).toBe("The skill file is not valid YAML."));

    fireEvent.change(file, { target: { value: BLANK_SKILL } });
    fireEvent.click(within(editor).getByRole("button", { name: "Check" }));
    const result = await within(editor).findByRole("status", { name: "Check result" });
    expect(within(result).getByText("My project checks v1.0.0: 1 step, needs files, risk caution.")).toBeTruthy();
    expect(within(result).getByText("run: python -m pytest -q")).toBeTruthy();
    expect(within(result).getByText("Saving adds My project checks to the library, switched off for every agent.")).toBeTruthy();
    expect(await api.list()).toEqual([]);

    fireEvent.click(within(editor).getByRole("button", { name: "Save to library" }));
    await screen.findByRole("row", { name: "My project checks" });
    expect(screen.queryByRole("region", { name: "New skill" })).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(
      "My project checks v1.0.0: 1 step, needs files, risk caution. Off for every agent.",
    );
  });

  it("refuses a step that is not a whitelisted primitive, in three parts", async () => {
    render(<SkillsPage api={new FakeSkillsApi()} />);
    await screen.findByText(/No skill yet/);
    fireEvent.click(screen.getByRole("button", { name: "New skill" }));
    const editor = screen.getByRole("region", { name: "New skill" });
    fireEvent.change(within(editor).getByLabelText("Skill file"), {
      target: { value: BLANK_SKILL.replace("run: { command: [python, -m, pytest, -q], expect_exit: 0, timeout_s: 600 }", 'shell: { command: "rm -rf /" }') },
    });
    fireEvent.click(within(editor).getByRole("button", { name: "Save to library" }));
    await waitFor(() =>
      expect(alertParts(editor)).toEqual([
        "the skill file uses `shell`, which is not a skill primitive.",
        "Skills may only use the whitelisted verbs; there is no shell, eval, python or download.",
        "Fix the file and import it again; the whitelist is CLAUDE.md §6.2.",
      ]),
    );
  });

  it("opens a skill to show its steps, edits it, and deletes it after a confirmation", async () => {
    const api = new FakeSkillsApi();
    await api.save(BLANK_SKILL);
    render(<SkillsPage api={api} />);
    const row = await screen.findByRole("row", { name: "My project checks" });
    fireEvent.click(within(row).getByRole("button", { name: "My project checks" }));
    const details = await screen.findByLabelText("My project checks details");
    expect(within(details).getByText("run: python -m pytest -q")).toBeTruthy();
    expect(within(details).getByText(/^Imported by pat@slas\.local/)).toBeTruthy();
    fireEvent.click(within(details).getByRole("button", { name: "Edit" }));
    expect((within(screen.getByRole("region", { name: "Edit My project checks" })).getByLabelText("Skill file") as HTMLTextAreaElement).value).toBe(
      BLANK_SKILL,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(within(screen.getByRole("row", { name: "My project checks" })).getByRole("button", { name: "Delete My project checks" }));
    const dialog = screen.getByRole("dialog", { name: "Delete My project checks?" });
    expect(within(dialog).getByText(/leaves the library: no agent can use it any more/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete My project checks" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("row", { name: "My project checks" })).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(
      "My project checks v1.0.0 was deleted; the Coding Agent can no longer use it. Tickets that already ran it keep their record.",
    );
  });

  it("only the importer or an administrator may delete a skill", async () => {
    let who = "lee@slas.local";
    const api = new FakeSkillsApi(() => who);
    await api.save(BLANK_SKILL);
    who = "pat@slas.local";
    render(<SkillsPage api={api} />);
    const row = await screen.findByRole("row", { name: "My project checks" });
    fireEvent.click(within(row).getByRole("button", { name: "Delete My project checks" }));
    const dialog = screen.getByRole("dialog", { name: "Delete My project checks?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete My project checks" }));
    await waitFor(() =>
      expect(alertParts(dialog)).toEqual([
        "You can't delete My project checks.",
        "It was imported by lee@slas.local; only they or an administrator may delete it.",
        "Ask them, or switch it off for your agent instead.",
      ]),
    );
  });

  it("is on the rail and opens from it", async () => {
    const { sessionApi } = signedIn();
    renderApp({ sessionApi, skillsApi: new FakeSkillsApi() }, "/skills");
    expect(await screen.findByRole("heading", { level: 1, name: "Skills" })).toBeTruthy();
    expect(await screen.findByRole("article", { name: "Python lint and tests" })).toBeTruthy();
  });
});
