# Models — copy (CLAUDE.md §7, §9)

Which model serves each role, and the cross-check voters. Read from `Models/models.yaml` on
every request (`GET /api/v1/models`, docs/api-contract.md); read-only in round 1. Swapping and
rolling back arrive with the Models service. Component: `apps/webui/src/models/ModelsPage.tsx`;
copy in `apps/webui/src/copy/en.ts` (`models`).

## Page

| Element | Text |
|---|---|
| Heading | Models |
| Lede | Which model serves each role, and the cross-check voters. |
| Registry sentence (from the api) | *4 models; coder → Qwen3.8-27B, planner → Qwen3.8-27B, triage → DeepSeek-V4 Flash, embed → BGE-M3, rerank → BGE Reranker v2 M3; 2 voters from 2 model families.* |
| Footer | Swapping and rolling back models arrives with the Models service; until then, edit Models/models.yaml on the host and run `slas model fit` before a load. |

**States**

| State | Text |
|---|---|
| Loading | Loading models… |
| Failed to load | The model registry didn't load. / The api service didn't answer. / Press Try again; if it repeats, run `slas logs api` on the host. |
| Registry problem (api `problem`) | the api's three parts, shown instead of the cards |

## Models on this installation (one card per model)

| Line | Example |
|---|---|
| Name | DeepSeek-V4 Flash |
| Presence pill | weights present · weights not here yet |
| Family and quantisation | DeepSeek family · FP8 *(FP8 · AWQ 4-bit · BF16)* |
| Size | 180 GiB of GPU memory · 131,072 tokens of context |
| Roles | may serve triage, planner *(or: serves no role)* |
| Empty | No models are listed. Fix Models/models.yaml on the host; the format is in services/model-manager/README.md. |

## Who serves each role

One line per role: *coder → Qwen3.8-27B*. Empty: No role is assigned yet.

## Cross-check voters

Sentence: *2 voters from 2 model families.* then one line per voter: *DeepSeek-V4 Flash
(DeepSeek)*. Empty: No voters are configured, so cross-checks run as a single model and are
flagged.
