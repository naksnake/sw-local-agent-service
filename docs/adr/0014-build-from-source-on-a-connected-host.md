# ADR-0014: Building the stack from source on a connected quickstart host

Status: accepted
Date: 2026-09-17

## Context
INV-1 says the running platform has no external network dependency, and ADR-0003 and
ADR-0004 make the offline bundle the way images arrive: built on a release host, saved,
signed, carried over, loaded. The bundle pipeline exists as scripts, but no release has
been cut, `compose/images.lock.*` ships unpinned, and the first target host — an Ubuntu box
with rootful Docker 29 and Compose, run as root — is connected while it is being prepared.
Waiting for the first signed bundle would keep INV-10 ("`./install.sh` on a fresh GPU host
reaches a working login page") untestable on real hardware for another round.

CLAUDE.md §15 requires an ADR for an invariant relaxation and records the neighbouring
question as open decision (13): whether the platform host may fetch anything during its
preparation window. This ADR answers it for images and build dependencies on the
quickstart profile; weights already have `install.sh --fetch-models`.

## Decision
- **`./install.sh --build`** is a third image source beside the bundle and the registry,
  for the **quickstart profile only**. After the preflight and every read-only check it
  pulls each third-party image by its pinned tag from its upstream, builds each
  first-party image from `images/<name>/Dockerfile` with the repository root as context,
  tags everything under a local registry label (`$SLAS_REGISTRY`, default `local`),
  records the manifest digest and image ID of every pull and the image ID of every build,
  writes that filled lock to `${SLAS_DATA_ROOT}/images.lock.json`, runs `check-lock` on
  it, and starts the stack with `compose up --pull never`. The lock in git stays unpinned;
  the filled one is never committed.
- **A first-party image counts as pinned by its image ID alone.** A local build has no
  registry digest; the image ID is what `docker inspect` reports for the tag compose
  starts, and `check-lock` refuses a first-party image without one. Third-party images
  still need both the digest and the ID.
- **What the build may reach:** Docker Hub, ghcr.io, quay.io, nvcr.io for images; PyPI
  through `uv sync --frozen` (versions from `uv.lock`); the npm registry through `pnpm
  install --frozen-lockfile` (versions from `pnpm-lock.yaml`); Debian and Alpine package
  mirrors for the few system packages named in the Dockerfiles. Every base image is
  pinned by digest. Nothing is resolved: what is fetched is what the lock files name.
- **The running platform is unchanged.** No service gains egress; the compose networks,
  the zone model and every healthcheck are the same as for a bundle install. The
  exception is the preparation window on the host, exactly as for `--fetch-models`.
- **`--build` is refused with `--profile prod`.** Prod verifies every image's cosign
  signature and a local build carries none; prod installs from the signed bundle or from
  Harbor as ADR-0012 says.
- **Every first-party image gets a Dockerfile now**, built the same way: `edge` (Caddy,
  `tls internal`), `webui` (Node build stage, Caddy serving the bundle), the Python
  services (one rendered template: `uv sync --frozen --no-dev --no-editable --package
  <member>` into `/opt/slas/.venv`, uid 10001, `slas-health` at `/usr/local/bin`), and
  `screen-worker` (moved from `services/screen-worker/`). Services whose entrypoint has
  not landed run `python -m slas_observability.serve <name>`, so they answer `/health`
  and `/metrics` today and their rounds replace one `CMD` line each.

## Alternatives considered
- Cut a first signed bundle on a separate build host: right for prod and still the
  release path; it needs the release key, a second machine and the vendored caches, none
  of which exist yet, and it would not have found the Dockerfile mistakes a real
  `docker compose up` finds.
- Let compose `build:` the images from the compose file: simple, but the image lock would
  no longer describe what runs, and `--pull never` plus the lock is how INV-8 is checked.
- Pin the Debian and Alpine packages now: the right end state (a snapshot mirror in the
  bundle build) and out of scope here; the Dockerfiles carry the same `TODO(SLAS-IMAGES)`
  the sandbox images already carry.

## Consequences
Easier: the stack starts on the target host from this checkout with one command
(`./install.sh --build --fetch-models`), and every later round can prove its service inside
the real stack. Harder: a `--build` install is not reproducible bit for bit (package
mirrors move), so the filled lock is per host and the runbooks say so; the operator must
know that a connected quickstart host is a preparation-time exception and that the prod
path is unchanged.

## Invariants touched
INV-1 — relaxed only during `./install.sh --build` on a quickstart host, for image pulls
and lock-pinned dependency downloads; the running platform still has no egress. INV-8 —
held: bases by digest, dependencies by lock file, third-party images by tag with the digest
recorded, first-party images by image ID; `check-lock` still refuses anything unpinned.
INV-10 — becomes checkable on the first real host now, with the bundle path unchanged.
INV-4 — untouched: the runtime socket still reaches only model-manager and sandbox-manager
(`SLAS_RUNTIME_SOCKET` only lets the host path be named).
