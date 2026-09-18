# SW Local Agent Service

Self-hosted, air-gap-capable AI workers for server hardware engineering: a **Coding Agent**,
a **Validation Agent** and a **Factory Agent** on one shared kernel. Every agent can operate
the OS of the machine it works on, has critical outputs cross-checked by several local
models, tracks its work as a ticket from ingestion to root-cause analysis, writes its report
and SOP in English and Chinese, and can be extended with importable skills.

No cloud. No external APIs. Nothing leaves the perimeter.

## Start here

| If you are… | Read |
|---|---|
| An AI coding session | [`CLAUDE.md`](CLAUDE.md) — the single source of truth. Read it first, every session. |
| Planning the build | [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — 13 phases, done criteria, milestone demos |
| Driving the build with prompts | [`docs/PROMPTS.md`](docs/PROMPTS.md) — copy-paste prompts per phase |
| Reviewing the intended UI | [`docs/ui-demo/slas-ui-demo.html`](docs/ui-demo/slas-ui-demo.html) — open in a browser, no server needed. The WebUI follows it: shell, tokens and Home in `apps/webui`, copy in `docs/ui/home.md` |

## Quick start

On a connected Ubuntu host with Docker and Compose (ADR-0014; quickstart profile):

```bash
git clone <this repository> && cd sw-local-agent-service
./install.sh --build --fetch-models   # preflight → weights → build and pull images → .env → up → sign-in URL
# Starts the Coding Agent (ADR-0017). Add --agents coding,validation,factory for the other two.
# Run it again after a failure: weights already here are recognised and never downloaded twice.
```

The images are built from this checkout with every base pinned by digest and every
dependency from the lock files; the filled image lock lands under `/AI/Agent`. Round 2
(ADR-0015, `docs/api-contract-round-2.md`) puts the agents on the wire: every service serves
HTTP on 8000 and finds the others through `SLAS_*_URL`; the model manager starts the vLLM
instances (`vllm-<role>`, `vllm-voter-<model id>`) from the pinned `vllm/vllm-openai` image
over the runtime socket — Podman's by default, Docker's when only that one exists, which
the installer writes into `.env` and says; the sandbox images are built from
`images/sandbox-*/Dockerfile` and their toolchain manifest written. `--dry-run` first prints
what would be built, pulled and written; the preflight says which engine serves the socket,
whether gVisor is registered and whether the NVIDIA runtime answers. The operator SOP's §7
walks through what is up afterwards and the first coding task.

The air-gapped path (target state, Phase 1), from a signed bundle built on a release host:

```bash
tar xzf slas-bundle-<version>.tar.gz
cd slas-bundle-<version>
./install.sh            # preflight → config → load images → models → up → login URL
```

One command, one `.env`, one page to manage models. Details in `CLAUDE.md` §3.

Model weights come from a connected host, never from the platform (INV-1). The sources file
ships filled in and pinned; fetch the set your profile needs, carry it over, and let the
installer verify, place it and write `models.yaml`:

```bash
./install.sh --fetch-models --models-only    # one command: download, verify, place, write models.yaml
```

Or in two steps, when the platform host has no route to the hub: run
`scripts/fetch_models.py fetch --sources config/model-sources.txt --profile quickstart --dest ./models`
on a connected host, carry `models/` next to `install.sh`, and run `./install.sh --models-only`.
The full `./install.sh` finds the weights in place once the bundle exists.

Deploying to a specific GPU host, and what runs today versus what still waits on a
dependency decision: [`docs/runbooks/deploy-hgx-b300.md`](docs/runbooks/deploy-hgx-b300.md).
The operator SOP, in English and Traditional Chinese:
[`docs/runbooks/setup-and-operations-sop.md`](docs/runbooks/setup-and-operations-sop.md).

## Status

**Phase 0 — Skeleton.** The repository holds the layout from `CLAUDE.md` §13, the Python
workspace (`uv`, `ruff`, `mypy --strict`, `pytest`), the WebUI shell (`pnpm`, Vite, React,
`vitest`, Playwright), CI with an egress-DROP job, `./install.sh` with the preflight step
(`slas doctor`), and ADR-0001/ADR-0002.

**Core through Phase 5, against fakes.** The Agent Kernel (`slas_kernel`: lifecycle,
write-ahead journal, crash recovery, NullAgent), the schemas, authz, the LLM gateway with
the Consensus Router, the model manager, the skills engine and screen driver, and now the
Phase 5 pieces: hybrid retrieval (`slas_rag`: dense + BM25 → RRF → rerank, cited answers),
the RCA pipeline (normalise → fingerprint → retrieve → draft → consensus → deterministic
owner routing from `config/owner-routing.yaml`), the dual-language SOP renderer
(`slas_sop`: `docs/glossary.yaml` pinned, identifiers protected by code), and the eval
checks (`slas_eval`: terminology consistency, back-translation spot check, local judges
only). Everything runs and is tested without a database, a model or a display; the
Qdrant/Postgres adapters, the gateway-backed embedder, reranker, drafter and translator,
and the Knowledge page wait on the dependency decisions listed in the phase reports.

**Phase 6, first session: sandboxes and the Coding Agent.** `services/sandbox-manager`
(hardened sandbox spec — no network, read-only rootfs, no capabilities, three mounts, no
credential; gVisor with a hardened runc fallback; TTL and quotas; the offline toolchain
resolver and `slas toolchain list|add`), one sandbox image per language, workspace-local
Git with `Slas-Agent`/`Slas-Ticket` trailers (`slas_git.workspace`), and the Coding Agent
on the kernel (`services/agent-core-orchestrator`): plan → breakdown → iterate with stall
detection → commit → ZIP → 3-voter cross-check → walkthrough SOP. The Coding page and the
three-step New coding task wizard run on an in-memory API fake until apps/api exists.

**Phase 6, second session: the Hybrid Git Control Engine.** `packages/slas-git` gains the
host allowlist (`config/git-hosts.yaml`), credentials sealed by reference (HKDF from
`SLAS_SECRET_KEY`; AES-GCM binds to `cryptography` once approved, a fake sealer for tests),
remotes, the validation gate (path scope, hooks, submodules, escaping symlinks, size, LFS,
secret scan, protected-branch policy, Consensus Router for agent diffs), merge-request
adapters for GitLab, Gitea and GitHub, bundles, audit rows and redaction.
`services/git-broker` runs every remote operation with the token on an inherited pipe fd
(`GIT_ASKPASS`) or an SSH key on tmpfs shredded after use, the hardening flags on every
`git`, and one audit row each. The Terminal session runs lines inside the sandbox with a
redacted transcript. Settings → Git remotes, Admin → Git hosts and the per-project Git
panel (Status · Commit · History · Push/Pull · Bundle · Terminal) run on API fakes. Tests
push through a fake Git host on loopback with a hostile pre-push hook and grep every sink
for the token afterwards. The Podman driver, the WebSocket/xterm.js terminal and the
service's HTTP surface wait on their dependency decisions.

**Phase 11: observability.** `packages/slas-observability` (standard library only): the §8.2
metrics in a closed catalogue with the Prometheus text format and a `/metrics` server for
every service; one W3C trace id from the WebUI (`apps/webui/src/trace.ts`) through the api,
the kernel's journal and executor context, the gateway and the vLLM HTTP client; JSON events
with the trace id on every line; a **local alert channel** (`Alerts/alerts.json`) written by
the gateway the moment a breaker trips or voters disagree and by Alertmanager through a
webhook; Prometheus scrape configuration, 19 alert rules in three parts, the Alertmanager
configuration and six Grafana dashboards (inference, GPU, agents, sandboxes and screens,
validation runs, factory) rendered to `observability/` and kept in step by tests that check
every PromQL name against the catalogue; and `slas status`. A test follows one trace id
from a WebUI header through the api edge, the kernel, the real gateway over HTTP to a fake
vLLM and the executor. Live dashboards from a real run wait on apps/api, the base compose
file and a GPU host; prometheus_client, structlog and OpenTelemetry remain unapproved and
are not needed for the wire formats used here.

**Phase 12: the prod profile.** `packages/slas-deploy` renders every deployment file from
code: the base `compose/docker-compose.yml` (a Phase 1 deliverable landing here, per
ADR-0003: no `env_file`, file secrets, hardened services, one member per lab, factory and
Git network), `prod.override.yml` (Vault at dispatch for the executors and git-broker,
Keycloak OIDC beside the built-in accounts, the Kata/Firecracker sandbox tier, pgBackRest
archiving to MinIO under object lock, Loki and Tempo), macvlan overlays for the lab VLAN and
the factory LAN, the image lock, the Keycloak realm, the Vault server file and per-service
policies, the pgBackRest and PostgreSQL settings, and the MinIO, backup and Vault bootstrap
scripts. `install.sh` now runs the whole pipeline — preflight, cosign verification of the
bundle manifest or of every image in Harbor, the lock and manifest checks, `.env`, secrets,
images, `compose up`, health — with read-only steps first and `--dry-run`; `slas backup
now|status|restore|drill` drives pgBackRest and the restore drill records every phase and
the RTO. The image lock ships **unpinned**, so the installer refuses to start until a
connected build host runs `scripts/lock-images.sh`; that is INV-8 held by refusal. Nothing
here has run against Docker, Vault, Keycloak, Harbor or Postgres: the installer is proven
against stub tools in dry-run, everything else against fakes, and the first real
`./install.sh --profile prod` and the first measured RTO are release-checklist items named
in `docs/runbooks/restore-drill.md`.

**Phase 7: the Validation Agent against fakes.** `plans/` (plan schema and primitives
rendered from `slas_hal.primitives`), the plan compiler (`suite.md`/`suite.xlsx` → `plan.yaml`
with the §10.2 reject rules), `slas_hal` (models, Redfish parsers that turn malformed and
truncated answers into three-part errors, and recorded fakes with planted failures),
`slas_diff` (device counts, PCIe width AND speed, firmware, AER/EDAC/MCE/Xid, new SEL),
`services/validation-executor` (ARM/QUIESCE/ACT/SETTLE/VERIFY/GATE with fence markers, the
guardrails from `config/guardrails.yaml`, exclusive leases and per-cycle crash recovery),
`slas_triage` (fingerprint, dedup, owner routing, the durable bug index) and bug-ticket
spawning in the kernel (`[Issue] … | [Owner] EE`, one child ticket per fingerprint across
runs, carrying the diagnosis votes). The Validation page (LED cycle map, findings, console)
and the three-step New validation run wizard run on an API fake. The done-when is a test: a
25-cycle DC run with a PCIe degradation planted at cycle 14 yields one deduplicated bug
ticket with 3 votes and an EN/中文 SOP; a kill mid-cycle resumes at that cycle without a
second power action; an AC plan is blocked until approved.

**Phase 8, first session: the real drivers, against fake hardware.** `slas_hal.drivers`
puts Redfish (DMTF URIs discovered from the service root, paged SEL, device links, the reset
action), `ipmitool` and `ssh` (argv only; password in `IPMI_PASSWORD`, key on tmpfs for one
command), SOL capture, a syslog receiver and the PDU protocol behind the same `Hal` interface
the fakes implement. Target records (`slas target add|list|arm|disarm`) carry credential
references only, and every target starts **disarmed**: no power action reaches hardware until
a person confirms it is free. A quirk-shim table (`config/bmc-quirks.yaml`) keyed by vendor
and firmware covers SEL paging, PCIe location, BDF field, reset types, IPMI-for-power and
unreliable link width (then `lspci` in-band). CI runs the 25-cycle run through the real
drivers against a fake Redfish service and greps every resulting file for the fake BMC
password, the fake key and every credential shape. Not done: the PDU model and the target
alias were not named, so no real hardware was touched and the model-specific PDU driver is
an explicit TODO.

**Phase 9: the Factory Agent against fakes.** `services/station-runner` (signed step
batches over mTLS with the standard library, GUI steps through the local screen driver with
screenshots back, allowlisted commands, state for the backup; a `FakeStation` with a scripted
Login → BurnIn screen and shell), `services/factory-executor` (the Factory verbs and
`plans/primitives/factory.yaml`, the `final-test-9-steps` template under `templates/factory/`,
the MES adapter with a file-drop implementation, station leases, station backups under
`Backups/stations/<station>/<ticket>/`, and the verdict: deterministic gate, then 3 of 3
voters for PASS; FAIL or a split vote holds the station and drafts a line-lead ticket), the
Factory Agent on the kernel (MES ticket, label scan or manual entry → template → plan →
production line SOP in EN/中文 → verdict back to the MES), and the Factory page with the
test-step map, the screenshot strip, the line lead's decision and the three-step New factory
job wizard. The done-when is a test: a fake MES ticket runs the 9-step loop on the fake
station and passes with 3 votes; a planted failure holds the station and drafts the ticket;
the SOP and the backup land on the ticket. A real station is P10.

**Phase 10: the factory station.** The station runner is packaged for a Linux or Windows
station (`deploy/station-runner/`: offline wheels, `install.sh` with a systemd `--user`
unit, `install.ps1` with a logon task, the platform CA in the bundle and nothing secret) and
enrols with a **one-time code** from Admin → Stations: the code is hashed, lives 15 minutes,
locks after five wrong tries, and is redeemed once over TLS; the factory executor mints the
station's certificate with the platform CA (`openssl` argv, created at first start) and a
per-station batch signing key kept by reference under `Factory/keys/`. Window matching
(contains · prefix · exact · regex), settle time and timeout scale are tuned per station
record, never in the skill; screenshot retention is a setting (`config/factory.yaml` and per
station) applied on the platform and on the runner. The operator watches the station's own
VNC server relayed over the runner's mTLS channel and takes over at any point: the runner
pauses at the next step boundary, resumes, or aborts with a sentence on the ticket. Tests
cover enrolment end to end with a real CA and mTLS on loopback, the relay, the pause and
the CLI. Tuning against a real station waits on its name; a Windows GUI backend
(PyAutoGUI), a single binary (PyInstaller) and Ed25519 signatures (`cryptography`) wait on
dependency decisions.

## Developing

```bash
uv sync                      # Python workspace, locked versions
uv run pytest                # unit tests with coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy
pnpm install --frozen-lockfile
pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright install chromium && pnpm e2e
./install.sh                 # preflight only in Phase 0; prints a plain-language report
uv run slas doctor --json    # the same report as data
```

Everything above also runs with the network disabled once the dependencies are installed;
CI proves it in the `egress-drop` job.

## Licence

To be decided (see `CLAUDE.md` §15, open decisions).
