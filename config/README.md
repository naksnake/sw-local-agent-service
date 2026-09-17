# config

Runtime configuration files.

| File | Phase | What it is |
|---|---|---|
| `.env.example` | P0 | Template `install.sh` copies to `${SLAS_DATA_ROOT}/.env`; one sentence per key. `SLAS_RUNTIME_SOCKET` (empty = Podman's socket; the installer writes Docker's when only that one exists, ADR-0015) and `SLAS_GPU_VRAM_GIB` (per-GPU budget the model manager places instances against, default 270) and `SLAS_AGENTS` (which agents start, default `coding`, ADR-0017) are the round-2 host knobs. |
| `rbac-roles.yaml` | P1 | Role definitions (label, description, capabilities) and the default role for new people. Rendered from `slas_authz.roles.DEFAULT_ROLES`; a test keeps the two in step. The api mounts it read-only at `/etc/slas/rbac-roles.yaml` and re-reads it when it changes, so an edit applies on the next request with no restart (INV-9). Assigning a role to a person is data in the database. Changing the role set needs an ADR (§15). |
| `redaction.yaml`, `consensus.yaml` | P3 | Redaction patterns and Consensus Router rules; rendered from code, tests keep them in step (CLAUDE.md §5.3). |
| `model-sources.txt` | P3 | Where `scripts/fetch_models.py` gets the weights on a connected host: `<path> <owner/repo> <commit>` per model, every line pinned to a commit, in `[quickstart]` and `[prod]` sections that `--profile` selects. A test checks that each profile's lines name exactly the paths of `models.<profile>.yaml`. The platform never reads it (INV-1). |
| `models.quickstart.yaml`, `models.prod.yaml` | P3 | The model registry each profile starts with (CLAUDE.md §7): models, role assignments, voters. Rendered from `slas_model_manager.registry.PROFILE_REGISTRIES`; a test keeps them in step. `install.sh --models` copies the profile's file to `${SLAS_DATA_ROOT}/Models/models.yaml` when there is none and never overwrites one (INV-9); roles change on the Models page. They assume GPUs of about 288 GB (HGX B300 class). |
| `owner-routing.yaml` | P5 | Deterministic owner / component / severity routing for RCA findings (CLAUDE.md §5.4, §10.2). Rendered from `slas_kernel.rca.DEFAULT_OWNER_ROUTING`; first matching rule wins; no match means the owner is "your call". A model never assigns an owner. |
| `git-hosts.yaml` | P6 | The only Git hosts `git-broker` may reach (CLAUDE.md §5.7). Rendered from `slas_git.hosts.DEFAULT_GIT_HOSTS`; a test keeps them in step. Each host: name, hostname, kind (gitlab · gitea · github · generic), `api_base` for merge requests, allowed protocols, the account name sent with a token, and a pinned `ssh_host_key` before ssh is allowed. `github.com` or `gitlab.com` need an ADR first. Managed under Admin → Git hosts. |
| `bmc-quirks.yaml` | P8 | BMC quirk shims keyed by Manufacturer regex and inclusive firmware range (CLAUDE.md §15 open decision 4): SEL paging, where PCIe devices live, the BDF field path, Redfish reset-type overrides, IPMI-for-power, power-off settle, unreliable LanesInUse, hex SEL ids. Rendered from `slas_hal.quirks.DEFAULT_QUIRKS`; a test keeps them in step. Only the DMTF defaults and the recorded fixture BMC are filled in; a real vendor entry is added from a recorded answer, never from memory. |
| `guardrails.yaml` | P7 | Validation guardrails (CLAUDE.md §10.2): max cycles per run, settle floors for warm/DC and AC, boot timeout, consecutive-failure abort, exclusive lease, max run hours, and which step kinds need a per-run approval. Rendered from `slas_validation_executor.guardrails.DEFAULT_GUARDRAILS`; a test keeps them in step. The plan compiler refuses a plan that exceeds them; the executor checks them again at every cycle. |

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
| `factory.yaml` | P10 | Factory settings (CLAUDE.md §10.3): screenshot retention (days kept, longer for failed or held jobs, most per job), how long a station's one-time enrolment code lives and how many wrong codes lock it, the VNC port stations expose, and the station lease length. Rendered from `slas_factory_executor.settings.DEFAULT_FACTORY_SETTINGS`; a test keeps them in step. Per-station tuning and retention live on the station record under Admin → Stations. |
| `cosign.pub` (`cosign.pub.example` here) | P12 | The release public key `install.sh --profile prod` verifies every image and the bundle manifest with (ADR-0012). Committed from the release host; its fingerprint goes in the release notes. The private key never enters the repository. |
| `vault/vault.hcl`, `vault/policies/*.hcl` | P12 | Vault's server file (file storage under the data root, TLS listener on the backend network, no UI, no telemetry) and one least-privilege policy per service under the `slas` KV mount. Rendered from `slas_deploy.vault`; a test keeps them in step. |
| `keycloak/slas-realm.json` | P12 | The realm Keycloak imports at start: client `slas-webui` (confidential, PKCE), the platform roles as realm roles, the `slas_roles` claim mapper, brute-force protection. Rendered from `slas_deploy.keycloak`. |
| `pgbackrest.conf`, `postgres/prod.conf` | P12 | pgBackRest's repository on MinIO under object lock with retention, and the PostgreSQL archive settings for point-in-time recovery. Rendered from `slas_deploy.pgbackrest`. |
| `loki/loki.yml`, `loki/grafana-datasources.yml`, `tempo/tempo.yml` | P12 | Loki and Tempo for the prod profile and the Grafana datasources that link logs and traces by `trace_id`. Rendered from `slas_deploy.observability_prod`. |
