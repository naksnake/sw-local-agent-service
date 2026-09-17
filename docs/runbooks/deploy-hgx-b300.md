# Deploying to an NVIDIA HGX B300 NVL8 host

This runbook says, in order, what you can run on the box today, what the full stack still
waits on, and the steps for both. It is written against the code on this branch; every
"works today" claim below is covered by a test or was run while writing it. Nothing here
relaxes an invariant: no download at runtime (INV-1); images arrive in the bundle and
weights in a directory fetched on a connected host (§2).

## 0 · What runs today, what does not

| Piece | State | Where it stands |
|---|---|---|
| `slas doctor` preflight, `slas status`, `slas toolchain`, `slas target`, `slas backup` | runs | stdlib CLI, no virtualenv needed |
| WebUI in the demo's shell (`docs/ui-demo/slas-ui-demo.html`): rail, health line, Home dashboard; Coding · Validation · Factory with their three-step wizards; Settings (Git remotes); Admin (Git hosts · Stations); Runs · Models · Skills say when they arrive | runs on API fakes | `pnpm dev`; sign-in and the live API wait on `apps/api` (ADR-0005 dependency approval) |
| Station runner on a physical test station | runs | `deploy/station-runner/`, offline wheels, mTLS enrolment |
| Kernel (with the ADR-0013 skill gate), skills, HAL, executors, gateway, model manager, git broker, observability | run against fakes, the Python unit suite (`uv run pytest`), 28 WebUI tests | Python packages in this repository |
| Model weights: fetched on a connected host from the pinned `config/model-sources.txt`, verified and placed under `Models/` with `models.yaml` by `./install.sh --models DIR --models-only` | runs | `scripts/fetch_models.py`, `config/models.<profile>.yaml`, §2 |
| `docker compose up` of the platform stack from this checkout on a **connected** quickstart host: `./install.sh --build` builds every first-party image (`images/<name>/Dockerfile`, bases by digest), pulls the third-party ones by their pinned tags, writes the filled lock to `/AI/Agent/images.lock.json` and starts the stack | runs (ADR-0014) | §3; the signed offline bundle for prod still waits on a release host and the vendored caches |
| Behind the edge once the stack is up: sign-in, Home, Admin → People, Admin → Settings, Models (the `api` and `webui` images build `apps/api` and `apps/webui`) | runs with the api and WebUI rounds | `docs/api-contract.md`; the api image needs `apps/api` in the uv workspace as `slas-api` |
| The other services (orchestrator, gateway, model manager, sandbox manager, git broker, executors) inside the stack, each on its own HTTP surface, the agents startable from the wizards | round 2 (ADR-0015), as each service's slice lands | `docs/api-contract-round-2.md`; each container's CMD is `<script> serve`; `local-search-api` and `screen-worker` stay health-only until their rounds |
| vLLM instances started by the model manager over the runtime socket (`vllm-<role>`, `vllm-voter-<model id>` on `slas_slas-inference`) | round 2 | the Engine API driver (`packages/slas-container`) replaces the Podman CLI; the pinned `vllm/vllm-openai:v0.29.0-x86_64-cu129` image is pulled by `--build` and handed to the model manager as `SLAS_VLLM_IMAGE` |
| Coding Agent sandbox images built on the connected path, the toolchain manifest they satisfy | round 2 | `--build` asks the sandbox manager for its image list and builds each from the repository root |

So: today the box can be prepared, checked, brought up from source with `--build`, and
signed in to; the agents' services come alive round by round inside the running stack. The
air-gapped bundle install stays the release path (§3). The operator SOP's §7
(`setup-and-operations-sop.md`) lists what is up after a round-2 install and the first coding
task.

## The procedure, in order

Tick each line; every command below is explained in the section it points to.

| # | Where | Do | Done when | Section |
|---|---|---|---|---|
| 1 | B300 host | `nvidia-smi` shows 8 GPUs, NVLink up; install Docker, rootless Podman + `uidmap`, gVisor, the NVIDIA container toolkit; boot with cgroups v2 | `./install.sh --preflight-only` shows no ✗ | §1 |
| 2 | B300 host | Mount ≥ 200 GiB at `/AI/Agent`; a separate volume for `Models/` (≥ 1.5 TB for the plan) | `slas doctor` Disk space is ✓ | §1 |
| 3 | Connected host | `scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models`, then the same without `--dry-run` | "Total: 7 models, …" then one sentence per model and `models/manifest.json` | §2 |
| 4 | Sneakernet → B300 | Copy `models/` next to `install.sh` (or anywhere, and pass `--models DIR`); `./install.sh --profile prod --models-only` verifies the checksums, places the weights under `/AI/Agent/Models/` and writes `models.yaml` from `config/models.prod.yaml` | "Placed 7 models under /AI/Agent/Models" and "Wrote … from the prod template." | §2, §4 |
| 5 | B300 host (connected, quickstart) | `./install.sh --build --fetch-models --dry-run`, then the same without `--dry-run` (ADR-0014) | "SW Local Agent Service is up. Sign in at https://…" and the one-time password while the api says the bootstrap is pending | §3a |
| 6 | Connected build host (prod) | `scripts/lock-images.sh --sign`, `scripts/build-bundle.sh --profile prod`; commit the filled lock | `compose/images.lock.*` has no null digest | §3b |
| 7 | Sneakernet → B300 | Carry `slas-bundle-<version>.tgz` and `config/cosign.pub` | both files on the host | §3b |
| 8 | B300 host | `./install.sh --profile prod --dry-run`, then `./install.sh --profile prod` | the sign-in URL and one-time admin password are printed | §3b |
| 9 | Browser | Sign in, change the password, Admin → People, Admin → Stations → Issue code | first station enrolled | §5 |
| 10 | B300 host | `slas backup drill` once; record the RTO | a row in `docs/runbooks/restore-drill.md` | prod runbook |

Steps 1 to 5 can be done today (step 5 is the connected quickstart path; step 4 alone with
`--models-only` when the weights arrive before the host is connected). Steps 6 to 10 are
the prod path and wait on a release host with the release key and the vendored caches.

## 1 · Prepare the host

1. Confirm the GPUs and NVLink:

   ```bash
   nvidia-smi --query-gpu=index,name,memory.total --format=csv
   nvidia-smi nvlink --status | head
   ```

   Expect 8 GPUs at about 288 GB each. Write the numbers into the GPU layout below if they
   differ.
2. Install Docker (rootful, for the platform stack), rootless Podman with `uidmap` (for
   Coding Agent sandboxes), gVisor (`runsc`), the NVIDIA container toolkit, and boot with
   cgroups v2. The preflight names each one that is missing and what to do.
3. Mount at least 200 GiB at `/AI/Agent` (the data root). Model weights for the plan below
   are about 1.2 TiB (1.3 TB) on top of that; put `Models/` on its own volume.
4. Open port 443 on the host; nothing else is published.
5. Run the preflight from a source checkout or the bundle:

   ```bash
   ./install.sh --preflight-only          # or: python3 -m slas_cli doctor
   ```

   Every line is ✓, ! or ✗ with what happened, the likely cause and what to do. ✗ stops the
   install; ! does not.

## 2 · Bring the weights

The running platform never downloads anything (INV-1). `scripts/fetch_models.py` does the
fetching on a connected build host: standard library only, resumable, every large file
checked against the sha256 the hub publishes, a `SHA256SUMS` beside each model and one
`manifest.json` for the set. ONNX, TensorFlow, Flax, Rust and Lightning files are always
skipped, and PyTorch `.bin` files when a model ships safetensors.

`config/model-sources.txt` ships filled in. Every line names the repository and the commit
that was current on 2026-09-16, so a fetch is reproducible; the `[quickstart]` section is
what the quickstart profile serves and `[prod]` adds the planner and the BF16 reference:

| Model | Repository | Download | Serves |
|---|---|---|---|
| `deepseek-v4-flash` | `deepseek-ai/DeepSeek-V4-Flash` (FP8 as published) | 149 GiB | triage; voter |
| `qwen3.8-27b-fp8` | `Qwen/Qwen3.8-27B-FP8` | 29 GiB | coder; planner in quickstart; voter |
| `bge-m3` | `BAAI/bge-m3` | 2.1 GiB | embed |
| `bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-m3` | 2.1 GiB | rerank |
| `deepseek-v4-pro` (prod) | `deepseek-ai/DeepSeek-V4-Pro` (FP8 as published) | 805 GiB | planner |
| `minimax-m2.7` (prod) | `MiniMaxAI/MiniMax-M2.7` (FP8 as published; licence "other", read it) | 214 GiB | voter, the third family |
| `qwen3.8-27b-bf16` (prod) | `Qwen/Qwen3.8-27B` | 52 GiB | eval reference only |

The coder is a vision-language checkpoint served text-only. Quickstart is about 182 GiB;
prod about 1.2 TiB. The matching registries are
`config/models.quickstart.yaml` and `config/models.prod.yaml`, rendered from
`slas_model_manager.registry`; the installer writes the right one as `models.yaml`.

1. On the connected host, see what will be fetched and whether the disk holds it, then
   fetch. A token goes in the environment only if a repository is gated (none of the seven
   is):

   ```bash
   export HF_TOKEN=…                      # only for gated repositories; never on argv
   export HF_ENDPOINT=https://hub.internal # only if a mirror inside the perimeter exists
   scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models
   scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dest ./models
   ```

   Run it again after any interruption; it resumes and keeps complete files. To pick
   another build, change the repository or the commit on that line and adjust `vram_gib`
   in the registry if the size moved.
2. Carry `./models/` to the box and put it next to `install.sh` as `models/`, or anywhere
   and pass `--models DIR`. Until the bundle exists, add `--models-only`: the weights are
   placed and the rest of the install is skipped. `./install.sh --profile prod --dry-run
   --models-only` lists what will be copied; the real run verifies every checksum first,
   places each model under `/AI/Agent/Models/` (hard links when both are on one volume, a
   copy verified again in place otherwise), refuses to mix two revisions of one model, and
   writes `/AI/Agent/Models/models.yaml` from `config/models.prod.yaml` when there is none.
   It never overwrites a `models.yaml` that exists (INV-9), and when it writes one it names
   any model the registry expects whose weights are not there yet.
3. If the platform host itself is connected while you prepare it, the same fetch command
   works there with `--dest /AI/Agent/Models`; the installer then finds the weights in place
   and copies nothing. The running platform never downloads (INV-1); whether the host may
   fetch during the preparation window is CLAUDE.md §15 open decision (13).
4. Edit roles later on the Models page or in `models.yaml`; the format is in
   `services/model-manager/models.example.yaml`, the layout for this box in §4.

## 3 · Install the platform stack

### 3a · From this checkout on a connected host (quickstart, ADR-0014)

On the box itself, as root, with Docker 29 and the Compose plugin installed and a route to
Docker Hub, ghcr.io, quay.io, nvcr.io, PyPI and the npm registry:

```bash
git clone <this repository> && cd sw-local-agent-service
./install.sh --build --fetch-models --dry-run   # preflight; the fetch plan; every pull and build described
./install.sh --build --fetch-models             # weights → build and pull images → .env → up → sign-in URL
```

What happens, in order: the preflight (it says which engine serves the runtime socket —
"Docker Engine 29.0.1 serves the container-runtime socket /var/run/docker.sock (no Podman
socket at /run/podman/podman.sock, so .env will name it) …" — whether gVisor is registered
with it and whether the NVIDIA runtime answers); the quickstart weights are fetched into
`./models` (skip `--fetch-models` if they are already under `/AI/Agent/Models`); the locked
Python environment is created with `uv` when `.venv` is missing; every third-party image is
pulled by its pinned tag — the vLLM image by the digest the lock records — and retagged
`local/<reference>`; every first-party image is built from `images/<name>/Dockerfile` with
the repository root as context (bases by digest, Python dependencies from `uv.lock`,
JavaScript from `pnpm-lock.yaml`); the filled lock is written to `/AI/Agent/images.lock.json`
and checked; the sandbox images the sandbox manager lists are built the same way, their IDs
recorded in `/AI/Agent/sandbox-images.lock.json` and the toolchain manifest written to
`/AI/Agent/Toolchains/manifest.json`; on this Docker-only box `.env` gets
`SLAS_RUNTIME_SOCKET=/var/run/docker.sock`; `.env`, the secret files and the data
directories the services bind-mount (`Coding`, `Toolchains`, `.git-broker`, `Tickets`,
`Skills/library`, `SOP`, `Validation`, `Factory/…`, `Models`, `Knowledge`,
`Backups/stations`, `qdrant`, `tls`) are written as root, the user the stack runs as; the
weights are placed; `docker compose up -d --pull never` starts the stack; the installer waits
up to five minutes for every service to be healthy and, if one is not, prints its last 20
log lines in three parts. At the end it asks the api whether the administrator's one-time
password is still pending and prints it only then. The model manager then starts the
`vllm-*` containers from `/AI/Agent/Models/models.yaml` on the `slas_slas-inference` network;
`docker ps --filter label=slas.kind=vllm` lists them and the Models page says the state of
each.

The first build downloads base images and packages and takes a while; a second run is
mostly cached. `SLAS_REGISTRY` names the local tag label (default `local`),
`SLAS_HEALTH_WAIT_S` the health budget. The browser will warn about the certificate:
export the edge's root from `/AI/Agent/tls/caddy/pki/authorities/local/root.crt` and trust
it, or set `SLAS_TLS_MODE=provided` in `.env` with `server.crt`/`server.key` under
`/AI/Agent/tls/` and run `docker compose up -d` again.

Honest scope: this is an INV-1 exception for the preparation window only (ADR-0014). Behind
the edge answer sign-in, Home, Admin → People, Admin → Settings, Models and, as each round-2
slice lands, the Coding, Validation and Factory wizards (`docs/api-contract-round-2.md`);
`screen-worker` and `local-search-api` answer `/health` and `/metrics` and nothing else until
their rounds. The build is not reproducible bit for bit (Debian and Alpine packages are not
yet pinned), which is why the filled locks stay on the host and are never committed.

### 3b · From the signed bundle (prod, the release path)

On the connected build host, once per release:

```bash
scripts/lock-images.sh --sign            # pin every third-party image by digest, sign
scripts/build-bundle.sh --profile prod   # build first-party images offline, save, sign manifest
```

Commit the filled `compose/images.lock.*`. Carry `dist/slas-bundle-<version>.tgz` and
`config/cosign.pub` to the box, then:

```bash
tar xzf slas-bundle-<version>.tgz && cd slas-bundle-<version>
./install.sh --profile prod --dry-run    # read-only steps for real, changes described
./install.sh --profile prod              # preflight → verify → .env → secrets → images → model weights → up
```

`docs/runbooks/prod-profile.md` covers Vault, Keycloak, backups and the restore drill. The
quickstart profile is the same command without `--profile prod` and without cosign.

## 4 · GPU and model layout for this box

Eight GPUs, about 288 GB each. The plan keeps one GPU free so every single-GPU role can be
swapped blue/green with no downtime.

| GPUs | Instance | Serves | Weights |
|---|---|---|---|
| 0–3 | DeepSeek-V4 Pro, tensor parallel 4 | planner | 805 GiB |
| 4 | DeepSeek-V4 Flash | triage; stand-in planner during a Pro swap | 149 GiB |
| 5 | Qwen3.8-27B FP8 ×2, BGE-M3, BGE reranker, Qwen3.8-27B BF16 | coder, voter, embed, rerank, eval reference | 114 GiB |
| 6 | DeepSeek-V4 Flash | voter (a second instance: the model manager starts one per voter) | 149 GiB |
| 7 | MiniMax-M2.7 | voter, the third family | 214 GiB |

Rules this layout follows: FP8 or FP4 on Blackwell (§7; FP4 needs an ADR to enter the
registry), one BF16 copy only for eval regression, voters from three families (§5.3:
DeepSeek, Qwen, MiniMax). The model manager starts one instance per role and one per voter,
so Flash runs twice; letting a voter share a role's instance is CLAUDE.md §15 open decision
(14) and would free GPU 6. No GPU is spare: a Pro swap cannot be blue/green on eight GPUs;
route planner to Flash, stop Pro, start the new Pro, smoke-test, route back.

Quickstart on the same box needs three GPUs of this class: Flash twice (triage and voter),
Qwen three times (coder, planner and voter) and the two BGE models, about 490 GiB in all;
that is what `config/models.quickstart.yaml` declares. Its two voters come from two
families, so every quickstart cross-check is reported as a weaker check (CLAUDE.md §15 open
decision 12); the sources file says how to add MiniMax-M2.7 as the third.

## 5 · Use it

- **WebUI** at `https://<host>/`: a left rail with Home · Coding · Validation · Factory · Runs ·
  Models · Skills · Settings · Admin, and one health sentence at the top. Home shows what needs
  you (three-part notices), what is running, and recent results, all drawn from the agents'
  own lists.
  Each agent page has one primary button, "New …", a three-step wizard that ends in a
  sentence saying what will happen and a verb button. Every finding is a sentence with a
  ticket link. Settings holds your Git remotes (paste-only credentials, fingerprint shown
  after save). Admin holds the Git host allowlist and the test stations.
- **CLI** mirrors the WebUI: `slas doctor`, `slas status`, `slas toolchain list|add`,
  `slas target list|add|arm|disarm`, `slas backup now|status|restore|drill`. `slas model`
  and `slas skill` arrive with Phases 3 and 4.
- **Stations**: Admin → Stations → add a station → "Issue code" → type the code on the
  station's runner (`deploy/station-runner/`). From then on the station is enrolled over
  mTLS and the Factory page can watch it live and take it over.
- **Models**: edit the `roles:` block of `Models/models.yaml`, or swap blue/green; the
  gateway route moves when the candidate passes its smoke test and can be rolled back for
  24 hours.

## 6 · Development use on the box, today

```bash
git clone <this repository> && cd sw-local-agent-service
uv sync --frozen && uv run pytest          # everything against fakes
cd apps/webui && pnpm install && pnpm dev  # WebUI on 127.0.0.1:5173 with the API fakes
uv run python -m slas_cli doctor           # the preflight against this host
```

The WebUI dev server binds loopback only. Reach it through an SSH tunnel, never by opening
the port.
