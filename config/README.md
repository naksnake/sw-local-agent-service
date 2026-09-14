# config

Runtime configuration files.

| File | Phase | What it is |
|---|---|---|
| `.env.example` | P0 | Template `install.sh` copies to `${SLAS_DATA_ROOT}/.env`; one sentence per key. |
| `rbac-roles.yaml` | P1 | Role definitions (label, description, capabilities) and the default role for new people. Rendered from `slas_authz.roles.DEFAULT_ROLES`; a test keeps the two in step. The api mounts it read-only at `/etc/slas/rbac-roles.yaml` and re-reads it when it changes, so an edit applies on the next request with no restart (INV-9). Assigning a role to a person is data in the database. Changing the role set needs an ADR (§15). |
| `redaction.yaml`, `consensus.yaml` | P3 | Redaction patterns and Consensus Router rules; rendered from code, tests keep them in step (CLAUDE.md §5.3). |
| `owner-routing.yaml` | P5 | Deterministic owner / component / severity routing for RCA findings (CLAUDE.md §5.4, §10.2). Rendered from `slas_kernel.rca.DEFAULT_OWNER_ROUTING`; first matching rule wins; no match means the owner is "your call". A model never assigns an owner. |
| `guardrails.yaml`, `git-hosts.yaml` | P6–P7 | See CLAUDE.md §5.7, §10.2. |

## rbac-roles.yaml format

```yaml
version: 1
default_role: engineer          # new people get this role
roles:
  <role_id>:                    # lowercase letters, digits, underscores; `system` is reserved
    label: "Engineer"           # what people see
    description: "One sentence."
    capabilities:               # names from slas_authz.Capability, no duplicates
      - screen
      - git:push_branch
```

Unknown capability names, a missing default role or a reserved id make the file unusable;
the api then keeps the last good set and reports the problem in three parts.
