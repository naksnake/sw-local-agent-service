# CLAUDE.md — SW Local Agent Service

> **Single Source of Truth (SSOT).** Any Claude session working on this repository reads
> this file first and treats it as authoritative. Where this file conflicts with a
> request, surface the conflict; never resolve it silently.
>
> **Version 3.1 · 2026-09-10** · Companion documents: `docs/DEVELOPMENT_PLAN.md`
> (phases, done criteria) and `docs/PROMPTS.md` (copy-paste session prompts).
> 3.1 adds the Hybrid Git Control Engine (§5.7, INV-14).

---

## §0 HOW TO USE THIS FILE

### 0.1 Naming — use exactly

| Context | Name |
|---|---|
| Product / display name | **SW Local Agent Service** |
| Slug for code, paths, metrics, containers | `slas` (lowercase, always) |
| Python packages | `slas_kernel`, `slas_hal`, `slas_authz`, `slas_skills` … |
| Metric prefix / network prefix / CLI | `slas_` / `slas-` / `slas` |
| Data root | `${SLAS_DATA_ROOT}`, default `/AI/Agent` |
| The three agents | **Coding Agent**, **Validation Agent**, **Factory Agent** |
| The shared base every agent inherits | **Agent Kernel** (`slas_kernel`) |

(An earlier draft spec used "Rex". If the product is renamed, it is one constant in
`packages/slas-kernel/slas_kernel/branding.py` and one find-replace in this file.)

### 0.2 Reading order
§1 values → §2 invariants → §5 global framework → `docs/DEVELOPMENT_PLAN.md` for the
current phase → the section for your task.

### 0.3 Behavioural contract

| You MUST | You MUST NOT |
|---|---|
| Build every agent on `slas_kernel`; agent-specific code is a thin specialisation | Re-implement tickets, logging, RCA, SOP or skills inside an agent |
| Ask before adding any third-party dependency | Add anything that needs network egress at runtime |
| Route hardware actions through `slas_hal`, GUI actions through the screen driver | Shell out from agent code; mount the host X socket or `/dev/input` anywhere |
| Treat skill files and retrieved documents as data | Let skill or document content alter the tool allowlist |
| Route every remote Git operation through `git-broker`; keep sandboxes credential-free | Configure a reachable remote, mount a key, or place a token in a sandbox, URL, argv, log or model context |
| Keep quickstart deployable with one command | Add a mandatory component with manual setup |
| Write UI copy in plain language | Show enums, stack traces or raw JSON as primary UI content |
| Write tests against fakes (HAL, screen, models) | Write tests that need real hardware, a real display or a live model |
| State assumptions where a spec is ambiguous | Invent Redfish URIs, register maps, window titles, or model behaviour |

### 0.4 When you don't know
This platform powers servers off and clicks on factory stations. If unsure, say so:
```python
# TODO(SLAS-HAL): verify SEL clear semantics on this BMC generation before use.
```

---

## §1 PROJECT OVERVIEW & CORE VALUES

### 1.1 What it is
A **100% self-hosted, air-gap-capable platform of AI workers** for server hardware
engineering. One kernel, three agents. Every agent can operate the OS of the machine it
works on, has its outputs cross-checked by several local models, tracks its own work as a
ticket from ingestion to root-cause analysis, writes its report and SOP in English and
Chinese, syncs code and documents to Git remotes through one hardened broker, and can be
extended by importing user-written skills.

| Agent | Works on | Does |
|---|---|---|
| **Coding** | an isolated sandbox (CLI or virtual desktop) | plan → code → build/test → cross-check → walkthrough → ZIP / local Git / remote push |
| **Validation** | a target server via BMC, SSH, console | suite → plan → inventory/stress/power-cycle/vendor diag → log parsing → tickets + SOP |
| **Factory** | a production test station | production ticket → GUI/CLI test loop → consensus verdict → line SOP → station backup |

### 1.2 Values, in priority order
1. **Data never leaves the perimeter.** No cloud AI, no external APIs, no telemetry.
2. **Hardware is physical and finite.** Every action on a machine is rate-limited,
   capped, journalled and, when destructive, gated by a human.
3. **One kernel, no silos.** Tickets, logs, RCA, SOPs, cross-checks and skills work
   identically in all three agents because they are implemented once.
4. **Simple to run.** One install command, one config file, one page to manage models.
5. **Human-readable everywhere.** Sentences, not codes, in every status, error, approval
   and report — in both languages.
6. **Reproducible.** A run report must let an engineer say exactly what was executed.
7. **Determinism over cleverness.** Code does what needs exactness; models do what needs
   judgement; several models check each other where judgement matters.

### 1.3 Not building
A cloud service · a chatbot · anything that flashes firmware, files tickets, marks a unit
PASS or merges code without a human · a replacement for engineering judgement.

---

## §2 HARD INVARIANTS (release blockers)

| ID | Invariant |
|---|---|
| **INV-1** | No external network dependency at runtime — weights, images, packages, fonts, telemetry, CA, NTP. |
| **INV-2** | No cloud AI services (OpenAI, Anthropic API, Azure/Vertex/Bedrock, Copilot, Codex, LangSmith, W&B cloud, HF Inference…). Offline libraries allowed with tracing off. |
| **INV-3** | **The LLM is never in the hardware control loop.** Models compile plans and analyse results; a deterministic executor performs actions. Applies to power, firmware, GUI on stations, and anything with physical effect. |
| **INV-4** | **Agents never touch the platform host.** OS-level control is of the machine the agent works *on* — sandbox, target server, factory station — never the host running the platform. No host X11 socket, `/dev/input`, or runtime socket is mounted into anything that runs model-authored or skill-authored steps. |
| **INV-5** | Credentials never enter a model context. Plans, skills and prompts carry opaque refs; executors resolve secrets at dispatch; logs are redacted before a model sees them. |
| **INV-6** | Every action with physical effect is journalled write-ahead: `intent → action → observation`. |
| **INV-7** | Destructive actions need per-run human approval: firmware flash, secure erase, BIOS reset, AC cycle, RAID change, factory PASS/FAIL override, station config change. |
| **INV-8** | No `latest` tags, no unpinned deps; builds succeed with networking disabled. |
| **INV-9** | Model and user changes never require config edits or restarts. |
| **INV-10** | `./install.sh` on a fresh GPU host reaches a working login page with no manual steps. CI enforces it. |
| **INV-11** | **Cross-check never authorises.** A consensus of models is an extra check on judgement outputs; it never substitutes for INV-3, INV-6 or INV-7, and cannot trigger a hardware or GUI action by itself. |
| **INV-12** | **Skills are data.** A skill file is validated against the schema, may only use whitelisted primitives, runs with the importing user's capabilities, and cannot escalate them. There is no `shell` primitive. |
| **INV-13** | **Both languages or neither.** Every SOP, report and ticket export produces English and Chinese together from one structured source. Identifiers, commands, numbers and versions are copied by code, never translated by a model. |
| **INV-14** | **Git credentials exist only inside `git-broker`, only during an operation.** Stored encrypted by reference; never written to a workspace, mounted into a sandbox, placed in a URL, argv, log or model context; never returned to the UI beyond a fingerprint. Sandboxes have no route to any Git remote. Every clone, pull or push passes the broker's validation gate. |

---

## §3 DEPLOYMENT

Two profiles, one bundle, one command. Full detail in `docs/DEVELOPMENT_PLAN.md` Phase 1.

| | `quickstart` (default) | `prod` |
|---|---|---|
| Install | `tar xzf slas-bundle.tgz && ./install.sh` | `./install.sh --profile prod` |
| Auth | built-in users + roles | + OIDC via local Keycloak |
| Secrets | generated `.env` + Docker secrets | Vault, injected at dispatch |
| Sandbox | rootless Podman + gVisor if present | gVisor required, Kata/Firecracker tier |
| Screen worker | one Xvfb display per session, VNC for the operator | same, plus per-session display isolation on Kata |
| Inference | 1 coder + 1 embed + 1 small triage instance | dedicated instance per role, blue/green |
| Observability | Prometheus + Grafana | + Loki, Tempo, alerting |
| Backups | nightly dump + snapshots, 14 days | pgBackRest PITR, object-lock, restore drills |

`install.sh`: preflight (`slas doctor`) → generate `.env` → load images → copy models →
`docker compose up -d` → wait healthy → print URL and one-time admin password. Idempotent.
Anything mandatory in quickstart has zero manual setup.

The `slas` CLI mirrors the WebUI: `doctor`, `status`, `logs`, `user add`, `model
scan|fit|swap|test`, `toolchain list|add`, `skill import|export|validate`, `backup now|restore`,
`upgrade`. The bundle ships the offline **toolchains** (Python 3.11/3.12, gcc/clang, Rust, Go,
Node, shell tooling, yamllint/jsonschema); `slas toolchain list` shows what is available.

---

## §4 SYSTEM ARCHITECTURE

### 4.1 Zone model

| Zone | What runs | Network | Runtime |
|---|---|---|---|
| **0 Control** | api, orchestrator (kernel), gateway, model-manager | backend | plain containers |
| **A Code sandbox** | model-authored code; optional virtual desktop | none / allowlist proxy | gVisor, Kata tier |
| **B Validation executor** | deterministic executor + HAL | lab VLAN only | plain container, sole lab member |
| **B′ Factory executor** | deterministic executor + station runner client | factory LAN only | plain container, sole factory member |
| **G Git broker** | clone/pull/push with injected credentials; bundle export/import | backend + `slas-git` (allowlisted Git hosts only) | plain container, no sockets, sole `slas-git` member |
| **C Inference** | vLLM instances | inference net, no egress | GPU containers, managed |
| **S Screen worker** | Xvfb + VNC + screen driver, per session | none (talks to orchestrator only) | gVisor |
| **D Targets** | servers, stations | lab / factory | not ours |

Zone A and Zone S never talk to vLLM. The kernel calls the gateway, then sends concrete
steps into the sandbox or screen worker and reads results back.

### 4.2 Topology

```
                 ┌──────────────────────────────────────────────────────┐
                 │  WebUI — Home · Coding (Git panel · terminal) ·      │
                 │  Validation · Factory · Runs · Tickets · Models ·    │
                 │  Skills · Knowledge · Admin (Git hosts)              │
                 └──────────────────────────┬───────────────────────────┘
                                            │ session / OIDC → capability token
                 ┌──────────────────────────▼───────────────────────────┐
                 │  API — authz · quota · leases · approvals · settings │
                 │        TICKET SERVICE (create, state, RCA, export)   │
                 └───┬───────────────────────────────────────────────┬──┘
                     │                                               │
   ┌─────────────────▼──────────────────────┐        ┌───────────────▼─────────────────┐
   │  ORCHESTRATOR — runs the AGENT KERNEL  │        │  LLM GATEWAY                    │
   │  ingest→ticket→plan→act→logs→RCA→SOP   │◄──────►│  role routing · redaction       │
   │  ├─ Skill Compiler (YAML → step plan)  │        │  schema enforce · breaker       │
   │  ├─ SOP Generator (EN + 中文)           │        │  CONSENSUS ROUTER (N voters)    │
   │  └─ RCA (RAG over logs + knowledge)    │        └───────────────┬─────────────────┘
   └──┬──────────────┬──────────────┬───────┘        ┌───────────────▼─────────────────┐
      │              │              │                │  MODEL MANAGER → vllm-*         │
      │              │              │                │  coder · planner · triage       │
      │              │              │                │  embed · rerank   (no egress)   │
      │              │              │                └─────────────────────────────────┘
      │              │              │                ┌─────────────────────────────────┐
      │              │              └───────────────►│  KNOWLEDGE — Qdrant · FTS       │
      │              │                               │  MinIO (docs, run artifacts)    │
      │              │                               └─────────────────────────────────┘
 ┌────▼─────────┐ ┌──▼──────────────────────────────────────────────────────────────────┐
 │ ZONE A       │ │ ZONE S  SCREEN WORKER — one Xvfb display per session, VNC to operator│
 │ code sandbox │ │         screen driver executes GUI steps; screenshot before/after    │
 │ gVisor       │ │         Used by: Coding (IDE/desktop), Validation (BIOS/vendor GUI), │
 │ git: local   │ │         Factory (station GUI via station runner)                     │
 │ commits only,│ └──────────────────────────────────────────────────────────────────────┘
 │ no creds, no │ ┌──────────────────────────────────────────────────────────────────────┐
 │ route out    │ │ ZONE G  GIT BROKER — the only holder of Git credentials and the only │
 └──────────────┘ │         route to remotes. Creds encrypted by reference, decrypted in  │
                  │         memory per operation. Hooks neutralised · validation gate ·   │
                  │         branch push · PR/MR · bundle export/import. Works on the SAME │
                  │         Projects/<slug>/.git that Zone A commits to locally.          │
                  │                                     slas-git ──► allowlisted Git hosts│
                  └──────────────────────────────────────────────────────────────────────┘
 ┌──────────────────────────────┐   ┌──────────────────────────────┐
 │ ZONE B  VALIDATION EXECUTOR  │   │ ZONE B′ FACTORY EXECUTOR      │
 │ state machine · slas_hal     │   │ state machine · station runner│
 │ SOL + syslog streaming       │   │ screenshots + MES adapter     │
 └──────────────┬───────────────┘   └───────────────┬──────────────┘
          lab VLAN │ SSH · Redfish · PDU       factory LAN │ runner RPC · SSH
 ┌────────────────▼────────────┐   ┌─────────────────────▼─────────┐
 │ target servers               │   │ test stations (with runner)   │
 └─────────────────────────────┘   └───────────────────────────────┘
 ┌──────────────────────────────────────────────────────────────────┐
 │ OBSERVABILITY — Prometheus · Grafana · DCGM · (Loki/Tempo prod)  │
 └──────────────────────────────────────────────────────────────────┘
```

### 4.3 Tech stack
Frontend TypeScript 5 strict, React 18, Vite, Tailwind, shadcn/ui, TanStack Query/Table,
react-hook-form + zod. Backend Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic,
Postgres 16, Redis 7, MinIO, `uv`. AI: vLLM (offline), Qdrant, Postgres FTS. Sandbox:
rootless Podman, gVisor, Kata. Screen: Xvfb, x11vnc/noVNC, PyAutoGUI + xdotool behind our
driver. Git: system `git` in the broker and in sandbox images; xterm.js terminal into the
sandbox; `git bundle` for offline transfer. Observability: Prometheus, Grafana, DCGM,
node/postgres exporters; prod Loki/Tempo.

**Rejected:** Docker-in-Docker (needs privileged or a socket) · host X11 socket or
`/dev/input` sharing into any agent container (INV-4) · SearXNG as an air-gapped search
(it is a proxy to Google/Bing and returns nothing offline — see §8.3) · tokens embedded in
remote URLs · SSH keys or credential helpers inside a workspace · running a workspace's
git hooks inside the broker (INV-14).

### 4.4 Filesystem layout

```
${SLAS_DATA_ROOT}/                       # default /AI/Agent
├── Coding/<user>/
│   ├── Container/<session>/                 # ephemeral sandbox overlay, reaped on TTL
│   ├── Projects/<slug>/.git                 # THE local repo — durable, bind-mounted rw into the sandbox
│   ├── Artifacts/<run>/                     # zips, walkthroughs
│   └── Bundles/                             # git bundle exports/imports for offline transfer
│   # Remotes and credentials are NOT on disk here — they live in the broker's encrypted store (§5.7)
├── Validation/{Suites/,Plans/,Runs/<run>/{journal.jsonl,baseline/,cycles/<n>/,findings/,report/}}
├── Factory/{Templates/,Jobs/<ticket>/{journal.jsonl,screens/,logs/,report/},Stations/<n>/backup/}
├── Tickets/<ticket-id>/                 # every agent; see §5.4
├── Skills/{library/<id>.skill.yaml, imported/, compiled/}
├── SOP/<ticket-id>/{sop.en.md, sop.zh-Hant.md, sop.json}
├── Models/{models.yaml, <model-id>/}
├── Knowledge/{datasheets/,test-specs/,runbooks/,past-reports/,ingest-manifest.yaml}
├── Backups/{postgres,qdrant,minio,stations}/
└── .env
```

---

## §5 GLOBAL AGENT FRAMEWORK (every agent inherits all of this)

### 5.1 Agent Kernel — the lifecycle every agent runs

```
INGEST      input arrives (plan.md · suite.xlsx · production ticket)
   │        kernel.ingest() → normalized Job
TICKET      kernel.ticket.create(job)  → T-xxxx, state=Open        ← automatic, always
   │        links: user, agent, inputs, target/sandbox, approvals needed
PLAN        agent.plan(job) → structured Plan (JSON-Schema)
   │        if plan has destructive steps → approval request (INV-7)
   │        if plan is critical → Consensus Router (§5.3) → votes attached to ticket
ACT         deterministic executor runs the Plan step by step        (INV-3)
   │        every step: journal write-ahead · stdout/stderr captured · screenshots for GUI
   │        skills (§5.6) expand into steps here
COLLECT     kernel.logs.collect() → normalized log bundle on the ticket
RCA         kernel.rca(bundle) → root-cause analysis via RAG over logs + knowledge
   │        + Consensus Router on the conclusion; deterministic fingerprint + owner routing
SOP         kernel.sop.generate() → structured SOP → EN + 中文 (§5.5)      (INV-13)
CLOSE       ticket state → Done / Needs review / Failed; exports attached
```

`slas_kernel` exposes exactly one abstract class:

```python
class Agent(Protocol):
    name: Literal["coding", "validation", "factory"]
    def ingest(self, raw: Upload | MesTicket) -> Job: ...
    def plan(self, job: Job) -> Plan: ...          # returns steps from the primitive whitelist
    def verify(self, step: Step, obs: Observation) -> Verdict: ...
    def sop_template(self) -> SopTemplate: ...     # agent-specific sections, kernel renders
```

Everything else — tickets, journal, log collection, RCA, SOP rendering, cross-check,
skill expansion, approvals, exports — is kernel code and is **forbidden in agent modules**.

### 5.2 OS Automation Interface

Agents operate the OS of the machine they work *on* (INV-4). Four drivers, one interface:

| Driver | Reaches | Used by | Runs where |
|---|---|---|---|
| `screen` | keyboard, mouse, windows, screenshots, clipboard | all three | Zone S (virtual display) or the **station runner** on a physical station |
| `inband` | SSH: commands, files, system config | Validation, Factory, Coding (inside sandbox) | executor / sandbox-manager |
| `oob` | BMC: Redfish/IPMI power, SEL, SOL, inventory, PDU | Validation | Zone B |
| `files` | read/write within the job's workspace | all three | sandbox / executor |

**Screen driver rules**
- Backed by PyAutoGUI + xdotool on an **Xvfb display the platform owns**, one per session,
  exposed to the operator through noVNC so they can watch and take over.
- On physical stations, a small **station runner** daemon (Python, signed step batches over
  mTLS) performs the same primitives locally. The platform never receives the station's
  raw display device; it receives screenshots and results.
- Every GUI step: screenshot before, screenshot after, both attached to the ticket.
  Targets are found by **window title / accessible name / template image**, never by
  bare coordinates unless the recipe author supplies them explicitly.
- Rate limit ≤ 10 actions/s, deny-list of windows (terminal emulators on the platform
  host, password managers), and a hard stop if the focused window changes unexpectedly.
- "Executing raw files dynamically" means: a file produced by the job may be run
  **inside the sandbox or on the target** via the `run` primitive, with argv, timeout,
  and the user's capabilities — never on the platform host.

### 5.3 Multi-Model Cross-Check — the Consensus Router

Lives in the LLM Gateway. Fans one request out to N voters, collects structured votes,
returns a verdict object. Voters are **different model families** where possible (Qwen,
DeepSeek, Kimi) to decorrelate errors; each voter sees the same evidence and **none of the
other votes**.

| Decision | Voters | Rule | If not met |
|---|---|---|---|
| Plan approval (Validation, Factory) | 3 | unanimous | human decides; concerns shown |
| Final code change (Coding) | 3 | majority; every concern surfaced | user addresses or dismisses with a note |
| Ticket diagnosis: owner / severity / root cause | 3 | majority **per field** | field marked "your call" |
| Factory PASS verdict | 3 | unanimous for PASS | line lead decides |
| RCA conclusion | 3 | majority | flagged "uncertain" in the report |

Not cross-checked: routine tool calls, per-line log triage, chat, anything inside a
deterministic loop. Budget: cross-checks ≤ 5% of daily tokens; the router degrades to
"single model + flagged" and alerts, never blocks work silently.

Vote schema is fixed (`slas_schemas/consensus.py`): `{voter, verdict: approve|concern|reject,
fields: {...}, reason, confidence}`. Votes are stored on the ticket and shown in the UI as
sentences: *"3 of 3 agree the owner is EE. Severity: 2 say S2, 1 says S1. Your call."*

**INV-11 restated:** a unanimous vote is *input to* a human approval or to the
deterministic gate — it never *is* the approval.

### 5.4 Automated ticketing, execution and log analysis

Every job **is** a ticket. Schema (`slas_schemas/ticket.py`):

```
T-<agent>-<seq>   state: Open → Planned → Approved → Running → Analysing → Needs review → Done | Failed
fields: agent, user, inputs[], target|sandbox, plan_id, approvals[], votes[], steps[],
        logs{stdout,stderr,console,screens}, findings[], rca{cause, evidence[], confidence},
        sop{en, zh}, exports[], parent (for bug tickets spawned from findings)
```

- Created at ingest, never by hand. Child **bug tickets** are spawned from findings
  (`[Issue] … | [Owner] EE` format, §10.2) after deduplication by fingerprint.
- Log collection is continuous, not at the end: stdout/stderr streamed; console (SOL)
  streamed; screenshots per GUI step; fence markers before every power action.
- **RCA pipeline (kernel):** normalise → fingerprint → retrieve similar past tickets and
  knowledge (RAG) → model drafts cause + evidence → Consensus Router → deterministic owner
  routing → human reviews. RCA never blocks the run; it runs after COLLECT.

### 5.5 Dual-language SOP / report generation

One structured source, two renderings, always exported together (INV-13).

```
SopModel (JSON): title, purpose, prerequisites[], steps[{n, action, expected, evidence}],
                 checks[], results{}, findings[], next_actions[], glossary_refs[]
        │
        ├─ prose fields authored by the model in English (schema-constrained)
        ├─ prose translated to zh-Hant by the `planner` role with the project glossary
        │   pinned (docs/glossary.yaml: e.g. "power cycle" → 電源循環, "baseline" → 基準)
        ├─ identifiers, commands, versions, numbers, paths, BDFs: copied by code
        └─ renderers: sop.en.md, sop.zh-Hant.md (+ .pdf via local WeasyPrint), sop.json
```

Default Chinese is **Traditional (zh-Hant)**; Simplified is a Settings toggle. Eval suite
includes a back-translation spot check and a terminology-consistency check against the
glossary. Agent-specific SOP shape: Coding → code walkthrough; Validation → verification
SOP; Factory → production line SOP.

### 5.6 User skills — import, compile, run, export

A **skill** is a YAML recipe of whitelisted steps (schema in §6). Users write them in the
WebUI (or by hand), export as `<id>.skill.yaml`, and import into any installation. Skills
declare which agents may use them; enabling is one toggle per agent.

```
IMPORT     schema validation (jsonschema) → primitive whitelist check →
           capability check (skill requires [screen, ssh] → user must hold them) →
           risk classification (safe / caution / destructive) → stored under Skills/library
COMPILE    `{{ inputs }}` bound · secrets resolved to handles · loops unrolled (bounded)
           → flat StepPlan the executor understands; identical for all agents
RUN        each step journalled; screen steps screenshot before/after; `run` steps capture
           stdout/stderr; failures follow on_failure
EXPORT     the stored YAML, with secrets stripped and a content hash
```

Skills never grant capabilities — they consume the importing user's. A skill containing a
destructive primitive shows the approval requirement at import time and at every run.

### 5.7 Hybrid Git Control Engine

Two ways to work with Git, one repository, one rule: **the sandbox never holds a credential
and never has a route to a remote** (INV-14). Both methods operate on the same
`Projects/<slug>/.git`.

```
      WebUI Git panel · `slas git …` · agent kernel "publish" step
                                │
     ┌──────────────────────────┴───────────────────────────┐
     │ METHOD 1 — Remote sync through the broker            │  METHOD 2 — Local Git in the workspace
     │ user adds a Remote in Settings → Git remotes:        │  Projects/<slug>/.git initialised at
     │   {name, uri, PAT | SSH private key}                 │  project creation with the user's identity
     │ credential → encrypted, stored BY REFERENCE          │  agent and user commit locally: terminal,
     │ git-broker decrypts in memory, runs clone/pull/push  │  file explorer, agent steps
     │ through the validation gate, wipes                   │  no remote is reachable; `git push` fails by
     │                                                      │  construction and the UI says where to push
     └──────────────────────────┬───────────────────────────┘
                                ▼
                 Projects/<slug>/.git  (durable, one truth)  ◄── git bundle in/out for sneakernet
```

**Method 1 — WebUI credential injection (`git-broker`)**

| Rule | Detail |
|---|---|
| Storage | `Remote {id, owner, name, uri (no secret), host, auth_type: pat\|ssh_key, credential_ref, fingerprint, allowed_ops, default_branch, last_used}`. The secret is AES-GCM encrypted with a key derived from `SLAS_SECRET_KEY` (quickstart) or held in Vault KV (prod), referenced by `credential_ref`. |
| Use | Decrypted inside `git-broker` for one operation. PAT via `GIT_ASKPASS` helper reading an inherited fd (never argv, never in the URL). SSH key written to a per-op tmpfs file, mode 0600, `GIT_SSH_COMMAND="ssh -i <f> -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<pinned>"`, shredded after. |
| Hostile-repo hardening | Every invocation: `GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0` and `-c core.hooksPath=/var/empty -c core.fsmonitor=false -c credential.helper= -c core.sshCommand= -c include.path=`. Repo config written by a model or a user never executes code inside the broker. |
| Egress | `git-broker` is the **only** member of the `slas-git` network, which egresses only to hosts in `config/git-hosts.yaml`. Quickstart default: internal GitLab/Gitea. `github.com`/`gitlab.com` require an ADR (INV-1 exception) and a per-host allow entry. A disallowed host fails with: *"github.com is not an allowed Git host. Allowed: gitlab.internal, gitea.internal."* |
| Push policy | Default: push to a branch, open a PR/MR when the host API is known (GitLab, Gitea, GitHub). Direct push to a protected branch needs `git:push_protected` (off by default). |
| Validation gate (every push) | changes inside the project path · secret scan (gitleaks) · no new hooks, submodules, or symlinks escaping the tree · size and file-count sane · LFS off unless enabled · **agent-authored diffs also pass the Consensus Router** (§5.3) |
| UI | Paste-only fields, never echoed back; after save the UI shows only the SSH fingerprint or PAT last-4. "Test connection" runs `ls-remote`. Rotate / delete / expiry. Recommend deploy keys or project tokens over personal credentials. |
| Audit | One row per operation: user, remote, op, branch, commit SHA, result, duration. Credentials are redacted at the logging boundary; a CI check greps every log sink for token/key patterns. |

**Method 2 — Workspace-local Git (inside the sandbox)**

| Rule | Detail |
|---|---|
| Identity | `git init` at project creation; `user.name = <display name>`, `user.email = <user>@slas.local`, per user, set in the sandbox's `GIT_CONFIG_GLOBAL`. |
| Agent commits | Carry trailers `Slas-Agent: coding` and `Slas-Ticket: T-…` so history shows what the agent did versus the user. |
| Terminal | xterm.js over WebSocket, executing **inside the sandbox** as the workspace user — same isolation as the agent, TTL, session recorded to the ticket with secrets redacted. Never a shell on the platform host. |
| File explorer | Shows git status decorations; Commit and History panels call `git` in the sandbox through the sandbox-manager exec API (argv only). |
| Remotes | None reachable. `git remote -v` may show a URL (not a secret) after a broker clone; `git push` fails with no route and the UI explains: *"Push happens from the Git panel, which uses your saved remote."* |
| Offline sync | `git bundle create` / `git bundle verify` + fetch through the Git panel → `Bundles/`. This is the air-gap-native way to move history between sites. |

**Shared rules**

- Validation and Factory use the same engine: any ticket export (SOP, plan, report) has
  *Publish to repo*, which commits to a configured docs remote through the broker.
- Capabilities (`slas-authz`): `git:remote_manage` (own remotes) · `git:clone` · `git:pull`
  · `git:push_branch` · `git:push_protected` (default off) · `git:bundle` · `git:terminal`
  · admin `git:hosts_manage`.
- A clone lands in `Projects/<slug>/` and is immediately visible to the sandbox; a push
  never moves files — it reads the repo the sandbox already committed to.
- Models see `remote_ref: gitlab-firmware`, never a URI with credentials, never a key.

---

## §6 SKILL RECIPE SPECIFICATION

### 6.1 Schema (`packages/slas-schemas/skill.schema.json` is generated from this)

```yaml
skill:
  id: string                    # slug, unique in the library
  name: string                  # shown in the UI
  version: semver
  description: string
  agents: [coding | validation | factory]       # where it may be enabled
  requires: [screen | ssh | redfish | files | network]   # capabilities the user must hold
  timeout_s: int                # whole-skill wall clock, default 900
  inputs:                       # bound at run time; {{ name }} in steps
    <name>: { type: string|int|bool|path|target_ref|secret, required: bool, default: any }
  steps: [ <step>, ... ]        # ≤ 200 after loop expansion
  on_failure: stop | screenshot_and_stop | continue | { retry: { max: int, delay_s: int } }
  outputs:                      # optional, exposed to the parent plan / SOP
    <name>: { from: <step_id> }
  sop_summary: { en: string, zh: string }        # optional; kernel generates if absent
```

A step is `{ <primitive>: { <args> }, id?: string, when?: <expr> }`.

### 6.2 Primitive whitelist (the only verbs a skill may use)

| Primitive | Args | Needs | Risk |
|---|---|---|---|
| `focus_window` | `title` \| `class` | screen | safe |
| `click` / `double_click` / `right_click` | `text` \| `image` \| `target` \| `x,y` | screen | safe |
| `type` | `text` (may reference `{{ secret }}`; never logged) | screen | safe |
| `key` | `press` (`Enter`, `Tab`, `ctrl+s`…) | screen | safe |
| `scroll` | `direction`, `amount` | screen | safe |
| `wait_for` | `window` \| `text` \| `image`, `timeout_s` | screen | safe |
| `screenshot` | `name` | screen | safe |
| `assert_visible` | `text` \| `image`, `message` | screen | safe |
| `run` | `command: [argv]`, `cwd`, `expect_exit`, `timeout_s`, `capture: true` | files (sandbox) or ssh (target) | caution |
| `ssh` | `target: {{ target_ref }}`, `command: [argv]`, `timeout_s` | ssh | caution |
| `copy` | `from`, `to` (within workspace or via ssh) | files/ssh | caution |
| `redfish` | `target`, `action: get_power_state|get_sel|get_inventory|power_on|power_off|force_off|graceful_restart` | redfish | `get_*` safe · power actions **destructive** |
| `sel_snapshot` / `inventory_snapshot` | `target` | redfish/ssh | safe |
| `wait` | `seconds` (≤ 3600) | — | safe |
| `set` | `var`, `value` (from a previous step's output) | — | safe |
| `assert` | `condition`, `message` | — | safe |
| `if` / `foreach` | `condition` \| `items` (bounded ≤ 100), `then`, `else` | — | inherits |

No `shell`, no `eval`, no `python`, no `download`, no `sudo`. `run` executes argv only;
strings are never joined into a shell line. Firmware flash, secure erase and BIOS reset
are **not skill primitives** — they exist only as Validation plan primitives behind INV-7.

### 6.3 Examples

```yaml
skill:
  id: station-login-burnin
  name: Log in to the test station and start BurnIn
  version: 1.0.0
  agents: [factory]
  requires: [screen, ssh]
  inputs:
    station: { type: target_ref, required: true }
    user:    { type: string, default: "operator" }
    password:{ type: secret, required: true }
  steps:
    - focus_window: { title: "Login" }
    - click:        { target: "#username" }
    - type:         { text: "{{ user }}" }
    - key:          { press: "Tab" }
    - type:         { text: "{{ password }}" }
    - key:          { press: "Enter" }
    - wait_for:     { window: "BurnIn v3.2", timeout_s: 60 }
    - click:        { text: "Start test" }
    - ssh:          { id: status, target: "{{ station }}", command: ["burnin-ctl","status","--json"] }
  outputs: { burnin_status: { from: status } }
  on_failure: screenshot_and_stop
```

```yaml
skill:
  id: sel-collect-clear
  name: Collect and clear the BMC event log
  version: 1.2.0
  agents: [validation, factory]
  requires: [redfish]
  inputs: { target: { type: target_ref, required: true } }
  steps:
    - redfish:      { id: sel, target: "{{ target }}", action: get_sel }
    - copy:         { from: "{{ steps.sel.file }}", to: "logs/sel-before.json" }
    - assert:       { condition: "{{ steps.sel.count }} < 4000", message: "SEL nearly full before clearing" }
  outputs: { sel_file: { from: sel } }
  on_failure: stop
```

---

## §7 MODEL MANAGEMENT
One registry, `Models/models.yaml`; code asks for **roles** (`coder`, `planner`, `triage`,
`embed`, `rerank`, plus `voters[]` for the Consensus Router); swaps are blue/green from the
Models page or `slas model swap`, with 24 h rollback (INV-9). vLLM has no native base-model
hot-swap; the Model Manager provides one by starting the candidate alongside, smoke-testing,
switching the gateway route, and draining the incumbent. `slas model fit` states VRAM need
vs. free in a sentence before any load. Quantisation: FP8 on Hopper/Blackwell, AWQ 4-bit on
Ada/Ampere, one BF16 reference kept for eval regression. `--enable-prefix-caching` and
`--guided-decoding-backend xgrammar` are mandatory on every generate instance.

## §8 LLMOps
**8.1 Eval** — Ragas/TruLens configured against local vLLM only (`slas_eval/judges.py` is
the single construction site; CI asserts no `api.openai.com`). Gates (prod): tool-call
schema validity ≥ 98%, plan validity ≥ 99%, RAG faithfulness ≥ 0.90, triage recall ≥ 0.95,
consensus agreement with golden labels ≥ 90%, SOP terminology consistency 100%, destructive
refusal without approval 100%. Golden set of labelled failures lives in `docs/golden-set/`.
**8.2 Observability** — vLLM `/metrics`, DCGM, plus `slas_agent_turns_total`,
`slas_consensus_votes_total{decision,verdict}`, `slas_consensus_disagreements_total`,
`slas_skill_runs_total{skill,outcome}`, `slas_screen_steps_total{primitive,outcome}`,
`slas_ticket_state_changes_total`, `slas_sop_exports_total{lang}`. Six Grafana dashboards
as code. One `trace_id` from WebUI to vLLM to executor.
**8.3 Search** — Option A (default): internal corpus hybrid search over datasheets, specs,
past tickets and SOPs (dense + FTS → RRF → rerank). Option B: SearXNG in a DMZ with
allowlisted egress, an INV-1 exception needing an ADR. Never call B "air-gapped".
**8.4 Backups** — quickstart nightly dump + Qdrant snapshot + MinIO mirror + station
state backups; prod pgBackRest PITR, object-lock, quarterly restore drill with recorded RTO.

## §9 WEBUI
Ten rules: plain language; one primary action per screen; say what will happen before it
happens; live progress for anything over 5 s; three-part errors (what happened, likely
cause, what to do); human time; progressive disclosure; no dead ends; dense but calm; WCAG
AA. Anti-patterns rejected in review: nested modals/tabs, raw JSON as primary content,
auto-refresh that steals scroll, toasts for errors that need action, settings needing a
restart, URL-only pages.

**Pages:** Home · Coding · Validation · Factory · Runs · Tickets · Models · Skills ·
Knowledge · Admin. Adding a page needs an ADR. Inside Coding, each project has a **Git
panel** (Status · Commit · History · Push/Pull · Bundle) and a **Terminal** tab that runs
inside the sandbox. Users manage their remotes under Settings → Git remotes (paste-only
fields, fingerprint shown after save, "Test connection"); admins manage the host allowlist
under Admin → Git hosts.

**Every agent has a "New …" wizard, always three steps, always ending in a sentence that
says what will happen and a verb button:**

| Wizard | Step 1 | Step 2 | Step 3 (finish) |
|---|---|---|---|
| New coding task | Plan: drop/paste `plan.md`, task name (languages are detected from the plan) | Setup: **languages only** — Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config — with an **optional** version per language (empty = the agent picks the newest bundled toolchain and says so); isolation, skills, cross-check, export target (ZIP · a Git remote you have added · bundle) | Review the agent's proposed steps (editable); the sentence names the resolved toolchain → **Start task** (creates T-coding-…) |
| New validation run | Suite: upload `.md`/`.xlsx`, see parsed items, destructive items flagged | Target: pick a free server; credentials come from the vault, never shown | Review & approve: sentence, cross-check votes, guardrails → **Approve and start** |
| New factory job | Trigger: scan label / MES ticket / pick station | Test loop: template, steps, skills used | Rules: voters, on-fail behaviour, exports, backup → **Start job** |

Live views: Validation's LED cycle map with console; Factory's test-step map with the
station screenshot strip; Coding's plan checklist with activity feed. Every finding is a
sentence with "Review ticket".

## §10 AGENT WORKFLOWS

### 10.1 Coding Agent
```
INGEST  plan.md → task breakdown (schema) → user edits/approves → ticket T-coding-n
ACT     Zone A sandbox (gVisor; virtual desktop via Zone S only if the plan needs an IDE/GUI)
        toolchain: the user picks LANGUAGES only (Python, C, C++, Rust, Shell, Go, TypeScript,
        YAML/JSON config); a version is optional. The agent resolves the newest version in the
        offline toolchain bundle, records the choice on the ticket and in the first feed line,
        honours a pinned version if the bundle has it, and otherwise says so in a sentence and
        falls back — it never guesses a version that isn't installed
        loop ≤ max_iterations: retrieve → generate edits → apply → lint/type/build/test
        skills enabled for coding expand here (e.g. lint-and-test)
        no progress for 3 iterations → stop, ask the human
CHECK   Consensus Router on the final diff (3 voters, majority, concerns surfaced)
SOP     code walkthrough EN + 中文: what changed, files, how to verify
EXPORT  ZIP → Artifacts (always available)
        Git — three paths, one repo (§5.7):
          local   the agent has already committed on a branch in Projects/<slug>/.git;
                  the user inspects, amends or continues in the Terminal / file explorer
          remote  "Push to <remote>" → git-broker: validation gate → Consensus Router on
                  the agent-authored diff → branch push → PR/MR where the host supports it
                  → a human merges. Credentials never leave the broker (INV-14).
          bundle  export a .bundle for sneakernet to another site; import the reply the same way
```
### 10.2 Validation Agent
```
COMPILE .md/.xlsx → normalized items → plan.yaml (schema) — LLM as compiler, once
        reject unknown primitive · count > cap · destructive without approval flag
GATE    human approval (sentence) + Consensus Router on the plan (unanimous)
ACT     deterministic state machine, zero LLM: lease target → SOL + syslog on → baseline
        snapshot → per cycle: ARM(journal) → QUIESCE(sync, SEL, fence) → ACT(Redfish) →
        SETTLE(SOL/SSH, timeout) → VERIFY(diff vs baseline: counts, PCIe LnkSta width AND
        speed, firmware, AER/EDAC/MCE/Xid, new SEL) → GATE (3 consecutive boot fails → abort)
        GUI steps (vendor tools with a UI, BIOS setup over KVM) run through the screen driver
ANALYSE fingerprint → dedup → RAG RCA → Consensus (owner/severity per field) → bug tickets
        `[Issue] PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle | [Owner] EE`
SOP     verification SOP EN + 中文 attached to the run and to each bug ticket
```
Guardrails (`config/guardrails.yaml`): `max_cycles_per_run 100 · min_settle_s 10 ·
min_ac_settle_s 30 · boot_timeout_s 900 · consecutive_failure_abort 3 · exclusive lease ·
max_run_hours 72 · requires_approval [ac_cycle, firmware_flash, secure_erase, bios_reset,
raid_reconfigure]`.

### 10.3 Factory Agent
```
TRIGGER production ticket from MES (adapter) · label scan · manual → T-factory-n
PLAN    pick test-loop template (Factory/Templates) → steps + skills → plan.yaml
        station lease (exclusive); unit SN bound to the ticket
ACT     deterministic loop on the station via the station runner: power on → GUI login →
        start vendor test app → read results from the screen and logs → sensors over SSH →
        every GUI step screenshot before/after → event log must be empty
VERDICT Consensus Router: PASS requires 3/3; otherwise line lead decides in the UI
        FAIL → unit stays on, station held, draft ticket for the line lead
SOP     production line SOP EN + 中文 attached to the ticket; returned to MES with verdict
BACKUP  station system state → Backups/stations/<n>/<ticket>/ (config, logs, app versions)
```

## §11 CODING STANDARDS
Python 3.12 · `uv` · `ruff` · `mypy --strict` · Pydantic v2 at every boundary · `structlog`
JSON with `trace_id` · async I/O with explicit timeouts · argv lists only, secrets via
env/stdin never argv · parameterised SQL · uploads type-sniffed and size-capped, `.xlsx`
parsed with macros/external entities off · TypeScript strict, no `any` without a comment.

**Separation:** `apps/api` (authz, tickets, approvals; no hardware, no LLM) ·
`services/orchestrator` (kernel; never hardware directly) · `services/llm-gateway` (only
component talking to vLLM; hosts the Consensus Router) · `services/model-manager` (only
one starting inference containers) · `services/sandbox-manager` · `services/screen-worker`
· `services/git-broker` (only component holding Git credentials or reaching Git hosts)
· `services/validation-executor` and `services/factory-executor` (only ones touching
targets, via `slas_hal` / station runner) · `packages/slas-kernel`, `slas-hal`,
`slas-skills`, `slas-authz` (shared; authz runs where the action executes).

**Tool-call fallback tiers:** 0 constrained decoding (`guided_json`, mandatory) → 1 retry
≤ 2 with the validation error as a tool result → 2 simplified schema + one-shot → 3
escalate to `planner` → 4 deterministic extraction (never for destructive ops) → 5 human
queue (release leases first). Never pass a partially valid call, guess an argument, or run
raw model text.

**Errors** carry `what_happened / likely_cause / what_to_do`; the UI renders exactly those.
**Tests:** unit · integration (ephemeral services) · `hal/` and `screen/` against recorded
fakes (including ugly fixtures) · `skills/` schema + compile + run against fakes · `eval/`
nightly · `e2e/` Playwright · `deploy/` fresh-VM `install.sh` under egress-DROP. Coverage
≥ 85% on `slas-kernel`, `slas-hal`, `slas-skills`, `slas-authz`. No test needs real
hardware, a real display, or a live model.

**Definition of done:** tests + coverage · ruff/mypy clean · egress-DROP green ·
trace_id + metrics · typed three-part errors · secrets via .env/Vault · authz at executor
· hardware via HAL, GUI via screen driver · kernel-only for tickets/RCA/SOP/skills · UI copy
per §9 · both-language export produced · install.sh still green · ADR if a boundary moved
· this file updated if an invariant or interface changed.

## §12 DOCKER COMPOSE BLUEPRINT (conceptual; `compose/` holds the real files)

```yaml
x-airgap: &airgap { DO_NOT_TRACK: "1", HF_HUB_OFFLINE: "1", TRANSFORMERS_OFFLINE: "1", HF_HUB_DISABLE_TELEMETRY: "1" }
x-common: &common { restart: unless-stopped, env_file: [.env], logging: { driver: json-file, options: { max-size: "50m", max-file: "5" } } }
networks:
  slas-edge: {}                                   slas-frontend: { internal: true }
  slas-backend: { internal: true }                slas-inference: { internal: true }   # no egress
  slas-knowledge: { internal: true }              slas-observability: { internal: true }
  slas-screen: { internal: true }                 # screen-worker ↔ orchestrator only
  slas-lab: {}                                    # validation-executor ONLY (macvlan in prod)
  slas-factory: {}                                # factory-executor ONLY
  slas-git: {}                                    # git-broker ONLY; egress allowlisted to config/git-hosts.yaml
services:
  edge:        { <<: *common, image: registry.internal/slas/edge@sha256:…, networks: [slas-edge, slas-frontend], ports: ["443:443"] }
  webui:       { <<: *common, image: registry.internal/slas/webui@sha256:…, networks: [slas-frontend] }
  api:         { <<: *common, image: registry.internal/slas/api@sha256:…, networks: [slas-frontend, slas-backend, slas-observability],
                 volumes: ["${SLAS_DATA_ROOT}:/data"] }                                   # includes Ticket Service
  agent-core-orchestrator:                                                                # runs the Agent Kernel
               { <<: *common, image: registry.internal/slas/orchestrator@sha256:…,
                 networks: [slas-backend, slas-inference, slas-knowledge, slas-screen, slas-observability],
                 volumes: ["${SLAS_DATA_ROOT}:/data"] }
                 # NOTE: no X11 socket, no /dev/input here (INV-4). GUI control goes through screen-worker.
  screen-worker: { <<: *common, image: registry.internal/slas/screen-worker@sha256:…,     # Xvfb + x11vnc + noVNC + screen driver
                 networks: [slas-screen, slas-frontend],                                  # frontend only for the operator's VNC view
                 environment: { <<: *airgap, DISPLAYS_PER_WORKER: "8", ACTION_RATE_LIMIT: "10" },
                 security_opt: ["no-new-privileges:true"], cap_drop: [ALL], shm_size: 1g }
  llm-gateway: { <<: *common, image: registry.internal/slas/llm-gateway@sha256:…,        # includes Consensus Router
                 networks: [slas-backend, slas-inference, slas-observability],
                 environment: { <<: *airgap, SCHEMA_ENFORCE: strict, CONSENSUS_DEFAULT_VOTERS: "3", CONSENSUS_TOKEN_BUDGET_PCT: "5" } }
  model-manager: { <<: *common, image: registry.internal/slas/model-manager@sha256:…,
                 networks: [slas-backend, slas-inference],
                 volumes: ["/run/podman/podman.sock:/run/podman/podman.sock", "${SLAS_DATA_ROOT}/Models:/data/Models"] }
  # vllm-cluster: vllm-coder, vllm-planner, vllm-triage, vllm-embed, vllm-rerank are CREATED by model-manager
  #               from Models/models.yaml (networks: [slas-inference], ipc: host, shm 16g, VLLM_NO_USAGE_STATS=1, GPU ids per role).
  vector-db:   { <<: *common, image: registry.internal/qdrant/qdrant@sha256:…, networks: [slas-knowledge, slas-observability],
                 environment: { QDRANT__TELEMETRY_DISABLED: "true" } }
  local-search-api: { <<: *common, image: registry.internal/slas/local-search-api@sha256:…,  # §8.3 Option A by default
                 networks: [slas-knowledge, slas-inference], environment: { SEARCH_MODE: internal_corpus } }
  sandbox-manager: { <<: *common, image: registry.internal/slas/sandbox-manager@sha256:…, networks: [slas-backend],
                 environment: { DEFAULT_RUNTIME: runsc, DEFAULT_NETWORK: none, PIDS_LIMIT: "512" },
                 volumes: ["/run/podman/podman.sock:/run/podman/podman.sock", "${SLAS_DATA_ROOT}/Coding:/data/Coding"],
                 security_opt: ["no-new-privileges:true"], cap_drop: [ALL] }
                 # sandbox images ship `git` for local commits; sandboxes get NO remote route and NO credentials (INV-14)
  git-broker:  { <<: *common, image: registry.internal/slas/git-broker@sha256:…,           # §5.7 — the ONLY holder of Git credentials
                 networks: [slas-backend, slas-git],                                       # ONLY member of slas-git
                 environment: { <<: *airgap, GIT_HOSTS_ALLOWLIST: /etc/slas/git-hosts.yaml, GIT_CONFIG_NOSYSTEM: "1", GIT_TERMINAL_PROMPT: "0",
                                CRED_STORE: "postgres+aesgcm" },                           # prod override: CRED_STORE: vault
                 volumes: ["${SLAS_DATA_ROOT}/Coding:/data/Coding", "./config/git-hosts.yaml:/etc/slas/git-hosts.yaml:ro"],
                 tmpfs: ["/run/slas-keys:mode=700,size=16m"],                              # per-operation SSH key files, shredded after use
                 security_opt: ["no-new-privileges:true"], cap_drop: [ALL] }
  validation-executor: { <<: *common, image: registry.internal/slas/validation-executor@sha256:…,   # + NVQual, MFT, fio, stress-ng, perftest
                 networks: [slas-backend, slas-observability, slas-lab],
                 environment: { GUARDRAIL_POLICY: /etc/slas/guardrails.yaml, SYSLOG_LISTEN: "0.0.0.0:5514", LLM_IN_CONTROL_LOOP: "false" } }
  factory-executor: { <<: *common, image: registry.internal/slas/factory-executor@sha256:…,
                 networks: [slas-backend, slas-observability, slas-factory],
                 environment: { MES_ADAPTER: file_drop, RUNNER_MTLS_CA: /etc/slas/runner-ca.pem, LLM_IN_CONTROL_LOOP: "false" } }
  postgres:    { <<: *common, image: registry.internal/library/postgres@sha256:…, networks: [slas-backend, slas-knowledge] }
  redis:       { <<: *common, image: registry.internal/library/redis@sha256:…, networks: [slas-backend] }
  minio:       { <<: *common, image: registry.internal/minio/minio@sha256:…, networks: [slas-backend] }
  prometheus:  { <<: *common, image: registry.internal/prom/prometheus@sha256:…, networks: [slas-observability, slas-inference, slas-backend, slas-knowledge] }
  grafana:     { <<: *common, image: registry.internal/grafana/grafana@sha256:…, networks: [slas-frontend, slas-observability],
                 environment: { GF_ANALYTICS_REPORTING_ENABLED: "false", GF_ANALYTICS_CHECK_FOR_UPDATES: "false", GF_INSTALL_PLUGINS: "" } }
  dcgm-exporter: { <<: *common, image: registry.internal/nvidia/dcgm-exporter@sha256:…, networks: [slas-observability], cap_add: [SYS_ADMIN] }
  node-exporter: { <<: *common, image: registry.internal/prom/node-exporter@sha256:…, networks: [slas-observability], pid: host }
  # prod profile: vault, keycloak, loki, tempo, backup-runner (pgBackRest + snapshots + station backups)
```

Start order: `postgres → redis → minio → model-manager (→ vllm-*) → llm-gateway →
vector-db → local-search-api → api → agent-core-orchestrator → screen-worker →
sandbox-manager → validation-executor → factory-executor → observability → edge → webui`.

## §13 REPOSITORY LAYOUT
```
sw-local-agent-service/
├── CLAUDE.md  README.md  install.sh
├── docs/{DEVELOPMENT_PLAN.md, PROMPTS.md, adr/, runbooks/, ui/, ui-demo/, golden-set/, glossary.yaml}
├── apps/{webui, api}
├── services/{agent-core-orchestrator, llm-gateway, model-manager, sandbox-manager, screen-worker,
│             git-broker, validation-executor, factory-executor, station-runner, local-search-api, edge}
├── packages/{slas-kernel, slas-schemas, slas-authz, slas-hal, slas-skills, slas-screen, slas-git,
│             slas-diff, slas-triage, slas-rag, slas-sop, slas-eval, slas-cli}
├── plans/{schema/plan.schema.json, primitives/}          # Validation/Factory plan verbs
├── skills/{schema/skill.schema.json, library/}           # shipped skills
├── templates/factory/                                    # test-loop templates
├── images/{sandbox-*, screen-worker, validation-executor, factory-executor}
├── compose/{docker-compose.yml, prod.override.yml, macvlan.override.yml}
├── config/{.env.example, guardrails.yaml, redaction.yaml, rbac-roles.yaml, consensus.yaml, git-hosts.yaml}
└── tests/{unit, integration, hal, screen, skills, eval, e2e, deploy}
```

## §14 GLOSSARY
Target / SUT · BMC · Redfish · SEL · SOL · warm boot · DC cycle (BMC stays up) · AC cycle
(BMC cycles too) · LnkSta · AER / EDAC / MCE · Xid · NVQual · MFT · GPUDirect · fence marker
· fingerprint · **role** (named inference slot) · **voter** (model in a cross-check) ·
**kernel** (shared agent lifecycle) · **skill** (YAML recipe) · **station runner** (daemon on
a physical test station) · **MES** (manufacturing execution system) · **remote** (a Git
repository the user has registered with a credential reference) · **broker** (`git-broker`,
the only service that touches remotes) · **bundle** (`git bundle` file for offline transfer).

## §15 CHANGE MANAGEMENT
ADRs in `docs/adr/NNNN-title.md` (Status, Context, Decision, Consequences, Invariants
touched). Required for: new dependency, container, page, boundary change, data-model
change, any invariant relaxation. Update this file when an invariant, boundary, phase or
public interface changes; bump the version; reference the ADR.

**Open decisions:** (1) user/target/station counts → single node vs. multi-node; (2) GPU
budget → which roles co-reside and whether 3 voters can be resident at once; (3) AC cycling:
PDU vs relay; (4) BMC fleet homogeneity → HAL quirk scope; (5) `.xlsx` suite schema fixed?
→ parser replaces LLM extraction; (6) search Option A/B; (7) Git target (GitLab/Gitea);
(8) MES integration: file drop, REST, or database; (9) Chinese default zh-Hant confirmed?
(10) Git host allowlist — internal GitLab/Gitea only, or is `github.com` an approved INV-1
exception? (11) Default remote auth — deploy keys / project tokens recommended over personal
credentials; confirm the policy.

*End of CLAUDE.md*
