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
| `images.lock.yaml`, `images.lock.json` | P12 (ADR-0003) | Every image by immutable tag with its upstream, digest, image ID, signer and `started_by`. Filled by `scripts/lock-images.sh` on a connected build host; `install.sh` refuses to start an image the lock does not pin. The JSON twin is what the installer reads. `./install.sh --build` (ADR-0014) leaves this file alone and writes its own filled copy to `${SLAS_DATA_ROOT}/images.lock.json`, where a first-party image is pinned by the image ID of the local build. The vLLM image (`vllm/vllm-openai:v0.29.0-x86_64-cu129`, `started_by: model-manager`) is in the lock with its digest known up front — pulled by that digest, saved in the bundle, never a compose service; `model-manager` starts it per role and voter (ADR-0015). The sandbox images have their own lock, `${SLAS_DATA_ROOT}/sandbox-images.lock.json`, written by `--build` beside this one. |
| `../observability/` | P11 | Prometheus, Alertmanager and Grafana files the base file mounts read-only (`observability/README.md`). |
| `../images/` | — | The Dockerfiles behind every `${SLAS_REGISTRY}/slas/<name>:${SLAS_VERSION}` image (`images/README.md`); every healthcheck calls the `slas-health` probe they carry. |

`install.sh` picks the files: the base, `prod.override.yml` for the prod profile, and each
macvlan overlay whose `SLAS_*_IFACE` key is set in `.env`.

Host-side knobs in `.env`: `SLAS_RUNTIME_SOCKET` names the container-runtime socket mounted
into `model-manager` and `sandbox-manager` only (empty = rootless Podman's
`/run/podman/podman.sock`; `install.sh` writes `/var/run/docker.sock` when only Docker's
exists and says so; INV-4 unchanged), `SLAS_GPU_VRAM_GIB` is the per-GPU budget the model
manager places instances against (default 270, an HGX B300 GPU), and `SLAS_TLS_MODE`/`SLAS_TLS_NAMES` shape
the edge's certificate. The edge binds 443 as `${SLAS_UID}:${SLAS_GID}` because the compose
file lowers `net.ipv4.ip_unprivileged_port_start` inside its network namespace.

## Round 2 wiring (ADR-0015, `docs/api-contract-round-2.md`)

- Every first-party service serves HTTP on `0.0.0.0:8000` (`SLAS_BIND`); the healthcheck
  probes `/health`, Prometheus scrapes `/metrics`. Callers find each other through the
  `SLAS_*_URL` variables (`http://<service>:8000`) on `slas-backend`: the api reads
  `SLAS_MODEL_MANAGER_URL`, `SLAS_SANDBOX_MANAGER_URL`, `SLAS_ORCHESTRATOR_URL`,
  `SLAS_GIT_BROKER_URL`, `SLAS_FACTORY_EXECUTOR_URL`; the orchestrator `SLAS_GATEWAY_URL`,
  `SLAS_SANDBOX_MANAGER_URL`, `SLAS_GIT_BROKER_URL`, `SLAS_VALIDATION_EXECUTOR_URL`,
  `SLAS_FACTORY_EXECUTOR_URL`; the model manager `SLAS_GATEWAY_URL`.
- `model-manager` starts the `vllm-*` containers itself over the runtime socket:
  `SLAS_VLLM_IMAGE` (the lock's reference), `SLAS_INFERENCE_NETWORK=slas_slas-inference` (the
  `slas-inference` network carries that fixed `name:`; compose would otherwise derive it from
  the project name), `SLAS_HOST_MODELS_DIR=${SLAS_DATA_ROOT}/Models` (the host path it
  bind-mounts read-only into every instance), `SLAS_GPU_VRAM_GIB`, `SLAS_VLLM_SHM=16g`,
  `SLAS_RECONCILE_INTERVAL_S=30`, `SLAS_RUNTIME_SOCKET=/run/podman/podman.sock` (the
  in-container path of the mount).
- `sandbox-manager` gets `SLAS_HOST_DATA_ROOT=${SLAS_DATA_ROOT}` (host paths for bind mounts),
  `SLAS_TOOLCHAIN_MANIFEST=/data/Toolchains/manifest.json` (written by `install.sh --build`,
  mounted read-only), `SLAS_SANDBOX_REGISTRY=${SLAS_REGISTRY}` (the label the sandbox images
  were tagged with) and the runtime socket.
- `git-broker` mounts `${SLAS_DATA_ROOT}/.git-broker` (credential store and audit log),
  `Coding/` and `config/git-hosts.yaml` read-write (Admin → Git hosts renders it).
- `validation-executor` mounts `Validation/` (`SLAS_TARGETS_REGISTRY=/data/Validation/targets.json`)
  and `Tickets/`; `factory-executor` mounts `Factory/`, `Tickets/`, `Backups/stations/` and the
  shipped `templates/factory` read-only at `/etc/slas/templates` (`SLAS_FACTORY_TEMPLATES`),
  copied into `Factory/Templates` when that directory is empty; its enrolment endpoint
  (`ENROLMENT_LISTEN=0.0.0.0:8444`) listens on the factory network — the macvlan overlay gives
  it an address, no host port is published.
- Prometheus scrapes `vllm-<role>:8000` and `vllm-voter-<model id>:8000`; the prod override
  mounts `observability/prometheus/prometheus.prod.yml`, which lists the prod registry's third
  voter.
