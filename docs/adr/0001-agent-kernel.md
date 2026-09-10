# ADR-0001: One Agent Kernel, three thin agents

Status: accepted
Date: 2026-09-10

## Context
The platform ships three agents (Coding, Validation, Factory) that all need the same
things: a ticket for every job, a write-ahead journal for every action with physical effect,
log collection, root-cause analysis, dual-language SOPs, a multi-model cross-check, skill
expansion, approvals and exports. Built separately, these would drift in exactly the places
where the invariants bite: INV-6 (journalling), INV-7 (human approval of destructive steps),
INV-11 (a vote never authorises), INV-13 (both languages or neither). CLAUDE.md value 3 is
"one kernel, no silos", and §5.1 already fixes the lifecycle
(ingest → ticket → plan → act → collect → rca → sop → close).

## Decision
`packages/slas-kernel` (`slas_kernel`) implements the whole lifecycle once. An agent is a
thin specialisation that implements only the `Agent` protocol of CLAUDE.md §5.1
(`ingest`, `plan`, `verify`, `sop_template`; `iterate` for the Coding Agent if the review
patch is adopted). Tickets, the journal, log collection, RCA, SOP rendering, the cross-check
call, skill expansion, approvals and exports are kernel code and are forbidden in agent
modules. The deterministic executor that performs plan steps is driven by the kernel; a
model never performs an action itself (INV-3). Agent modules may not import gateway or HAL
clients directly; from P2 an import-linter contract enforces the boundary, and the weekly
health-check prompt in `docs/PROMPTS.md` audits for drift.

## Consequences
Easier: one place to test the invariant gates against fakes; identical ticket, RCA and SOP
behaviour in every agent; a new agent is a small module. Harder: agent-specific needs must
be expressed through the protocol rather than by reaching into the kernel, so the protocol
will grow deliberately (each addition is an interface change under §15); the kernel is on
the critical path of every phase from P2 onward, so its coverage floor is 85% (§11).

## Invariants touched
INV-3, INV-6, INV-7, INV-11 and INV-13 are upheld by construction: the gates live in one
place. No invariant is relaxed.
