# slas-deploy

Everything `install.sh` and `docker compose` read, rendered from code (CLAUDE.md §3, §12;
ADR-0003, ADR-0012). Standard library only; tests run against fakes and check the zone model
on the data before the YAML exists.

| Module | Owns |
|---|---|
| `compose.py` | The base stack (`compose/docker-compose.yml`), the prod override (Vault, Keycloak, Loki, Tempo, backup-runner, minio-init; Vault at dispatch, OIDC beside built-in auth, the Kata/Firecracker tier, pgBackRest archiving, object lock), and the macvlan overlays for the lab VLAN and the factory LAN. No `env_file`; file secrets; hardening on every service; only `edge` publishes a port. |
| `images.py` | The image lock (`compose/images.lock.yaml` and its JSON twin): every image by immutable tag, its digest and image ID once a build host fills them, which profile starts it. `check_lock` refuses an unpinned or `latest` image; `check_manifest` compares a bundle with the lock. |
| `cosign.py` | `CosignVerifier`: the bundle manifest by `verify-blob`, images in Harbor by `verify --key`; key-based, offline (`--insecure-ignore-tlog --private-infrastructure`). |
| `vault.py` | Vault's server file and one least-privilege policy per service; the one-time bootstrap commands (KV v2, AppRole per service). |
| `keycloak.py` | The realm import: one confidential client with PKCE, the platform roles as realm roles, the `slas_roles` mapper the api reads. |
| `pgbackrest.py` | `config/pgbackrest.conf` (MinIO repository under object lock, retention, async archiving), the PostgreSQL archive settings, the MinIO bucket initialisation with object lock, the backup schedule, `BackupRunner` (argv only) and `RestoreDrill` (phases, RTO, record under `Backups/drills/`). |
| `observability_prod.py` | Loki and Tempo configurations and the Grafana datasources that link logs and traces by `trace_id`. |
| `installer.py` | `python -m slas_deploy.installer write-env \| secrets \| check-lock \| check-manifest \| compose-files`: the parts of `install.sh` that are safer in Python. |
| `render.py` | `uv run python -m slas_deploy.render` writes every file above; a test keeps them in step. |

The runbooks are `docs/runbooks/prod-profile.md` and `docs/runbooks/restore-drill.md`.
