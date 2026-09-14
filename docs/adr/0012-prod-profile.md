# ADR-0012: The prod profile — Vault at dispatch, OIDC beside built-in auth, signed images, the Kata tier, PITR under object lock

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §3 describes the prod profile in one table row: OIDC via local Keycloak beside
built-in users, Vault injected at dispatch, gVisor required with a Kata/Firecracker tier,
Loki and Tempo, pgBackRest PITR with object lock and restore drills. §12 says `compose/`
holds the real files. When Phase 12 started, no base compose file existed: Phase 1 shipped
groundwork and ADR-0003/0004 (image pinning by tag plus a lock, file secrets, no `env_file`,
the GPU-less deploy test) but not `docker-compose.yml`, and `install.sh` stopped after the
preflight. This environment has no Docker, no registry, no Postgres and no clean host, so
"prove `./install.sh --profile prod` on a clean host" and "record the restore RTO" cannot
be executed here.

## Decision
- **The base compose file is rendered now, from code** (`slas_deploy.compose`), applying
  ADR-0003 as written: no `env_file`, file secrets under `${SLAS_DATA_ROOT}/secrets`,
  third-party images by immutable tag, hardening on every service, only `edge` publishing a
  port, one member each on `slas-lab`, `slas-factory` and `slas-git`, the runtime socket
  only in model-manager and sandbox-manager. `prod.override.yml`, the two macvlan overlays
  and every prod configuration file are rendered from the same package and kept in step by
  tests that check the zone model on the data.
- **The image lock ships unpinned and the installer refuses.** `compose/images.lock.*`
  records every image's immutable tag and upstream; `digest` and `image_id` are `null`
  until `scripts/lock-images.sh` runs on a connected build host. `install.sh` checks the
  lock before it changes anything and stops with what to do. INV-8 is held by refusal,
  never relaxed: no tag is mutable, no unpinned image starts.
- **Read-only steps first, then changes.** `install.sh` runs the preflight, verifies the
  bundle manifest with cosign (`verify-blob`, key-based, `--insecure-ignore-tlog
  --private-infrastructure`: there is no transparency log in an air gap) or every image in
  Harbor (`cosign verify --key` by digest before `pull`), and checks the lock and the
  manifest; only then does it write `.env`, generate secrets, load or pull images and start
  the services. `--dry-run` performs the read-only steps for real and prints the rest.
  A `--skip-preflight` flag exists for tests and CI dry-runs and announces itself.
- **Vault at dispatch.** `slas_schemas.vault.VaultKv` (KV v2, AppRole login from Docker
  secret files, TLS with the pinned CA). Executors resolve `vault:<mount>/<path>/<key>`
  through `VaultCredentialResolver` for one operation; git-broker keeps remote credentials
  in `slas/git/<ref>` through `VaultCredentialStore`. One AppRole and one least-privilege
  policy per service (`config/vault/policies`); the KV mount is `slas`.
- **OIDC beside built-in accounts.** Keycloak imports `config/keycloak/slas-realm.json`:
  one confidential client with PKCE, the platform roles as realm roles, a mapper that puts
  them in `slas_roles`. `slas_authz.oidc.OidcClient` runs the authorization-code flow and
  takes identity from the userinfo endpoint over the backend network — no token is parsed
  or verified locally, so no JWT library and no key material in the api. `SLAS_AUTH_MODES`
  lists the modes the sign-in page offers; `builtin` stays.
- **The Kata/Firecracker tier** is a third runtime, `kata-fc`, chosen when
  `SANDBOX_TIER=kata`; a host without it refuses to open a sandbox with a sentence rather
  than falling back silently. The doctor reports the tier and cosign in the prod profile.
- **PITR under object lock.** PostgreSQL runs from a first-party image with pgBackRest
  (`images/postgres-pgbackrest`); `archive_command` pushes WAL to a MinIO bucket created
  with object lock in compliance mode (nobody deletes a backup early); run artifacts go to
  a governance-locked bucket. `backup-runner` keeps the schedule and hosts `slas backup
  now|status|restore|drill`. `RestoreDrill` measures every phase and writes the record under
  `Backups/drills/`; the runbook carries the RTO table.
- **Loki and Tempo** join the observability network; Tempo receives the platform's own
  `traceparent` over OTLP; Grafana links logs and traces by `trace_id`.

## Consequences
- The base compose file is a Phase 1 deliverable landing in Phase 12; ADR-0003's three
  CLAUDE.md edits (§12 `env_file`, digest form, §5.7 key wording) are still owed.
- Nothing here has run against Docker, Vault, Keycloak, Harbor or Postgres: every piece is
  tested against fakes and the installer against stub tools. The first real
  `./install.sh --profile prod` on the reference host, and the first measured RTO, are
  release-checklist items recorded in `docs/runbooks/restore-drill.md`.
- New containers: vault, keycloak, loki, tempo, backup-runner, minio-init (plus alertmanager
  from ADR-0011). New first-party image: postgres-pgbackrest. New Python packages:
  `slas-deploy` (and `slas-observability` from ADR-0011).
- Secrets the installer generates grow by seven files in prod; all under one 0700 directory.

## Invariants touched
INV-1 (nothing pulled from outside the perimeter; Harbor and the bundle are inside it),
INV-4 (no agent container sees a host socket, X11 or `/dev/input`; enforced by test),
INV-5 (Vault at dispatch; tokens only in memory), INV-7 (a restore is confirmed with
`--yes`; the drill stops the writers first), INV-8 (held by refusal until the lock is
filled), INV-10 (one command still; the prod profile adds two files to copy: `cosign.pub`
and the bundle), INV-14 (git-broker's credentials move to Vault by reference).
