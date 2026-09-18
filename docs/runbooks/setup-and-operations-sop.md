# SOP — Setting up and running SW Local Agent Service

Version 1.3 · 2026-09-17 · Chinese twin: `setup-and-operations-sop.zh-Hant.md` (INV-13).
1.2 adds §7, what runs after a round-2 install (ADR-0015): every service on HTTP, the vLLM
instances the model manager starts, the runtime-socket choice, the sandbox images, and the
first coding task.
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
| Sign-in, people, settings, Postgres tickets (`apps/api`, ADR-0005) | Today | — |
| Every service on its own HTTP surface; the agents startable from the wizards (ADR-0015, round 2) | Today, as each service's slice lands | `docs/api-contract-round-2.md` is the contract; §7 says what to expect |
| vLLM instances started by the model manager over the runtime socket (Docker or Podman) | Today, with round 2 | the pinned `vllm/vllm-openai` image is pulled by `./install.sh --build` |

A connected quickstart host runs the whole stack from source (`./install.sh --build
--fetch-models`, ADR-0014 and ADR-0015); §7 walks through what is up afterwards. An air-gapped
host is a development and station-runner host until the signed bundle exists; §11 says how to
use it that way.

## 3 · Roles

| Role | Does | Needs |
|---|---|---|
| Build-host operator | fetches weights, pins and signs images, builds the bundle | a connected host, the release key |
| Platform administrator | prepares the host, installs, manages people, stations, Git hosts | root on the platform host, `admin:*` |
| Validation engineer | uploads suites, approves destructive steps, reviews findings | `redfish`, `ssh`, `approve:destructive` |
| Line lead | starts factory jobs, decides PASS or FAIL when the voters disagree | `factory:verdict`, `factory:control` |
| Developer | runs coding tasks, pushes through the Git broker | `files`, `git:push_branch` |

## 4 · Prerequisites

Platform host: 8 GPUs at about 288 GB, NVLink up; Docker (or rootless Podman with `uidmap`
and its socket enabled) — the preflight says which engine serves the runtime socket, whether
gVisor (`runsc`) is registered with it and whether the NVIDIA runtime answers; the NVIDIA
container toolkit; cgroups v2; at least 200 GiB at `/AI/Agent` and a separate volume of at
least 1.5 TB for `Models/`; port 443 open, nothing else.

Files to carry in: the release bundle `slas-bundle-<version>.tgz`, `config/cosign.pub`, the
`models/` directory from §5. Nothing downloads on the platform host (INV-1).

## 5 · Procedure A — On the connected build host

| # | Do | Done when |
|---|---|---|
| A1 | `git clone` the repository; `uv sync --frozen && uv run pytest` | every test passes |
| A2 | `config/model-sources.txt` ships filled in, every line pinned to a commit; change a line only to pick another build | every line names a repository and a commit |
| A3 | `scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models`, then the same without `--dry-run`; `export HF_TOKEN=…` only for gated repositories | "Total: 7 models, …" and the free disk, then one sentence per model and `models/manifest.json` |
| A4 | Rerun A3 after any interruption; it resumes a cut file and keeps complete ones. A model that finished earlier is recognised from `manifest.json` and its checksum file and is neither listed on the hub nor hashed again | "Done: N models", or "already complete … nothing to download" |
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

## 7 · Round 2 — what runs after `./install.sh --build --fetch-models`

The connected quickstart install (ADR-0014, ADR-0015; `docs/api-contract-round-2.md`). What
`./install.sh --build` does, in order, after the preflight and the read-only checks:

| # | Step | You see |
|---|---|---|
| R1 | pulls every third-party image by its pinned tag — the vLLM image `vllm/vllm-openai:v0.29.0-x86_64-cu129` by the digest the lock records — and retags them `local/…`; builds every first-party image from `images/<name>/Dockerfile` | one "Pulled …" or "Built …" sentence per image; the filled lock at `/AI/Agent/images.lock.json` |
| R2 | asks the sandbox manager for its image list (`python -m slas_sandbox_manager.images list`), builds each `images/sandbox-<language>/Dockerfile` from the repository root, records their IDs in `/AI/Agent/sandbox-images.lock.json` and writes `/AI/Agent/Toolchains/manifest.json`, the languages and versions those images carry | "Built local/slas/sandbox-python:… ", "Wrote the toolchain manifest to …" |
| R3 | chooses the runtime socket: Docker's `/var/run/docker.sock` when it exists (the images live in Docker's store), else Podman's `/run/podman/podman.sock`, written to `.env` as `SLAS_RUNTIME_SOCKET` | "Docker's socket /var/run/docker.sock serves the container runtime, so SLAS_RUNTIME_SOCKET=/var/run/docker.sock in .env points model-manager and sandbox-manager at it (the images install.sh builds and loads live in Docker's store); nothing else sees it (INV-4)." |
| R4 | writes `.env` and the secret files; creates the data directories the services bind-mount, as your user: `Coding`, `Toolchains`, `.git-broker`, `Tickets`, `Skills/library`, `SOP`, `Validation`, `Factory/{Templates,mes/inbox,ca}`, `Models`, `Knowledge`, `Backups/stations`, `qdrant`, `tls` | "Created N data directories under /AI/Agent as uid …" |
| R5 | places the weights, `docker compose up -d --pull never`, waits for health, prints the sign-in URL | "SW Local Agent Service is up." |

**Which agents start (ADR-0017).** By default the install starts the **Coding Agent** only:
`SLAS_AGENTS=coding` in `.env`; no `validation-executor`, `factory-executor`, `vector-db` or
`local-search-api` container; the WebUI shows Coding, Runs, Models, Skills and Admin.
Validation, Factory and the knowledge base (Qdrant, local search) are built and tested but
off; `./install.sh --build --agents coding,validation,factory,knowledge` (or `SLAS_AGENTS`)
builds their images, starts their compose profiles and adds their pages. Run it
again with a different list to change the choice; nothing else in the install moves.

**The services.** Every service is one container serving HTTP on port 8000 inside the stack:
`api` (the only one behind the edge), `agent-core-orchestrator` (the kernel and the three
agents), `llm-gateway`, `model-manager`, `sandbox-manager`, `git-broker`,
`validation-executor`, `factory-executor`; `screen-worker` and `local-search-api` answer
health only until their rounds. They find each other through the `SLAS_*_URL` variables
compose sets (`http://<service>:8000`), on the internal `slas-backend` network. `slas status`
and `docker compose -p slas ps` list them; `docker compose -p slas logs <service>` reads one.

**Where the vLLM instances appear.** They are not compose services. The model manager reads
`/AI/Agent/Models/models.yaml`, and for every role and every voter creates a container from
`SLAS_VLLM_IMAGE` over the runtime socket: `vllm-coder`, `vllm-planner`, `vllm-triage`,
`vllm-embed`, `vllm-rerank`, and `vllm-voter-<model id>` for each voter, all on the
`slas_slas-inference` network (internal, no egress), with `/AI/Agent/Models` mounted
read-only and the GPUs the placement assigns (`SLAS_GPU_VRAM_GIB`, 270 GiB per GPU by
default). `docker ps --filter label=slas.kind=vllm` lists them; the Models page and
`GET /v1/status` on the model manager say the state of each in a sentence; an instance that
does not fit is reported, never started. Prometheus scrapes them by those names.

**The Docker socket.** On a host that has Docker the installer writes
`SLAS_RUNTIME_SOCKET=/var/run/docker.sock` and says so: the stack runs on `docker compose`, so
the images `./install.sh --build` builds or `docker load` loads live in Docker's store, and a
sandbox or vLLM container asked of Podman's socket on such a host fails with "image not known".
Rootless Podman's socket stays the compose default and serves a host without Docker. Only
`model-manager` and `sandbox-manager` mount the socket, never a service that runs
model-authored or skill-authored steps (INV-4). Set `SLAS_RUNTIME_SOCKET` yourself before the
install to name another path (rootless Docker, a rootless Podman socket under
`/run/user/<uid>/podman/`); a value already in `.env` is kept. The preflight says which engine
answers on it: "Docker Engine 29.0.1 serves the container-runtime socket /var/run/docker.sock
…" or "Podman 4.9.3 serves …".

**Sandbox images.** One image per language (`local/slas/sandbox-<language>:<version>`), built
in R2 from a pinned upstream toolchain image or package, with `git` and `slas-check`, no
credential helper and no network (INV-14). The sandbox manager starts them by tag over the
runtime socket, with gVisor (`runsc`) when the engine registers it and hardened `runc`
otherwise — the preflight and the sandbox manager's health say which. `slas toolchain list`
shows the manifest R2 wrote.

**The first coding task.**

| # | Do | What happens |
|---|---|---|
| C1 | Sign in; Home → New coding task; drop or paste a `plan.md`; give the task a name | the languages are detected from the plan (`/api/v1/coding/languages/detect`) |
| C2 | Setup: keep or change the languages; leave every version empty | the sentence names the newest bundled version per language, from the manifest of R2 |
| C3 | Review the proposed steps; **Start task** | ticket `T-coding-n`; the sandbox manager prepares `/AI/Agent/Coding/<you>/Projects/<slug>` (`git init` with your identity) and opens a sandbox on the resolved image |
| C4 | Watch the checklist and the feed | the first feed line names the toolchain; each step runs `slas-check …` inside the sandbox; the agent commits on its own branch |
| C5 | Read the result | the walkthrough in English and Chinese, the ZIP under `Artifacts/`, and the Git panel: commit, history, "Push to <remote>" through the broker once a remote is saved under Settings → Git remotes |

If the task stops with "no instance serves the role coder yet", the model manager has not
finished starting `vllm-coder`: the Models page shows it as starting, with the fit sentence
when it will never fit.

## 8 · Procedure C — Daily operation

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

## 9 · Procedure D — Models

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

## 10 · Procedure E — Backups, restore, upgrade

| Task | Do | Done when |
|---|---|---|
| Back up now | `slas backup now --type full` | the archive check passes |
| Restore drill | `slas backup drill --to <time>`; check by hand; record the RTO row | a row in `docs/runbooks/restore-drill.md` |
| Restore for real | `slas backup restore --to <time> --yes` | services healthy; the sign-in page answers |
| Upgrade | unpack the new bundle; `./install.sh --profile prod` again | idempotent; only changed services restart |

Nightly backups and Qdrant snapshots are automatic in quickstart; prod adds pgBackRest PITR
under object lock (`docs/runbooks/prod-profile.md`).

## 11 · Development mode, today

```bash
git clone <repository> && cd sw-local-agent-service
uv sync --frozen && uv run pytest              # everything against fakes
cd apps/webui && pnpm install && pnpm dev      # WebUI on 127.0.0.1:5173 with the API fakes
uv run python -m slas_cli doctor               # the preflight against this host
```

The dev server binds loopback only; reach it through an SSH tunnel. Station enrolment (B8)
works today against the executor's enrolment server.

## 12 · Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Preflight ✗ Container runtime | Docker stopped or your user not in the `docker` group | `sudo systemctl enable --now docker`; add the user; log in again |
| Preflight ✗ GPU | no NVIDIA driver | install the driver, reboot, run again |
| The browser cannot open `https://<ip>` (TLS error, connection reset) | the edge answers only to the names in `SLAS_TLS_NAMES`; an install before 2026-09-18 listed the host's name only | `git pull && ./install.sh`: the sign-in URL becomes the host's address on the default route and the certificate covers every address of the host |
| The host's DHCP address changed and the old URL no longer answers | `SLAS_PUBLIC_HOST` and the certificate name the old address | `./install.sh` again: it refreshes the address (unless `SLAS_PUBLIC_HOST_PINNED=yes`) and recreates the edge; a fixed DHCP reservation for the host avoids the repeat |
| Install prints "Nothing was changed on this host." | a read-only step failed: signature, lock, manifest | read the sentence above it; fix; run again |
| "not pinned … scripts/lock-images.sh" | the image lock shipped unfilled | run A5 on the build host, commit, rebuild the bundle |
| `verify` names a file | copy error on the sneakernet disk | copy that file again, verify again |
| "The hub refused … (401)" | gated repository, no token | accept the licence, `export HF_TOKEN`, rerun |
| "… is not turned on for the Factory Agent." | the skill's switch is off here (ADR-0013) | Skills → turn it on for Factory; the next job uses it |
| "… is waiting for your approval." | a destructive step in the plan | Home notice → Open run → Approve, or remove the step |
| Station shows "not enrolled yet" | the code was not entered, or it expired after 15 minutes | Issue a new code; enter it on the runner |
| Preflight ✗ Runtime socket: "No container-runtime socket was found at …" | neither Podman's socket nor Docker's exists | `systemctl --user enable --now podman.socket`, or install Docker; set `SLAS_RUNTIME_SOCKET` for another path |
| Preflight ! gVisor runtime: "runsc is not registered …" | gVisor not installed or not registered with the engine | `sudo runsc install && sudo systemctl restart docker`; quickstart goes on with hardened runc |
| Preflight ✗ NVIDIA runtime | the toolkit is installed but not configured for the engine | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| "The sandbox image list could not be read …" | this checkout's sandbox manager does not ship `images list` | update the checkout; `uv sync --frozen`; `./install.sh --build` again |
| `model-manager` or `sandbox-manager` unhealthy: "runtime: down" | the socket in `SLAS_RUNTIME_SOCKET` is not the one the engine serves, or the engine stopped | check `.env`, `ls -l` the socket, restart the engine, `docker compose -p slas up -d` |
| Coding task: "no instance serves the role coder yet" | `vllm-coder` is still starting, or does not fit | Models page: wait for "healthy", or pick a smaller build for the role |

## 13 · References

`CLAUDE.md` (invariants, §3 deployment, §7 models, §9 UI) · `docs/api-contract-round-2.md`
(the round-2 service contract) · `docs/adr/0014-build-from-source-on-a-connected-host.md` ·
`docs/adr/0015-service-http-surfaces-and-runtime-socket-driver.md` ·
`docs/runbooks/deploy-hgx-b300.md` (host detail and GPU layout) · `docs/runbooks/prod-profile.md`
· `docs/runbooks/restore-drill.md` · `docs/runbooks/station-runner.md` ·
`docs/adr/0013-skill-enablement-record.md` · `config/model-sources.txt` ·
`config/models.quickstart.yaml` · `config/models.prod.yaml` · `scripts/fetch_models.py`.
