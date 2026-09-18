# Models — copy (CLAUDE.md §7, §9; ADR-0018)

Which model serves each role, the cross-check voters, and adding a model from a link. The
registry is read from `Models/models.yaml` on every request (`GET /api/v1/models`); "Add a
model" talks to the model fetcher (`/api/v1/models/fetches`, quickstart only) and the Roles
and Voters panels save through the model manager (`PUT /api/v1/models/roles`). Both forms need
`model:manage`; without it they are disabled and say why. Component:
`apps/webui/src/models/ModelsPage.tsx`; copy in `apps/webui/src/copy/en.ts` (`models`).

## Page

| Element | Text |
|---|---|
| Heading | Models |
| Lede | Which model serves each role, the cross-check voters, and adding a model from a link. |
| Registry sentence (from the api) | *4 models; coder → Qwen3.8-27B, planner → Qwen3.8-27B, triage → DeepSeek-V4 Flash, embed → BGE-M3, rerank → BGE Reranker v2 M3; 2 voters from 2 model families.* |
| Footer | Changes save to Models/models.yaml on the host; the model manager starts and stops instances to match within about 30 seconds, no restart needed. |

**States**

| State | Text |
|---|---|
| Loading | Loading models… |
| Failed to load | The model registry didn't load. / The api service didn't answer. / Press Try again; if it repeats, run `slas logs api` on the host. |
| Registry problem (api `problem`) | the api's three parts, shown instead of the cards |

## Add a model (one panel, one primary action)

| Element | Text |
|---|---|
| Heading | Add a model |
| Link field | Paste a Hugging Face link — placeholder *https://huggingface.co/Qwen/Qwen3.8-27B-FP8* |
| Id field | Registry id (optional) — help: Empty means the repository name in lowercase, made unique. |
| What will happen | The weights download onto this host through the model fetcher and the model appears on this page; give it a role to start it. |
| Button | Download and import *(Starting… while the request is out; disabled until a link is pasted)* |
| Without `model:manage` | Adding a model needs the model:manage capability. Ask an administrator to add it, or to give you a role that includes it. |
| Refused link, taken id, hub problem | the api's three parts (the accepted forms are named) |
| Not started | The download didn't start. / The api service didn't answer. / Press Download and import again; … |

**Downloads** (one line per fetch, newest first, polled every 3 s while one runs): the
model's name, a pill (Planning · Downloading · Importing · Done · Failed · Cancelled), a
progress bar labelled *12.4 GiB of 29.0 GiB downloaded*, and the fetcher's sentence:

- *Downloading Qwen3.8-27B-FP8: 12.4 GiB of 29.0 GiB, 3 of 9 files.*
- *Qwen3.8-27B-FP8 is here (29.0 GiB, 9 files) and registered as qwen3.8-27b-fp8; give it a role on this page to start it. Its GPU memory is estimated at 37 GiB from the file sizes; correct it in the registry if you know better.*
- a failed fetch shows its three parts in place of the sentence.

Buttons: **Cancel** on a running fetch (stops after the current file; the files stay and the
same link resumes), **Remove** on a finished one (the record only; the files stay). The
answer is one sentence: *Removed the record of Qwen3.8-27B-FP8; the files stay.* When a
fetch lands, the registry is re-read and the new card appears with *serves no role*.

## Models on this installation (one card per model)

| Line | Example |
|---|---|
| Name | DeepSeek-V4 Flash |
| Presence pill | weights present · weights not here yet |
| Family and quantisation | DeepSeek family · FP8 *(FP8 · AWQ 4-bit · BF16)* |
| Size | 180 GiB of GPU memory · 131,072 tokens of context |
| Roles | may serve triage, planner *(or: serves no role)* |
| Empty | No models are listed. Fix Models/models.yaml on the host; the format is in services/model-manager/README.md. |

## Who serves each role (a form with the voters)

One select per role — coder, planner, triage, embed, rerank — listing every model by name;
a model whose weights are not here reads *BGE Reranker v2 M3 (weights not here yet)* and
cannot be picked; *— no model —* unassigns the role. Empty registry: No role is assigned yet.

## Cross-check voters

A checkbox per model whose weights are here, labelled *DeepSeek-V4 Flash (DeepSeek)*, under
the sentence *2 voters from 2 model families.* (or, with none ticked: No voters are
configured, so cross-checks run as a single model and are flagged.) Help: Pick models from
different families so their errors decorrelate (CLAUDE.md §5.3).

**Save roles** is the one button for both panels (Saving… while out; disabled until
something differs from the saved registry). The answer is the model manager's sentence, for
example *Saved: planner → DeepSeek-V4 Flash. 2 voters from 2 families. To match the registry:
start vllm-planner; stop vllm-planner.*, and the registry is re-read. A refusal (a model
without weights, a swap in flight) is shown in three parts and nothing moves. Without
`model:manage`: Changing roles and voters needs the model:manage capability. Ask an
administrator to change them, or to give you a role that includes it.
