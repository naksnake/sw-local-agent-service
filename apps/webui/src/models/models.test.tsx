import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { models as copy } from "../copy/en";
import { FakeModelsApi } from "./fake";
import { ModelsPage } from "./ModelsPage";

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

    const roles = screen.getByRole("region", { name: copy.rolesHeading });
    expect(within(roles).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "coder → Qwen3.8-27B",
      "planner → Qwen3.8-27B",
      "triage → DeepSeek-V4 Flash",
      "embed → BGE-M3",
      "rerank → BGE Reranker v2 M3",
    ]);
    const voters = screen.getByRole("region", { name: copy.votersHeading });
    expect(within(voters).getByText("2 voters from 2 model families.")).toBeTruthy();
    expect(within(voters).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["DeepSeek-V4 Flash (DeepSeek)", "Qwen3.8-27B (Qwen)"]);
    expect(screen.getByText(copy.readOnly)).toBeTruthy();
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
});
