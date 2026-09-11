# ADR-0005: Roles and capability names live in config/rbac-roles.yaml

Status: accepted
Date: 2026-09-11

## Context
CLAUDE.md §13 makes `config/rbac-roles.yaml` a Phase 1 deliverable and §11 says authz runs
where the action executes, but no document names the roles or the capability strings beyond
the `git:*` set in §5.7 and the skill `requires` values in §6.1. Phase 1 must gate people and
settings management; Phases 2 to 9 gate approvals, leases, model swaps, skill imports and
knowledge uploads. If each phase invents names, the INV-7 approval gate has no fixed
capability to check and the UI demo's four roles cannot be mapped.

## Decision
One file, `config/rbac-roles.yaml`, holds the capability catalogue and the roles. Roles are
the UI demo's four plus a read-only viewer: `administrator`, `validation-engineer`,
`factory-lead`, `firmware-engineer`, `viewer`. Capability names are `area:verb` slugs; the
catalogue declares every name for every phase now, each with the phase that enforces it, so
later phases use fixed names. Phase 1 enforces `users:manage`, `settings:manage` and
`settings:read`. `git:push_protected` is held by no shipped role (off by default, §5.7).

`packages/slas-authz` is a small framework-free library: `RolesConfig` validates the file
(names, references, no duplicates, and at least one role holding both `users:manage` and
`settings:manage` so an installation cannot lock itself out), `principal_for` resolves a
user's capabilities from the role at request time, `authorize` returns `Allowed` or `Denied`
with a three-part sentence. Code checks capabilities, never role names.

The api mounts `${SLAS_DATA_ROOT}/config` read-only as a directory at `/etc/slas` and reads
`rbac-roles.yaml` through `RolesLoader`, which re-reads on inode or mtime change and keeps the
last valid configuration when an edit is invalid. A site can therefore reshape roles without
a restart (INV-9) and a typo never locks everyone out.

## Consequences
Easier: every later phase has its capability names; the People page can list roles and their
descriptions from the same file; tests build role sets with `slas_authz.fakes`. Harder: a new
capability is an edit to the catalogue and to the administrator role, reviewed like code;
per-target approval scopes ("runs on rack 2") are not expressible yet and stay for the
phase that introduces targets.

## Invariants touched
INV-7 — the approval capabilities `approve:destructive` and `approve:factory_verdict` are
named now so the gate has a fixed string to check; INV-11 stays untouched because a
capability, not a vote, is what authorises. INV-9 — role changes take effect without a
restart. No invariant is relaxed.
