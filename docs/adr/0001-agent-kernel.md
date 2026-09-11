# ADR-0001: One Agent Kernel shared by every agent

Status: accepted
Date: 2026-09-11

## Context
The platform ships three agents — Coding, Validation and Factory — that work on very
different machines (a sandbox, a target server, a factory station) but need the same
things: a ticket that tracks the job from ingestion to root cause, a write-ahead journal of
every action, continuous log collection, root-cause analysis over logs and knowledge, a
report and SOP in English and Chinese, cross-checks by several local models, human
approvals for destructive steps, and user-written skills. Building these three times would
produce three slightly different ticket models, three RCA pipelines and three places where
an invariant can quietly drift (CLAUDE.md §1.2 value 3, "One kernel, no silos").

## Decision
`packages/slas-kernel` implements the whole lifecycle once —
`ingest → ticket → plan → act → collect → rca → sop → close` (CLAUDE.md §5.1) — and exposes
exactly one abstract class:

```python
class Agent(Protocol):
    name: Literal["coding", "validation", "factory"]

    def ingest(self, raw: Upload | MesTicket) -> Job: ...
    def plan(self, job: Job) -> Plan: ...
    def verify(self, step: Step, obs: Observation) -> Verdict: ...
    def sop_template(self) -> SopTemplate: ...
```

An agent module is a thin specialisation that implements these four methods and nothing
else. Tickets, the journal, log collection, RCA, SOP rendering, the Consensus Router call,
skill expansion, approvals and exports are kernel code and are forbidden in agent modules
(CLAUDE.md §0.3, §5.1). Phase 2 proves the kernel with a `NullAgent` that runs five fake
steps end to end, including crash recovery from the journal, before any real agent exists.

## Consequences
Easier: one ticket schema, one journal format, one RCA pipeline and one SOP renderer to
test, audit and keep bilingual; a fix or an invariant applies to all three agents at once;
a fourth agent is a small module. The weekly health check in `docs/PROMPTS.md` can grep
for kernel concerns implemented outside the kernel.

Harder: agent-specific needs must be expressed through the four methods and the shared
schemas, so the kernel's interfaces need care; an agent cannot take a shortcut for its own
convenience. Kernel changes touch every agent and need the full test suite.

## Invariants touched
INV-3, INV-6, INV-7 and INV-11 are implemented once, in the kernel's deterministic executor,
journal, approval gate and consensus handling, rather than re-implemented per agent. INV-13
is implemented once in the kernel's SOP path. No invariant is relaxed.
