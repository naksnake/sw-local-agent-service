# Deploying to an NVIDIA HGX B300 NVL8 host

This runbook says, in order, what you can run on the box today, what the full stack still
waits on, and the steps for both. It is written against the code on this branch; every
"works today" claim below is covered by a test or was run while writing it. Nothing here
relaxes an invariant: no download at runtime (INV-1), weights and images arrive in the
bundle.

## 0 · What runs today, what does not

| Piece | State | Where it stands |
|---|---|---|
| `slas doctor` preflight, `slas status`, `slas toolchain`, `slas target`, `slas backup` | runs | stdlib CLI, no virtualenv needed |
| WebUI in the demo's shell (`docs/ui-demo/slas-ui-demo.html`): rail, health line, Home dashboard; Coding · Validation · Factory with their three-step wizards; Settings (Git remotes); Admin (Git hosts · Stations); Runs · Models · Skills say when they arrive | runs on API fakes | `pnpm dev`; sign-in and the live API wait on `apps/api` (ADR-0005 dependency approval) |
| Station runner on a physical test station | runs | `deploy/station-runner/`, offline wheels, mTLS enrolment |
| Kernel (with the ADR-0013 skill gate), skills, HAL, executors, gateway, model manager, git broker, observability | run against fakes, 783 Python tests, 28 WebUI tests | Python packages in this repository |
| `docker compose up` of the platform stack | **blocked** | first-party images have no Dockerfiles yet except the sandboxes and the screen worker; `compose/images.lock.*` is unpinned, so the installer refuses (INV-8) |
| vLLM instances started by the model manager | **blocked** | the Podman driver behind `ContainerRuntime` waits on its dependency approval; the registry, fit and swap logic are done |
| Sign-in, users, Postgres-backed tickets | **blocked** | `apps/api` stack (ADR-0005) not approved |

So: today the box can be prepared, checked and used for development and for the station
runner. The one-command install becomes real once the three blocked rows land.

## The procedure, in order

Tick each line; every command below is explained in the section it points to.

| # | Where | Do | Done when | Section |
|---|---|---|---|---|
| 1 | B300 host | `nvidia-smi` shows 8 GPUs, NVLink up; install Docker, rootless Podman + `uidmap`, gVisor, the NVIDIA container toolkit; boot with cgroups v2 | `./install.sh --preflight-only` shows no ✗ | §1 |
| 2 | B300 host | Mount ≥ 200 GiB at `/AI/Agent`; a separate volume for `Models/` (≥ 1.5 TB for the plan) | `slas doctor` Disk space is ✓ | §1 |
| 3 | Connected host | Fetch the weights into directories named after their registry `path`; checksum | checksums recorded | §2 |
| 4 | Sneakernet → B300 | Copy the weights under `/AI/Agent/Models/<path>/`; verify checksums; write `models.yaml` from §4 | `models.yaml` validates (the registry says so in one sentence) | §2, §4 |
| 5 | Repository | Decide the three dependency items that block the container stack: `apps/api` stack (ADR-0005), the Podman driver for the model manager, first-party Dockerfiles | ADRs accepted | §0 |
| 6 | Connected build host | `scripts/lock-images.sh --sign`, `scripts/build-bundle.sh --profile prod`; commit the filled lock | `compose/images.lock.*` has no null digest | §3 |
| 7 | Sneakernet → B300 | Carry `slas-bundle-<version>.tgz` and `config/cosign.pub` | both files on the host | §3 |
| 8 | B300 host | `./install.sh --profile prod --dry-run`, then `./install.sh --profile prod` | the sign-in URL and one-time admin password are printed | §3 |
| 9 | Browser | Sign in, change the password, Admin → People, Admin → Stations → Issue code | first station enrolled | §5 |
| 10 | B300 host | `slas backup drill` once; record the RTO | a row in `docs/runbooks/restore-drill.md` | prod runbook |

Steps 1 to 4 can be done today. Steps 6 to 10 wait on step 5.

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
   are about 1.2 TB on top of that; put `Models/` on its own volume.
4. Open port 443 on the host; nothing else is published.
5. Run the preflight from a source checkout or the bundle:

   ```bash
   ./install.sh --preflight-only          # or: python3 -m slas_cli doctor
   ```

   Every line is ✓, ! or ✗ with what happened, the likely cause and what to do. ✗ stops the
   install; ! does not.

## 2 · Bring the weights

Weights never download on the box. On a connected build host:

1. Fetch each model into a directory named after its registry `path`, for example
   `deepseek-v4-pro-fp4/`, `deepseek-v4-flash-fp4/`, `qwen3.8-27b-fp8/`, `qwen3.8-27b-bf16/`,
   `bge-m3/`, `bge-reranker-v2-m3/`.
2. Checksum, copy to the box, and place under `/AI/Agent/Models/<path>/`. Verify the
   checksums on the box.
3. Write `/AI/Agent/Models/models.yaml`. The registry format is in
   `services/model-manager/models.example.yaml`; the plan for this box is in §4.

## 3 · Install the platform stack (when the blocked rows land)

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
./install.sh --profile prod              # preflight → verify → .env → secrets → images → up
```

`docs/runbooks/prod-profile.md` covers Vault, Keycloak, backups and the restore drill. The
quickstart profile is the same command without `--profile prod` and without cosign.

## 4 · GPU and model layout for this box

Eight GPUs, about 288 GB each. The plan keeps one GPU free so every single-GPU role can be
swapped blue/green with no downtime.

| GPUs | Instance | Serves | Weights |
|---|---|---|---|
| 0–3 | DeepSeek-V4 Pro, tensor parallel 4 | planner, voter | ~960 GB |
| 4 | DeepSeek-V4 Flash | triage, voter, stand-in planner during a Pro swap | ~160 GB |
| 5 | Qwen3.8-27B FP8 | coder, voter | ~28 GB |
| 6 | BGE-M3, BGE reranker, Qwen3.8-27B BF16 | embed, rerank, eval reference | ~61 GB |
| 7 | free | blue/green candidates, or a third-family voter | — |

Rules this layout follows: FP8 or FP4 on Blackwell (§7; FP4 needs ADR-0014 to enter the
registry), one BF16 copy only for eval regression, voters from different families where
possible (§5.3; with two DeepSeek voters the registry reports "2 model families" every time
it loads). A Pro swap cannot be blue/green on eight GPUs: route planner to Flash, stop Pro,
start the new Pro, smoke-test, route back.

Quickstart on the same box uses GPUs 4, 5 and 6 only: triage, coder, embed and rerank.

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
