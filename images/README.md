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
| `agent-core-orchestrator/`, `llm-gateway/`, `model-manager/`, `sandbox-manager/`, `git-broker/`, `validation-executor/`, `factory-executor/`, `local-search-api/` | Python service images rendered from `slas_deploy.dockerfiles` (one template; `uv run python -m slas_deploy.render` regenerates them, a test keeps them in step). Each installs its workspace member from `uv.lock`, runs as uid 10001 unless compose sets `user:`, and answers `/health` and `/metrics` on 8000 with `python -m slas_observability.serve <name>` until the service's own entrypoint lands. `git-broker` adds `git` and `openssh-client`; the executors add `openssh-client` (and `ipmitool` for validation). |
| `screen-worker/` | Xvfb + x11vnc + noVNC + xdotool + ImageMagick (ADR-0002) with its entrypoint from `services/screen-worker/entrypoint.sh`, which also starts the stdlib health runner beside the display. Moved here from `services/screen-worker/` so every first-party image builds the same way. |
| `sandbox-<language>/` | One Coding Agent sandbox image per language and toolchain version (Python 3.11.10 and 3.12.6, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config), tagged `<registry>/slas/sandbox-<language>:<version>`. Rendered from `slas_sandbox_manager.images` (`uv run python -m slas_sandbox_manager.images render` regenerates them; a unit test keeps the files in step); an older version of a language lives beside the newest as `Dockerfile.<version>`. The committed files are the **connected** variant `./install.sh --build` runs (ADR-0014): the toolchain comes from a pinned upstream image by digest (`python`, `gcc`, `rust`, `golang`, `node`, `bash`) or a pinned package (`typescript@5.9.3`, `yamllint==1.35.1`, the Python companions `ruff==0.16.7 mypy==2.3.1 pytest==9.1.1`, `jsonschema==4.23.0`), and every build checks `<tool> --version` against the toolchain manifest, so `python -m slas_sandbox_manager.images manifest --out …` and the images cannot drift apart. `--source bundle` renders the offline variant (Debian base, toolchain copied from the bundle). Every image installs `git` for local commits and `slas-check`, runs as uid 10001 in `/workspace`, idles on `sleep infinity`, and adds no credential helper, no remote and no network tooling (INV-14). Not in the image lock; `python -m slas_sandbox_manager.images list` prints `name<TAB>tag<TAB>dockerfile` for the build. |
| `sandbox-common/slas-check.sh` | The `slas-check <lint\|type\|build\|test\|validate>` wrapper every plan step calls (argv only). |
| `postgres-pgbackrest/` | PostgreSQL 16 with pgBackRest for the prod profile (ADR-0012): the `postgres` service's `archive_command` and the `backup-runner` share it. Its base is `${SLAS_REGISTRY}/library/postgres:16.6`, pinned through the image lock. |

## Base images

Every `FROM` uses the digest; the tag beside it is what the digest was resolved from
(`slas_deploy.dockerfiles.BASES`; a test checks that every Dockerfile uses one of these).

| Tag | Digest | Used by | How the digest was obtained |
|---|---|---|---|
| `python:3.12.14-slim-bookworm` | `sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254` | the Python services | `HEAD registry-1.docker.io/v2/library/python/manifests/3.12.14-slim-bookworm` (Docker-Content-Digest), cross-checked with the Docker Hub tag listing, 2026-09-17 |
| `ghcr.io/astral-sh/uv:0.8.17` | `sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1` | the static `uv` binary in the build stage | `HEAD ghcr.io/v2/astral-sh/uv/manifests/0.8.17`; the same query reproduced CI's pinned `0.8.17-python3.12-bookworm-slim` digest exactly |
| `debian:bookworm-20250908-slim` | `sha256:df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3` | screen-worker, the sandboxes' offline-bundle variant | pinned earlier in this repository |
| `python:3.12.6-slim-bookworm` | `sha256:ad48727987b259854d52241fac3bc633574364867b8e20aec305e6e7f4028b26` | sandbox-python (3.12.6), sandbox-config | `HEAD registry-1.docker.io/v2/library/python/manifests/3.12.6-slim-bookworm` after the anonymous pull-token flow (`auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull`), Accept set to the manifest-list and OCI-index types so the Docker-Content-Digest is the multi-arch index, 2026-09-17 |
| `python:3.11.10-slim-bookworm` | `sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e` | sandbox-python (3.11.10) | the same query on `library/python/manifests/3.11.10-slim-bookworm`, 2026-09-17 |
| `gcc:13.2.0` | `sha256:15c73bc59ae88b3fd563ef2ec4a8743a8848a9f74362b6d116c4543c4844b6e0` | sandbox-c, sandbox-cpp | the same query on `library/gcc/manifests/13.2.0`, 2026-09-17 |
| `rust:1.80.1-slim-bookworm` | `sha256:907ff4b3ee7df57149ffee04f606e0a08b9b2ed3507f00a19cf3c9c0f74b7681` | sandbox-rust | the same query on `library/rust/manifests/1.80.1-slim-bookworm`, 2026-09-17 |
| `bash:5.2.21` | `sha256:a422913be6a4b0ea5403d2af72eb73779b6d9ed84d0dcf85d6b4309e891a379e` | sandbox-shell (Alpine-based) | the same query on `library/bash/manifests/5.2.21`, 2026-09-17 |
| `golang:1.23.1-bookworm` | `sha256:dba79eb312528369dea87532a65dbe9d4efb26439a0feacc9e7ac9b0f1c7f607` | sandbox-go | the same query on `library/golang/manifests/1.23.1-bookworm`, 2026-09-17 |
| `caddy:2.11.4` | `sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017` | edge, webui | `HEAD registry-1.docker.io/v2/library/caddy/manifests/2.11.4`; equals the `caddy:2` tag on 2026-09-17 |
| `node:22.22.2-bookworm-slim` | `sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e` | the webui build stage, sandbox-typescript | the digest `.github/workflows/ci.yml` pins for the same tag, reproduced by `HEAD registry-1.docker.io/v2/library/node/manifests/22.22.2-bookworm-slim` on 2026-09-17 (and again by the sandbox query above) |

The sandbox images' bases and the way each digest was obtained are also recorded in code
(`slas_sandbox_manager.images.BASES`, `Base.how`); a test checks that this table lists them.

The connected sandbox build (`./install.sh --build`) reaches, beyond the pinned base image:
the Debian or Alpine mirror for `git`, `make` and the few companions the Dockerfile names
(the `TODO(SLAS-IMAGES)` on pinning them from a snapshot mirror stands); PyPI for the pinned
`pip install ==` lines; the npm registry for `typescript@5.9.3`; and `static.rust-lang.org`
for `rustup component add clippy rustfmt` at the image's own toolchain version. The offline
bundle variant (`python -m slas_sandbox_manager.images render --source bundle`) needs the
bundle's `toolchains/<language>/<version>/` directory in the build context and downloads
nothing. The apt and apk installs in the other images carry the same TODO: on the connected
`--build` path they reach the distribution mirrors; the offline bundle path builds from
vendored caches.
