import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { models as copy } from "../copy/en";
import { FakeModelsApi } from "./fake";
import { ModelsPage } from "./ModelsPage";

const FAST_POLL = 5;

function roleSelect(role: string): HTMLSelectElement {
  return screen.getByLabelText(role) as HTMLSelectElement;
}

describe("Models (docs/ui/models.md)", () => {
  it("shows the registry sentence, one card per model, the role assignments and the voters", async () => {
    render(<ModelsPage api={new FakeModelsApi()} />);
    expect(screen.getByText(copy.loading)).toBeTruthy();
    expect((await screen.findByTestId("registry-sentence")).textContent).toBe(
      "4 models; coder → Qwen3.8-27B, planner → Qwen3.8-27B, triage → DeepSeek-V4 Flash, embed → BGE-M3, rerank → BGE Reranker v2 M3; 2 voters from 2 model families.",
    );
    const cards = within(screen.getByRole("region", { name: copy.modelsHeading })).getAllByRole("listitem");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["DeepSeek-V4 Flash", "Qwen3.8-27B", "BGE-M3", "BGE Reranker v2 M3"]);
    const deepseek = screen.getByRole("listitem", { name: "DeepSeek-V4 Flash" });
    expect(deepseek.textContent).toContain("DeepSeek family · FP8");
    expect(deepseek.textContent).toContain("180 GiB of GPU memory · 131,072 tokens of context");
    expect(deepseek.textContent).toContain("may serve triage, planner");
    expect(within(deepseek).getByText(copy.present)).toBeTruthy();
    expect(within(screen.getByRole("listitem", { name: "BGE Reranker v2 M3" })).getByText(copy.notPresent)).toBeTruthy();
    expect(screen.getByRole("listitem", { name: "BGE-M3" }).textContent).toContain("BAAI family · BF16");

    // The roles are a select each, showing who serves it; a model without weights cannot be picked.
    expect(roleSelect("coder").value).toBe("qwen3.8-27b-fp8");
    expect(roleSelect("planner").value).toBe("qwen3.8-27b-fp8");
    expect(roleSelect("triage").value).toBe("deepseek-v4-flash");
    expect(roleSelect("embed").value).toBe("bge-m3");
    expect(roleSelect("rerank").value).toBe("bge-reranker-v2-m3");
    const reranker = within(roleSelect("coder")).getByRole("option", { name: copy.edit.notHere("BGE Reranker v2 M3") }) as HTMLOptionElement;
    expect(reranker.disabled).toBe(true);
    // The voters are checkboxes over the models whose weights are here.
    const voters = screen.getByRole("region", { name: copy.votersHeading });
    expect(within(voters).getByText("2 voters from 2 model families.")).toBeTruthy();
    expect(within(voters).getByRole("checkbox", { name: "DeepSeek-V4 Flash (DeepSeek)" })).toHaveProperty("checked", true);
    expect(within(voters).getByRole("checkbox", { name: "Qwen3.8-27B (Qwen)" })).toHaveProperty("checked", true);
    expect(within(voters).getByRole("checkbox", { name: "BGE-M3 (BAAI)" })).toHaveProperty("checked", false);
    expect(within(voters).queryByRole("checkbox", { name: /Reranker/ })).toBeNull();
    expect((screen.getByRole("button", { name: copy.edit.save }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(copy.footer)).toBeTruthy();
    expect(screen.getByText(copy.add.whatHappens)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a registry problem in three parts instead of cards", async () => {
    const api = new FakeModelsApi();
    api.breakRegistry({
      what_happened: "The model registry Models/models.yaml could not be used.",
      likely_cause: "models[0].quant: 'int4' is not one of fp8, awq4, bf16.",
      what_to_do: "Fix Models/models.yaml on the Models page or by hand; the format is in services/model-manager/README.md.",
    });
    render(<ModelsPage api={api} />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("strong").textContent).toBe("The model registry Models/models.yaml could not be used.");
    expect(within(alert).getAllByRole("definition").map((d) => d.textContent)).toEqual([
      "models[0].quant: 'int4' is not one of fp8, awq4, bf16.",
      "Fix Models/models.yaml on the Models page or by hand; the format is in services/model-manager/README.md.",
    ]);
    expect(screen.queryByRole("region", { name: copy.modelsHeading })).toBeNull();
    expect(screen.queryByTestId("registry-sentence")).toBeNull();
  });

  it("says when the registry didn't load and offers Try again", async () => {
    const api = new FakeModelsApi();
    api.down = true;
    render(<ModelsPage api={api} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(copy.failedToLoad.whatHappened);
    api.down = false;
    fireEvent.click(within(alert).getByRole("button", { name: copy.tryAgain }));
    expect(await screen.findByTestId("registry-sentence")).toBeTruthy();
  });

  it("adds a model from a pasted link: progress, then the card appears (ADR-0018)", async () => {
    const api = new FakeModelsApi();
    render(<ModelsPage api={api} pollMs={FAST_POLL} />);
    await screen.findByTestId("registry-sentence");
    const button = screen.getByRole("button", { name: copy.add.button }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(copy.add.linkLabel), { target: { value: "https://huggingface.co/Qwen/Qwen3.8-27B-FP8" } });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    const download = await screen.findByRole("listitem", { name: "Qwen3.8-27B-FP8 download" });
    await waitFor(() => expect(within(download).getByRole("progressbar")).toBeTruthy());
    expect(within(download).getByText(/^Downloading Qwen3.8-27B-FP8: /).textContent).toMatch(/of 29\.0 GiB, \d of 9 files\./);
    expect(within(download).getByRole("button", { name: copy.add.cancel })).toBeTruthy();

    await waitFor(() => expect(within(download).getByText(copy.add.state.done!)).toBeTruthy(), { timeout: 3000 });
    expect(download.textContent).toContain(
      "Qwen3.8-27B-FP8 is here (29.0 GiB, 9 files) and registered as qwen3.8-27b-fp8-2; give it a role on this page to start it.",
    );
    expect(download.textContent).toContain("Its GPU memory is estimated at 37 GiB from the file sizes");
    // The registry was re-read: the new card is there and it can now serve a role.
    const card = await screen.findByRole("listitem", { name: "Qwen3.8-27B-FP8" });
    expect(card.textContent).toContain("Qwen family · FP8");
    expect(card.textContent).toContain("37 GiB of GPU memory");
    expect(card.textContent).toContain("serves no role");
    expect(within(roleSelect("coder")).getByRole("option", { name: "Qwen3.8-27B-FP8" })).toBeTruthy();
    expect((screen.getByLabelText(copy.add.linkLabel) as HTMLInputElement).value).toBe("");

    fireEvent.click(within(download).getByRole("button", { name: copy.add.remove }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "Qwen3.8-27B-FP8 download" })).toBeNull());
    expect((await screen.findByRole("status")).textContent).toBe("Removed the record of Qwen3.8-27B-FP8; the files stay.");
  });

  it("refuses a link that is not a hub link in three parts, and can cancel a running download", async () => {
    const api = new FakeModelsApi();
    api.pollsToFinish = 1000;
    render(<ModelsPage api={api} pollMs={FAST_POLL} />);
    await screen.findByTestId("registry-sentence");
    fireEvent.change(screen.getByLabelText(copy.add.linkLabel), { target: { value: "just words" } });
    fireEvent.click(screen.getByRole("button", { name: copy.add.button }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("strong").textContent).toBe("The link 'just words' is not one the model fetcher accepts.");
    expect(alert.textContent).toContain("Paste one of: https://huggingface.co/<owner>/<repo>, ");
    expect((screen.getByLabelText(copy.add.linkLabel) as HTMLInputElement).value).toBe("just words");

    fireEvent.change(screen.getByLabelText(copy.add.linkLabel), { target: { value: "MiniMaxAI/MiniMax-M2.7" } });
    fireEvent.change(screen.getByLabelText(copy.add.idLabel), { target: { value: "minimax" } });
    fireEvent.click(screen.getByRole("button", { name: copy.add.button }));
    const download = await screen.findByRole("listitem", { name: "MiniMax-M2.7 download" });
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => expect(within(download).getByRole("progressbar")).toBeTruthy());
    fireEvent.click(within(download).getByRole("button", { name: copy.add.cancel }));
    await waitFor(() => expect(within(download).getByText(copy.add.state.cancelled!)).toBeTruthy());
    expect(download.textContent).toContain("the files stay, and fetching the same link again resumes.");
    expect(within(download).getByRole("button", { name: copy.add.remove })).toBeTruthy();
    expect(screen.queryByRole("listitem", { name: "MiniMax-M2.7" })).toBeNull();
  });

  it("saves roles and voters through the model manager and re-reads the registry", async () => {
    const api = new FakeModelsApi();
    render(<ModelsPage api={api} />);
    await screen.findByTestId("registry-sentence");
    const save = screen.getByRole("button", { name: copy.edit.save }) as HTMLButtonElement;
    fireEvent.change(roleSelect("planner"), { target: { value: "deepseek-v4-flash" } });
    expect(save.disabled).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: "BGE-M3 (BAAI)" }));
    expect(screen.getByText("3 voters from 3 model families.")).toBeTruthy();
    fireEvent.click(save);
    const status = await screen.findByRole("status");
    expect(status.textContent).toBe(
      "Saved: planner → DeepSeek-V4 Flash; voters: DeepSeek-V4 Flash, Qwen3.8-27B, BGE-M3. 3 voters from 3 families. To match the registry: start what changed.",
    );
    expect(api.view.roles.planner).toBe("deepseek-v4-flash");
    expect(api.view.voters).toEqual(["deepseek-v4-flash", "qwen3.8-27b-fp8", "bge-m3"]);
    expect((await screen.findByTestId("registry-sentence")).textContent).toContain("planner → DeepSeek-V4 Flash");
    expect(save.disabled).toBe(true);

    // A refusal from the manager is shown in three parts and nothing moves.
    fireEvent.change(roleSelect("embed"), { target: { value: "" } });
    api.view.models[2]!.present = false; // BGE-M3's weights vanish before the save
    fireEvent.click(screen.getByRole("checkbox", { name: "Qwen3.8-27B (Qwen)" }));
    fireEvent.click(save);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("strong").textContent).toBe("The weights of BGE-M3 are not here yet.");
    expect(alert.textContent).toContain("Add the model from this page");
  });

  it("disables both forms and says why when the person lacks model:manage", async () => {
    render(<ModelsPage api={new FakeModelsApi()} canManage={false} />);
    await screen.findByTestId("registry-sentence");
    expect((screen.getByRole("button", { name: copy.add.button }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText(copy.add.linkLabel) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(copy.add.notAllowed)).toBeTruthy();
    expect((roleSelect("coder") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: copy.edit.save }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(copy.edit.notAllowed)).toBeTruthy();
  });
});
