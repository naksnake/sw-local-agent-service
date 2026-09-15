# ADR-0006: Roles, capabilities and where authorization runs

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md names capabilities in two places — the skill `requires` names in §6.1 (screen,
ssh, redfish, files, network) and the Git capabilities in §5.7 — and says `slas-authz`
holds "roles and capabilities from `config/rbac-roles.yaml`", but never enumerates the
roles. INV-9 says user changes never need a config edit or a restart. INV-12 says skills
consume the importing user's capabilities and cannot escalate them.

## Decision
- **Capabilities are a closed enumeration** in `slas_authz.Capability`: the five skill
  names, the eight `git:*` names from §5.7, `admin:people` and `admin:settings` for the
  Phase 1 pages, and three reserved for later phases (`approve:destructive`,
  `factory:verdict`, `model:manage`) so roles can name them now. An unknown name in a roles
  file is an error, not an ignored string. Only `admin:*` is enforced in Phase 1.
- **The shipped role set** (`slas_authz.roles.DEFAULT_ROLES`, rendered to
  `config/rbac-roles.yaml`):
  `administrator` (every capability except `git:push_protected`), `engineer` (the default
  for new people: the five skill capabilities, the Git capabilities except
  `git:push_protected` and `git:hosts_manage`, and `approve:destructive`), `line_lead`
  (engineer plus `factory:verdict`), `viewer` (read only). `git:push_protected` is granted
  to no role by default (§5.7). The id `system` is reserved.
- **Definitions are configuration; assignments are data.** The api mounts
  `config/rbac-roles.yaml` read-only at `/etc/slas/rbac-roles.yaml`, re-reads it when its
  mtime changes and keeps the last good set when an edit is invalid, reporting the problem
  in three parts. Which role a person holds lives in Postgres. Both kinds of change apply
  on the next request with no restart.
- **Authorization runs where the action executes.** In Phase 1 that is `apps/api`:
  `decide(principal, capability)` is pure, a denial is a `ThreePartMessage`, and the api
  maps it to a 403 with exactly that body. Executors in later phases call the same
  function with the same principal snapshot (§11).
- A `Principal` is a per-request snapshot (subject, display name, role, role label,
  capabilities). `SYSTEM` is the platform acting for itself — the installer's bootstrap and
  `slas user add` on the host — and holds every capability.

## Alternatives considered
- Free-form capability strings: flexible, but a typo in a roles or skill file would
  silently grant nothing or everything; a closed set fails loudly.
- Roles in the database with an editing UI: more to build, and role edits are rare
  decisions that belong in a reviewed file and an ADR.
- One `admin` capability for both pages: two names cost nothing and let a future role
  manage settings without managing people.

## Consequences
Easier: skills, the api and later executors share one vocabulary; the role file is
validated with sentences; tests cover every role × capability row. Harder: adding a
capability is a code change plus an ADR; the role set here is an assumption the product
owner should confirm (see the open questions in the Phase 1 plan).

## Invariants touched
INV-9 (roles and assignments change without config edits or restarts), INV-12 groundwork
(skills can only name capabilities from this set and never grant them), INV-14 (the Git
capability names are the ones the broker enforces). No invariant is relaxed.
