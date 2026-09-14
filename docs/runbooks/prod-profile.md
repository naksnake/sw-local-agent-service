# The prod profile: installing and operating it

`./install.sh --profile prod` brings up the quickstart stack plus Vault, Keycloak, Loki,
Tempo, pgBackRest with object lock, and the Kata/Firecracker sandbox tier
(CLAUDE.md §3, ADR-0012). One command, two files to bring: the release bundle (or access to
Harbor inside the perimeter) and `config/cosign.pub`.

## 1 · Before the first install

| Item | Why |
|---|---|
| A build host ran `scripts/lock-images.sh --sign` and `scripts/build-bundle.sh --profile prod`, and the resulting `compose/images.lock.*` is committed | The installer refuses to start while any image the profile starts has no digest and image ID in the lock (INV-8). |
| `config/cosign.pub` from the release; fingerprint checked against the release notes | Every image and the bundle manifest are verified with it; the installer refuses without it. |
| cosign on the host (`tools/cosign` in the bundle), gVisor, Kata Containers with Firecracker (`kata-runtime`, `firecracker`), the NVIDIA container toolkit | `slas doctor --profile prod` checks all of them; the Kata tier is a warning, the rest fail. |
| Harbor, if installing from the registry: the release's signed images pushed with their cosign artifacts | `install.sh --registry harbor.internal` verifies each reference by digest before pulling. |
| A host name every browser will use (`SLAS_PUBLIC_HOST`) | Keycloak and the OIDC redirect are bound to it. |

## 2 · Install

```
tar xzf slas-bundle-<version>.tgz && cd slas-bundle-<version>
./install.sh --profile prod --dry-run       # read-only steps for real, the rest described
./install.sh --profile prod                 # the real thing
```

Order: preflight → verify (manifest signature, lock, manifest) → `.env` → secret files →
images → `docker compose up` → Vault bootstrap → wait healthy → the sign-in URL and the
one-time administrator password. Nothing changes until every read-only step passed;
"Nothing was changed on this host." is printed whenever one fails.

Overlays are chosen from `.env`: set `SLAS_LAB_*` for the lab VLAN and `SLAS_FACTORY_*` for
the factory LAN and the installer adds `macvlan.override.yml` and
`macvlan-factory.override.yml` (each executor gets its own address; nothing else on the host
is on those networks).

## 3 · Vault

The bootstrap (`deploy/prod/vault-bootstrap.sh`, run by the installer) enables KV v2 at
`slas/`, AppRole, one policy per service (`config/vault/policies/`) and one role each. The
installer writes each service's role id and secret id as Docker secret files; the token
lives in memory only. **Unseal keys** are printed once by `vault operator init`; store them
offline before anything else.

Where secrets live (mount `slas`):

| Path | Who reads | Referenced as |
|---|---|---|
| `lab/<target>/bmc` (`user`, `password`), `lab/<target>/ssh` (`private_key`) | validation-executor | `vault:slas/lab/<target>/bmc/password` on the target record |
| `factory/<station>/operator` (`password`) | factory-executor | `vault:slas/factory/<station>/operator/password` |
| `git/<ref>` | git-broker (read, write) | the remote's `credential_ref` |
| `platform/<name>` | api | settings that are secrets |

Rotate a service's AppRole secret id: `vault write -f auth/approle/role/<service>/secret-id`
inside the vault container, write the new id to `${SLAS_DATA_ROOT}/secrets/vault_approle_secret_id`
for that service, restart it. Rotate a target credential: `vault kv put slas/lab/<target>/bmc
user=… password=…`; the next run reads the new value (nothing restarts).

## 4 · Keycloak

The realm `slas` is imported at start from `config/keycloak/slas-realm.json`: client
`slas-webui` (confidential, PKCE), realm roles `administrator`, `engineer`, `line_lead`,
`viewer`, and the `slas_roles` claim the api reads. The sign-in page offers both modes
(`SLAS_AUTH_MODES=builtin,oidc`): built-in accounts keep working for the bootstrap
administrator and for break-glass. Administration: `https://<host>/auth/admin/` with the
password in `${SLAS_DATA_ROOT}/secrets/keycloak_admin_password`. Give a person a platform
role by giving them the realm role of the same name; no match means the default role.

## 5 · Sandboxes: the Kata tier

`SANDBOX_TIER=kata` (default in prod) runs every Coding Agent sandbox in its own Kata
micro-VM on Firecracker: a separate kernel, no shared host kernel surface. Without the
runtime installed the sandbox manager refuses to open a sandbox and says so; set
`SANDBOX_TIER=gvisor` in `.env` to run on gVisor meanwhile (still no fallback to runc in
prod). The same three mounts, no network and no capability apply in every tier (INV-4).

## 6 · Backups

`backup-runner` takes a full backup weekly and a differential daily (`SLAS_BACKUP_FULL_CRON`
weekday, `SLAS_BACKUP_DIFF_CRON` hour), checks the archive hourly, and snapshots Qdrant next
to every backup. The repository is the MinIO bucket `slas-backups` under **object lock in
compliance mode** for `SLAS_BACKUP_RETENTION_DAYS` days: nobody, root included, deletes a
backup early. Run artifacts go to `slas-artifacts` under governance lock for
`SLAS_ARTIFACT_RETENTION_DAYS` days.

```
slas backup status               # what is in the repository; the archive check
slas backup now --type full      # a backup now
slas backup drill --to <time>    # the restore drill (docs/runbooks/restore-drill.md)
```

## 7 · Logs and traces

Loki keeps 30 days of every container's JSON events; Tempo keeps 30 days of traces. Grafana
links them by `trace_id`: from a log line to its trace and back. The id is the one the
WebUI minted for the request (`docs/runbooks/observability.md`).

## 8 · Waiting on decisions and hosts

| Blocked | Why | Until then |
|---|---|---|
| `./install.sh --profile prod` on a clean host | No Docker, registry or clean host in the build environment | `tests/unit/test_install_prod.py` runs the installer against stub `cosign` and `docker` in dry-run and proves the order and the refusals |
| A filled image lock and a signed bundle | Needs a connected build host and the release key | `scripts/lock-images.sh`, `scripts/build-bundle.sh`; the lock ships unpinned and the installer refuses |
| The measured RTO | No Postgres, pgBackRest or MinIO here | The drill's phases and record are tested; the runbook's table names the missing row |
| `slas-api verify-restore` and the OIDC callback route | apps/api is not approved yet | `slas_authz.oidc` is the flow; the api mounts it |
| The `slas-health` helper in healthchecks | Ships in the first-party images (a 40-line static HTTP probe, no curl in slim images) | Named in compose; built by `scripts/build-bundle.sh` with the images |
