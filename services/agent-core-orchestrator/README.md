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

The gateway-backed `Coder`, `Breakdowner` and cross-checker adapters, and the service's HTTP
surface, wait on the dependency decisions; everything here runs against fakes.

## `slas_orchestrator.validation` — the Validation Agent (P7)

| Module | What it does |
|---|---|
| `suite.py` | INGEST: `suite.md` (table matched by header name, or bullets such as `- DC cycle x25, settle 60 s`) or `suite.xlsx` (standard-library zip + XML reader; macros are never read, no external entities) → `Suite` of `SuiteItem`s with cycles, parameters and the author's `approved` flag. |
| `compiler.py` | COMPILE: deterministic action table first, a `Compiler` protocol (the model as compiler, once) for the rest, then the reject rules from §10.2 — unknown primitive, missing arguments, destructive without the approval flag, more cycles than `max_cycles_per_run`, more than 200 steps — and `check_plan` against the guardrails. Cycles unroll into one `power_cycle` step each so the kernel journal gives crash recovery per cycle. Renders `plan.yaml` (+ `.json`). |
| `agent.py` | `ValidationAgent`: the four `Agent` methods plus `choose_target()` and `destructive_items()` for the wizard. Destructive steps become kernel approvals (INV-7); the kernel sends every Validation plan to the Consensus Router (unanimous, §5.3) before the human approves. |

The executor it drives is `services/validation-executor`; the kernel dedups the findings it
returns and spawns one child bug ticket per fingerprint across runs.
