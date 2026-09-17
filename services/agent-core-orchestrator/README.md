# services/agent-core-orchestrator

Runs the Agent Kernel: ingest → ticket → plan → act → logs → RCA → SOP. Never touches hardware
directly (CLAUDE.md §11).

## `slas_orchestrator.coding` — the Coding Agent (P6)

| Module | What it does |
|---|---|
| `plan_doc.py` | `parse_plan()`: plan.md → title, task bullets, detected languages. |
| `breakdown.py` | `Breakdown` (tasks, languages with optional versions, isolation, skills, cross-check, export target) that a person edits and approves in the wizard; `Breakdowner` protocol with a deterministic fallback; the wizard's closing sentence. |
| `agent.py` | `CodingAgent`: the four `Agent` methods plus `propose()`/`approve()` for the wizard. `plan()` resolves toolchains and records the choice as the first step and in the plan summary. |
| `executor.py` | `CodingExecutor` (kernel-side, deterministic): opens the sandbox, runs the iterate loop with the `Coder` protocol and stall detection after 3 iterations without progress, commits with `Slas-Agent`/`Slas-Ticket` trailers, exports a ZIP, sends the final diff to the Consensus Router. |
| `export.py` | Reproducible ZIP of the project without `.git`. |

| `coder.py` | `GatewayLike` (the gateway's `complete`/`generate`/`cross_check` signatures) and `GatewayCoder`: `Coder.propose_edits` through `generate(role="coder", …, EditSet)`; the prompt carries the task, the file snapshot and the last check output, never a credential. |

## `slas_orchestrator.service` — the HTTP surface (round 2, ADR-0015)

`slas-orchestrator serve` reads the settings from the environment (`SLAS_GATEWAY_URL`,
`SLAS_SANDBOX_MANAGER_URL`, `SLAS_GIT_BROKER_URL`, `SLAS_VALIDATION_EXECUTOR_URL`,
`SLAS_FACTORY_EXECUTOR_URL`, `SLAS_DATA_ROOT`, `SLAS_BIND`, `SLAS_GLOSSARY`,
`SLAS_OWNER_ROUTING`) and serves `docs/api-contract-round-2.md` §5.

| Module | What it does |
|---|---|
| `settings.py` | `Settings.from_env()`; which health checks are mandatory (gateway, sandbox manager). |
| `app.py` | `create_app(...)` with every collaborator injectable; `/health` per the contract (optional zones reported, never fatal); the lazy `slas_llm_gateway.client.HttpGateway` import; one kernel per run (`build_coding_kernel`). |
| `runs.py` | `RunRegistry`: one daemon thread per `Kernel.run()`, the ticket id learned through `TrackingStore`, a run that raises ends its ticket Failed with the sentence journalled. |
| `coding.py` · `skills.py` · `tickets.py` | The routers of contract §5 for `/v1/coding`, `/v1/skills`, `/v1/tickets`. `validation.py` and `factory.py` arrive with the executor slice and are included in `app.py`. |
| `views.py` | `coding_task_view()` (steps from the plan and the step records, feed from the journal, first line the toolchain choice) and the Home ticket rows. |
| `deps.py` | The `Deps` container the routes share; `workspace_user()` (the email's local part is the name on disk and on tickets). |

`slas_orchestrator.clients.HttpSandboxManager` is the sandbox manager over HTTP: it satisfies
the executor's `SandboxAccess` protocol, computing paths locally (both containers mount the
same `/data`) and sending open/exec over the wire; `TicketBoundExecutor` binds the ticket
each step runs for, so the session request can name it.

## `slas_orchestrator.validation` — the Validation Agent (P7)

| Module | What it does |
|---|---|
| `suite.py` | INGEST: `suite.md` (table matched by header name, or bullets such as `- DC cycle x25, settle 60 s`) or `suite.xlsx` (standard-library zip + XML reader; macros are never read, no external entities) → `Suite` of `SuiteItem`s with cycles, parameters and the author's `approved` flag. |
| `compiler.py` | COMPILE: deterministic action table first, a `Compiler` protocol (the model as compiler, once) for the rest, then the reject rules from §10.2 — unknown primitive, missing arguments, destructive without the approval flag, more cycles than `max_cycles_per_run`, more than 200 steps — and `check_plan` against the guardrails. Cycles unroll into one `power_cycle` step each so the kernel journal gives crash recovery per cycle. Renders `plan.yaml` (+ `.json`). |
| `agent.py` | `ValidationAgent`: the four `Agent` methods plus `choose_target()` and `destructive_items()` for the wizard. Destructive steps become kernel approvals (INV-7); the kernel sends every Validation plan to the Consensus Router (unanimous, §5.3) before the human approves. |

The executor it drives is `services/validation-executor`; the kernel dedups the findings it
returns and spawns one child bug ticket per fingerprint across runs.
