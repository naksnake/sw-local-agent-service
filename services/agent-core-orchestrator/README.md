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
