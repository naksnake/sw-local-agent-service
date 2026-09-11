# Architecture decision records

One file per decision, copied from `0000-template.md`, numbered in order (CLAUDE.md §15).
A record is required for a new dependency, container or page, a boundary or data-model
change, and any invariant relaxation. `tests/unit/test_adrs.py` checks the format.

| ADR | Title | Status |
|---|---|---|
| 0001 | One Agent Kernel, three thin agents | accepted |
| 0002 | A dedicated screen worker instead of the host's display | accepted |
| 0003 | The Phase 1 dependency set | accepted |
| 0004 | Install contract: file secrets, image lock, bundle | accepted |
| 0005 | Roles and capability names live in config/rbac-roles.yaml | accepted |
| 0006 | Settings store and user identity | accepted |
