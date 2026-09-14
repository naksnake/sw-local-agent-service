# services/validation-executor

Deterministic state machine driving target servers through `slas_hal` (CLAUDE.md §4.1 Zone
B, §10.2, INV-3). Sole member of the lab network. Zero LLM: the kernel hands it plan steps,
it performs them and hands back observations with findings.

| Module | What it does |
|---|---|
| `guardrails.py` | `Guardrails` rendered to `config/guardrails.yaml` (max cycles, settle floors, boot timeout, consecutive-failure abort, exclusive lease, max run hours, what needs approval); `check_plan` names every broken guardrail as a sentence. |
| `leases.py` | Exclusive target leases in one JSON file, so a restarted executor still knows who holds what. |
| `cycle.py` | One power cycle: ARM → QUIESCE (sync, SEL before, fence marker into console and syslog) → ACT (journalled ahead, then the power action) → SETTLE (wait for the OS) → VERIFY (`slas_diff` against the baseline). Every phase is written to `Runs/<ticket>/cycles/<n>/journal.jsonl` before it happens; a resumed cycle whose ACT was journalled continues at SETTLE and never powers the target twice. |
| `executor.py` | `ValidationExecutor` behind the kernel's `Executor` protocol: lease, console, baseline, power cycles with the GATE (three boot failures in a row abort the run, exit 3), SEL and inventory snapshots, stress and diagnostics over SSH argv, destructive primitives (refused unless the step is marked destructive, which is what made the kernel ask for approval), log collection, release. Keeps the LED cycle map in `Runs/<ticket>/cycles.json`. |

Real drivers (Redfish, IPMI, SSH, PDU, SOL capture, syslog receiver) and the HTTP surface
arrive in P8; everything here runs and is tested against `slas_hal.fakes`.
