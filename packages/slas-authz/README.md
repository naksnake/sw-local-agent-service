# slas-authz

Roles and capabilities. Authorization runs where the action executes (CLAUDE.md §11); in
Phase 1 that is `apps/api`.

- `capabilities.py` — the closed `Capability` set: skill `requires` names, the `git:*` names
  from §5.7, `admin:*` for the Phase 1 pages, and reserved later-phase names.
- `roles.py` — `Role`, `RoleSet`, validation of a parsed roles file with three-part errors,
  the shipped `DEFAULT_ROLES`, and a renderer that keeps `config/rbac-roles.yaml` in step.
- `principal.py` — `Principal` (a per-request snapshot) and the `SYSTEM` principal.
- `decide.py` — pure `decide()` / `require()`; a denial is a `ThreePartMessage`.

Role definitions are configuration: the api mounts `config/rbac-roles.yaml` read-only and
re-reads it when it changes. Assigning a role to a person is data. See ADR-0006.
