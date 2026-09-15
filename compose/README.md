# compose

The platform's compose files, rendered from `slas_deploy.compose` (CLAUDE.md §12, ADR-0003,
ADR-0012); `uv run python -m slas_deploy.render` regenerates them and a unit test keeps file
and code in step. Never edit these files by hand.

| File | Phase | What it is |
|---|---|---|
| `docker-compose.yml` | P12 (P1 scope) | The quickstart stack: every zone and network of §12, hardened services, file secrets, no `env_file`, only `edge` publishing a port. Images by immutable tag from `images.lock.*`. |
| `prod.override.yml` | P12 | `-f docker-compose.yml -f prod.override.yml`: vault, keycloak, loki, tempo, backup-runner, minio-init; Vault at dispatch for the executors and git-broker; OIDC beside built-in auth on the api; the Kata/Firecracker tier for sandboxes; pgBackRest archiving on postgres; object lock on backups and artifacts. |
| `macvlan.override.yml` | P8 | Puts `validation-executor` on the lab VLAN with its own address (macvlan). Values from `.env`: `SLAS_LAB_IFACE`, `SLAS_LAB_SUBNET`, `SLAS_LAB_GATEWAY`, `SLAS_LAB_EXECUTOR_IP`. Targets send syslog to that address on UDP 5514. |
| `macvlan-factory.override.yml` | P12 | Puts `factory-executor` on the factory LAN with its own address. Values from `.env`: `SLAS_FACTORY_*`. Station runners reach the enrolment endpoint (8444) and the executor reaches the runners (8443). |
| `images.lock.yaml`, `images.lock.json` | P12 (ADR-0003) | Every image by immutable tag with its upstream, digest, image ID and signer. Filled by `scripts/lock-images.sh` on a connected build host; `install.sh` refuses to start an image the lock does not pin. The JSON twin is what the installer reads. |
| `../observability/` | P11 | Prometheus, Alertmanager and Grafana files the base file mounts read-only (`observability/README.md`). |

`install.sh` picks the files: the base, `prod.override.yml` for the prod profile, and each
macvlan overlay whose `SLAS_*_IFACE` key is set in `.env`.
