# ADR-0003: Quickstart stack — image pinning, secrets and settings without container churn

Status: proposed
Date: 2026-09-14

## Context
Phase 1 brings up the first running stack: postgres, redis, minio, api, webui and edge
(Caddy with self-signed TLS). Three things in CLAUDE.md §12's blueprint collide with the
rest of the specification once a real bundle install and a real Admin → Settings page
exist.

1. **Digest references and `docker load`.** §12 writes every image as
   `registry.internal/…@sha256:…`. A quickstart bundle is a tarball loaded with
   `docker load`, which does not record the registry digest on the loaded image, so a
   compose file that references images by digest tries to pull them — impossible under
   INV-1 and wrong for an air-gapped install.
2. **`env_file: [.env]` on every service.** §3 says Admin → Settings "writes `.env`", and
   INV-9 says a setting change never needs a restart. If every container loads `.env`
   wholesale, any write to it changes every container's configuration hash and the next
   `docker compose up` recreates them all.
3. **Where generated secrets live.** §3 says quickstart uses "generated `.env` + Docker
   secrets"; §5.7 says the credential store key is "derived from `SLAS_SECRET_KEY`". A key
   in `.env` is readable by every process that reads the file and appears in
   `docker compose config` output.

## Decision
- **Pin third-party images by immutable version tag plus a lock file.**
  `compose/images.lock.yaml` records, for each image, the tag compose references
  (`registry.internal/library/postgres:16.<patch>`), the upstream reference by manifest
  digest, and the image ID (config digest). The installer pulls by digest, asserts the image
  ID, and retags; `install.sh` verifies every loaded image's ID against the lock that ships
  in git. INV-8 is contained by a digest and an ID in git rather than by a digest in the
  compose file. First-party images are `registry.internal/slas/<name>:${SLAS_VERSION}` and
  are built with `docker build --network none` from vendored caches.
- **No `env_file` anywhere.** `x-common` carries no `env_file`. Each service lists the
  install-time keys it needs under `environment:` (Compose interpolates them from `.env`
  at `up` time); a unit test asserts no service references a runtime-scope key and no
  `environment:` value carries a password. A Settings write therefore leaves every
  `docker compose config --hash` unchanged (proven in ADR-0008).
- **Secrets are files, not `.env` keys.** The installer generates six files under
  `${SLAS_DATA_ROOT}/secrets/` (directory 0700): `postgres_password`, `redis_password`,
  `redis.conf` (derived, carries `requirepass`), `minio_root_password`, `secret_key`,
  `admin-initial-password`. Compose bind-mounts them as file secrets. Files read by
  third-party images are 0644 (Compose ignores secret uid/gid/mode and the mount targets
  the file, so only the file's mode matters inside the container); api-only files are 0600
  because the api runs as `${SLAS_UID}`. `SLAS_SECRET_KEY` is not an `.env` key; §5.7's
  "derived from `SLAS_SECRET_KEY`" becomes "derived from the `secret_key` Docker secret".
- **TLS by explicit names.** Caddy's internal CA issues one certificate for
  `SLAS_TLS_NAMES`, computed at install time (127.0.0.1, localhost, `hostname -f`, every
  non-loopback IPv4). No `on_demand` (IP literals send no SNI), `ocsp_stapling off`,
  `admin off`, no ACME. The root certificate is exported to `${SLAS_DATA_ROOT}/tls/ca.crt`
  and its fingerprint printed. `SLAS_TLS_MODE=provided` switches to operator files.
- **Hardening for every P1 service:** the `airgap` anchor plus `MINIO_UPDATE=off`,
  `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, explicit non-root `user`,
  a healthcheck, `depends_on: condition: service_healthy`. `slas-edge` is the only
  non-internal network and edge its only member. No host socket, X11 socket or
  `/dev/input` anywhere.

## Alternatives considered
- Keep digest references and require a registry on the host: adds a mandatory component
  with manual setup, against §0.3.
- Keep `env_file` and write runtime settings to a second file: works, but leaves a
  wholesale-loaded `.env` that the Settings page must never touch; recorded as the fallback
  in ADR-0008.
- Secrets as `.env` keys with 0600: visible in `docker compose config`, copied into every
  container that loads the file.

## Consequences
Easier: bundles install offline; a Settings write recreates nothing; secrets reach only
the containers that need them; the compose file is testable with a YAML parser in CI.
Harder: three CLAUDE.md edits (§12 `env_file`, §12 digest form, §5.7 key wording), a lock
file to maintain, and per-file secret modes that must be verified against the pinned
Compose version in the first hour of the implementing session.

## Invariants touched
INV-1 (no pull at install, no OCSP/ACME/update checks), INV-4 (no host sockets), INV-8
(pinned by digest and image ID in git; contained, not relaxed), INV-9 (groundwork: no
service loads `.env` wholesale), INV-5 posture (no password in any `environment:`).
CLAUDE.md §3, §5.7 and §12 need the edits above once this ADR is accepted.
