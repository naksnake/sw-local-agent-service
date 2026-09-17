# SOP — Setting up and running SW Local Agent Service

Version 1.1 · 2026-09-16 · Chinese twin: `setup-and-operations-sop.zh-Hant.md` (INV-13).
Written against branch `claude/vigilant-gauss-tsqua0`. Every "works today" line is covered by a
test or was run while writing; every "waits" line names what it waits on.

## 1 · Purpose and scope

This SOP takes an operator from an empty GPU host to a running installation and through daily
use: starting work, approving destructive steps, watching a station, changing a model, backing
up and restoring. It covers the HGX B300 NVL8 reference host; other hosts differ only in the GPU
layout of §6.

Two states apply to every step:

| Mark | Meaning |
|---|---|
| **Today** | runs on this branch, tested |
| **Waits** | blocked until the three dependency decisions in §2 are taken |

## 2 · What runs today and what waits

| Piece | State | Waits on |
|---|---|---|
| `slas doctor`, `slas status`, `slas toolchain`, `slas target`, `slas backup` | Today | — |
| WebUI in the demo's shell, all pages on API fakes | Today | sign-in and live data wait on `apps/api` |
| Station runner on a physical test station, mTLS enrolment | Today | — |
| Kernel, skills, HAL, executors, gateway, model manager, Git broker, observability | Today, against fakes | — |
| Model weights fetched on a connected host and verified offline | Today | — |
| `docker compose up` of the platform stack from a source checkout on a connected quickstart host (`./install.sh --build`, ADR-0014) | Today | the signed bundle for prod still waits on a release host |
| vLLM instances started by the model manager | Waits | the Podman driver's dependency approval |
| Sign-in, people, Postgres tickets | Waits | the `apps/api` stack (ADR-0005) |

The two waiting rows are dependency approvals nobody has answered. Until they are, a connected
quickstart host runs the stack from source (`./install.sh --build`, ADR-0014) with sign-in,
Home, People, Settings and Models behind the edge and every other service answering health
only; an air-gapped host is a development and station-runner host. §10 says how to use it
that way.

## 3 · Roles

| Role | Does | Needs |
|---|---|---|
| Build-host operator | fetches weights, pins and signs images, builds the bundle | a connected host, the release key |
| Platform administrator | prepares the host, installs, manages people, stations, Git hosts | root on the platform host, `admin:*` |
| Validation engineer | uploads suites, approves destructive steps, reviews findings | `redfish`, `ssh`, `approve:destructive` |
| Line lead | starts factory jobs, decides PASS or FAIL when the voters disagree | `factory:verdict`, `factory:control` |
| Developer | runs coding tasks, pushes through the Git broker | `files`, `git:push_branch` |

## 4 · Prerequisites

Platform host: 8 GPUs at about 288 GB, NVLink up; Docker; rootless Podman with `uidmap`; gVisor
(`runsc`); the NVIDIA container toolkit; cgroups v2; at least 200 GiB at `/AI/Agent` and a
separate volume of at least 1.5 TB for `Models/`; port 443 open, nothing else.

Files to carry in: the release bundle `slas-bundle-<version>.tgz`, `config/cosign.pub`, the
`models/` directory from §5. Nothing downloads on the platform host (INV-1).

## 5 · Procedure A — On the connected build host

| # | Do | Done when |
|---|---|---|
| A1 | `git clone` the repository; `uv sync --frozen && uv run pytest` | every test passes |
| A2 | `config/model-sources.txt` ships filled in, every line pinned to a commit; change a line only to pick another build | every line names a repository and a commit |
| A3 | `scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models`, then the same without `--dry-run`; `export HF_TOKEN=…` only for gated repositories | "Total: 7 models, …" and the free disk, then one sentence per model and `models/manifest.json` |
| A4 | Rerun A3 after any interruption; it resumes and keeps complete files | "Done: N models" |
| A5 | Waits: `scripts/lock-images.sh --sign` then `scripts/build-bundle.sh --profile prod`; commit the filled lock | `compose/images.lock.*` has no null digest |
| A6 | Copy `models/`, the bundle and `config/cosign.pub` to the sneakernet disk | checksums recorded |

## 6 · Procedure B — On the platform host

| # | Do | Done when |
|---|---|---|
| B1 | `./install.sh --preflight-only` | no ✗ line; every ! line understood |
| B2 | Mount the data and model volumes; `mkdir -p /AI/Agent/Models` | preflight Disk space ✓ |
| B3 | Copy `models/` next to `install.sh`, or anywhere and pass `--models DIR`; `./install.sh --profile prod --models-only --dry-run` says what will be copied | "Would copy 7 models (…) … into /AI/Agent/Models" |
| B4 | `./install.sh --profile prod --models-only`: verifies the checksums, places the weights and writes `/AI/Agent/Models/models.yaml` from `config/models.prod.yaml` when there is none; it never overwrites one. Edit roles later on the Models page. Works before the bundle exists | "Placed 7 models under /AI/Agent/Models" and "Wrote /AI/Agent/Models/models.yaml from the prod template." |
| B5 | Waits: `tar xzf slas-bundle-<version>.tgz && cd slas-bundle-<version>`; `./install.sh --profile prod --dry-run` | "Dry run finished: every read-only step passed" |
| B6 | Waits: `./install.sh --profile prod` | the sign-in URL and one-time administrator password print |
| B7 | Waits: sign in, choose a new password; Admin → People; Admin → Git hosts | first person added |
| B8 | Admin → Stations → add a station → Issue code; enter the code on the station's runner (`deploy/station-runner/`) | the station shows "enrolled" |

GPU layout for the B300 (eight GPUs, about 288 GB each):

| GPUs | Instance | Serves |
|---|---|---|
| 0–3 | DeepSeek-V4 Pro, tensor parallel 4 | planner |
| 4 | DeepSeek-V4 Flash | triage; stand-in planner during a Pro swap |
| 5 | Qwen3.8-27B FP8 ×2, BGE-M3, BGE reranker, Qwen3.8-27B BF16 | coder, voter, embed, rerank, eval reference |
| 6 | DeepSeek-V4 Flash (second instance) | voter |
| 7 | MiniMax-M2.7 | voter, the third family |

`quant: fp4` waits on an ADR; until then the DeepSeek entries validate as `fp8`. The model
manager starts one instance per role and one per voter (CLAUDE.md §15, decision 14).

## 7 · Procedure C — Daily operation

**Home** shows what needs you (three-part notices), what is running, and recent results. The
health sentence at the top counts running jobs and items that need a person.

| Task | Steps | What happens |
|---|---|---|
| Start a coding task | Home → New coding task → drop `plan.md` → pick languages → review steps → Start task | ticket `T-coding-n`; the agent commits on its own branch in a sandbox; the diff is cross-checked by three voters; ZIP always, Git push through the broker on request |
| Start a validation run | Home → New validation run → upload the suite → pick a free target → Approve and start | ticket `T-validation-n`; destructive steps wait for your approval; cycles show on the LED map with the console |
| Approve a destructive step | Home notice → Open run → read the plan → Approve | the run starts; the approval is journalled with your name |
| Start a factory job | Home → New factory job → pick the MES ticket or scan the label → template → Start job | ticket `T-factory-n`; every GUI step is screenshot before and after; PASS needs 3 of 3 voters |
| Decide when voters disagree | Factory → the held job → PASS or FAIL with a note | the station is released or held; MES receives the verdict |
| Watch or take over a station | Factory → the job → Watch station · Take over | VNC through the enrolled station's runner; Resume or Abort |
| Read a result | Home → Recent results → the row | the ticket, its SOP in English and Chinese, and the exports |
| Check the platform | `slas status` | services, GPUs, models, work, alerts, in sentences |

Rules that never bend: a destructive step needs a per-run approval (INV-7); a unanimous vote is
input to a person, never the approval (INV-11); the model never controls hardware (INV-3).

## 8 · Procedure D — Models

| Task | Do | Done when |
|---|---|---|
| Check a fit before a load | `slas model fit <id>` (waits) or the fit sentence in the Models page | "…so it fits." |
| Change a role's model | Models → the role → Swap → pick the candidate | "coder is now served by X. Y can be restored until <time>." |
| Roll back | Models → the role → Restore, within 24 hours | "Rolled back: Y is serving coder again." |
| Swap the planner (Pro) | route planner to Flash; stop Pro; start the new Pro; smoke test; route back | planner served by the new Pro |
| Edit by hand | `roles:` in `/AI/Agent/Models/models.yaml`; each role's model must list that role | the registry validates in one sentence |

Quantisation: FP8 or FP4 on Blackwell, AWQ 4-bit on Ada and Ampere, one BF16 copy only for
eval regression (CLAUDE.md §7). Voters should come from three model families; prod ships DeepSeek,
Qwen and MiniMax, quickstart two families (CLAUDE.md §15, decision 12).

## 9 · Procedure E — Backups, restore, upgrade

| Task | Do | Done when |
|---|---|---|
| Back up now | `slas backup now --type full` | the archive check passes |
| Restore drill | `slas backup drill --to <time>`; check by hand; record the RTO row | a row in `docs/runbooks/restore-drill.md` |
| Restore for real | `slas backup restore --to <time> --yes` | services healthy; the sign-in page answers |
| Upgrade | unpack the new bundle; `./install.sh --profile prod` again | idempotent; only changed services restart |

Nightly backups and Qdrant snapshots are automatic in quickstart; prod adds pgBackRest PITR
under object lock (`docs/runbooks/prod-profile.md`).

## 10 · Development mode, today

```bash
git clone <repository> && cd sw-local-agent-service
uv sync --frozen && uv run pytest              # everything against fakes
cd apps/webui && pnpm install && pnpm dev      # WebUI on 127.0.0.1:5173 with the API fakes
uv run python -m slas_cli doctor               # the preflight against this host
```

The dev server binds loopback only; reach it through an SSH tunnel. Station enrolment (B8)
works today against the executor's enrolment server.

## 11 · Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Preflight ✗ Container runtime | Docker stopped or your user not in the `docker` group | `sudo systemctl enable --now docker`; add the user; log in again |
| Preflight ✗ GPU | no NVIDIA driver | install the driver, reboot, run again |
| Install prints "Nothing was changed on this host." | a read-only step failed: signature, lock, manifest | read the sentence above it; fix; run again |
| "not pinned … scripts/lock-images.sh" | the image lock shipped unfilled | run A5 on the build host, commit, rebuild the bundle |
| `verify` names a file | copy error on the sneakernet disk | copy that file again, verify again |
| "The hub refused … (401)" | gated repository, no token | accept the licence, `export HF_TOKEN`, rerun |
| "… is not turned on for the Factory Agent." | the skill's switch is off here (ADR-0013) | Skills → turn it on for Factory; the next job uses it |
| "… is waiting for your approval." | a destructive step in the plan | Home notice → Open run → Approve, or remove the step |
| Station shows "not enrolled yet" | the code was not entered, or it expired after 15 minutes | Issue a new code; enter it on the runner |

## 12 · References

`CLAUDE.md` (invariants, §3 deployment, §7 models, §9 UI) · `docs/runbooks/deploy-hgx-b300.md`
(host detail and GPU layout) · `docs/runbooks/prod-profile.md` · `docs/runbooks/restore-drill.md`
· `docs/runbooks/station-runner.md` · `docs/adr/0013-skill-enablement-record.md` ·
`config/model-sources.txt` · `config/models.quickstart.yaml` · `config/models.prod.yaml` ·
`scripts/fetch_models.py`.
