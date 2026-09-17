# images

Container image definitions. Every base image is pinned by digest (INV-8). The build context
for every first-party image is the **repository root**, so a Dockerfile can `COPY` from
`packages/`, `services/` and `apps/`:

```bash
docker build -f images/<name>/Dockerfile -t <registry>/slas/<name>:<version> .
```

`./install.sh --build` does exactly that for every first-party image the profile starts and
pulls the third-party ones (ADR-0014); `scripts/build-bundle.sh` does the same with
`--network none` from the vendored caches. `.dockerignore` at the root keeps `.git`, `.venv`,
`node_modules`, `models/` and the like out of the context.

| Directory | What it is |
|---|---|
| `slas-health/slas-health` | The probe every compose healthcheck runs: `slas-health URL [--insecure-local]`, POSIX `sh`, exit 0 on HTTP 2xx and 1 otherwise, using whichever of `curl`, `wget` (busybox is fine) or `python3` the image has. `--insecure-local` skips TLS verification for a loopback host only (the edge's own certificate). Copied into every image at `/usr/local/bin/slas-health`. |
| `edge/` | Caddy 2 on `:443` in front of everything (`Dockerfile`, entrypoint `slas-edge`). `SLAS_TLS_MODE=self-signed` uses Caddy's own CA (`tls internal`; root at `${SLAS_DATA_ROOT}/tls/caddy/pki/authorities/local/root.crt`), `provided` uses `/data/tls/server.crt` and `server.key`; `SLAS_TLS_NAMES` lists the site names. `/healthz` answered locally · `/api/*` → `api:8000` · `/grafana/*` → `grafana:3000` (prefix kept: Grafana serves from the sub-path) · everything else → `webui:8000`. Everything Caddy writes lives under `/data/tls`, bind-mounted from the data root. Port 443 for any uid: compose lowers `net.ipv4.ip_unprivileged_port_start` in the container's namespace. |
| `webui/` | Multi-stage: a Node stage runs `corepack enable && pnpm install --frozen-lockfile && pnpm --filter @slas/webui build`; the final stage is Caddy serving `/srv` on `:8000` with `/health` and the single-page fallback (`Caddyfile`). No TLS: the edge terminates it. |
| `api/` | Python service image (rendered): `uv sync --frozen --no-dev --no-editable --package slas-api` into `/opt/slas/.venv`; `CMD ["slas-api", "serve"]` (migrate, then uvicorn on 8000; `docs/api-contract.md`). Needs `apps/api` to be a uv workspace member named `slas-api`. |
| `agent-core-orchestrator/`, `llm-gateway/`, `model-manager/`, `sandbox-manager/`, `git-broker/`, `validation-executor/`, `factory-executor/`, `local-search-api/` | Python service images rendered from `slas_deploy.dockerfiles` (one template; `uv run python -m slas_deploy.render` regenerates them, a test keeps them in step). Each installs its workspace member from `uv.lock`, runs as uid 10001 unless compose sets `user:`, and serves `/health` and `/metrics` on 8000. The CMD is the service's console script (`docs/api-contract-round-2.md` §1, ADR-0015): `slas-orchestrator serve`, `slas-gateway serve`, `slas-model-manager serve`, `slas-sandbox-manager serve`, `slas-git-broker serve`, `slas-validation-executor serve`, `slas-factory-executor serve`; `local-search-api` keeps the placeholder `python -m slas_observability.serve local-search-api` until its round. `git-broker` adds `git` and `openssh-client`; the executors add `openssh-client` (`ipmitool` for validation, `openssl` for the factory station CA). |
| — (`vllm/vllm-openai:v0.29.0-x86_64-cu129`) | Not built here and not a compose service: the third-party vLLM image in `compose/images.lock.*` (`started_by: model-manager`, pinned by digest `sha256:3e10e818…`), started by the model manager. `./install.sh --build` pulls it by that digest and retags it `${SLAS_REGISTRY}/vllm/vllm-openai:v0.29.0-x86_64-cu129`; compose hands that reference to `model-manager` as `SLAS_VLLM_IMAGE`, and the model manager starts one container from it per role and per voter on the `slas_slas-inference` network (CLAUDE.md §7, contract §3). |
| `screen-worker/` | Xvfb + x11vnc + noVNC + xdotool + ImageMagick (ADR-0002) with its entrypoint from `services/screen-worker/entrypoint.sh`, which also starts the stdlib health runner beside the display. Moved here from `services/screen-worker/` so every first-party image builds the same way. |
| `sandbox-<language>/` | One Coding Agent sandbox image per language (Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config). Rendered from `slas_sandbox_manager.images` for the newest bundled toolchain version; a unit test keeps the files in step. `git` is installed for local commits; there is no credential helper, no remote and no route out (INV-14). Not in `compose/images.lock.*` and never a compose service: `./install.sh --build` asks `python -m slas_sandbox_manager.images list --registry <label>` for `name<TAB>tag<TAB>dockerfile` lines, builds each from the repository root, records the IDs in `${SLAS_DATA_ROOT}/sandbox-images.lock.json` and writes the toolchain manifest with `… images manifest --out ${SLAS_DATA_ROOT}/Toolchains/manifest.json` (contract §4, §9); the sandbox manager starts them by tag over the runtime socket. The offline bundle path builds them from the toolchain bundle beside them. |
| `sandbox-common/slas-check.sh` | The `slas-check <lint\|type\|build\|test\|validate>` wrapper every plan step calls (argv only). |
| `postgres-pgbackrest/` | PostgreSQL 16 with pgBackRest for the prod profile (ADR-0012): the `postgres` service's `archive_command` and the `backup-runner` share it. Its base is `${SLAS_REGISTRY}/library/postgres:16.6`, pinned through the image lock. |

## Base images

Every `FROM` uses the digest; the tag beside it is what the digest was resolved from
(`slas_deploy.dockerfiles.BASES`; a test checks that every Dockerfile uses one of these).

| Tag | Digest | Used by | How the digest was obtained |
|---|---|---|---|
| `python:3.12.14-slim-bookworm` | `sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254` | the Python services | `HEAD registry-1.docker.io/v2/library/python/manifests/3.12.14-slim-bookworm` (Docker-Content-Digest), cross-checked with the Docker Hub tag listing, 2026-09-17 |
| `ghcr.io/astral-sh/uv:0.8.17` | `sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1` | the static `uv` binary in the build stage | `HEAD ghcr.io/v2/astral-sh/uv/manifests/0.8.17`; the same query reproduced CI's pinned `0.8.17-python3.12-bookworm-slim` digest exactly |
| `debian:bookworm-20250908-slim` | `sha256:df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3` | screen-worker, the sandboxes | pinned earlier in this repository |
| `caddy:2.11.4` | `sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017` | edge, webui | `HEAD registry-1.docker.io/v2/library/caddy/manifests/2.11.4`; equals the `caddy:2` tag on 2026-09-17 |
| `node:22.22.2-bookworm-slim` | `sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e` | the webui build stage | the digest `.github/workflows/ci.yml` pins for the same tag, reproduced by `HEAD registry-1.docker.io/v2/library/node/manifests/22.22.2-bookworm-slim` on 2026-09-17 |

Building the sandbox images needs the bundle's `toolchains/<language>/<version>/` directory
next to the Dockerfile as build context, and an apt snapshot mirror for the pinned system
packages (the TODO in each Dockerfile). The apt and apk installs in the other images carry
the same TODO: on the connected `--build` path they reach the distribution mirrors; the
offline bundle path builds from vendored caches.
