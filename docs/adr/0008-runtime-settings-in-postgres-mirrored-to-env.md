# ADR-0008: Runtime settings live in Postgres and are mirrored into `.env`

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §3 says Admin → Settings "writes `.env`". INV-9 says a setting change never
requires a config edit or a restart. §9 lists "settings needing a restart" as a rejected
anti-pattern. A container reads its environment once at start, so a value that lives only
in `.env` cannot change a running service. The two sentences reconcile only if `.env` is a
mirror, not the thing services read at runtime.

## Decision
- **Two scopes of setting**, declared in one registry (`slas_schemas.settings`, stdlib):
  - *Runtime* settings — Phase 1: `installation_name` (≤ 60 characters), `chinese_variant`
    (`zh-Hant` | `zh-Hans`), `session_lifetime_hours` (1–168, applies to new sign-ins).
    They live in a Postgres `settings` table read on use, so a change applies to the very
    next request.
  - *Install-time* facts — data root, HTTPS port, TLS mode and names, profile, version.
    Read-only in the UI under "Set at install", with the sentence "To change these, edit
    `.env` on the host and run `./install.sh` again; only the affected service is restarted."
- **The mirror.** After every change and at api start, the api rewrites *only* the runtime
  keys in `${SLAS_DATA_ROOT}/.env` under a `# managed by Admin → Settings` marker, through
  `slas_schemas.envfile`: comments, order and unknown keys are preserved byte for byte, the
  write is atomic and keeps the file mode. A mirror failure never blocks the save: the API
  answers 200 with a notice ("Saved, but the copy in `.env` couldn't be written…").
- **Why this is restart-free.** ADR-0003 removed `env_file` from every service and lets
  each service list only the install-time keys it needs. A unit test asserts that no service
  references a runtime-scope key, so a Settings write leaves every `docker compose config
  --hash` unchanged and a later `compose up` or `./install.sh` rerun recreates nothing. An
  integration test proves it with `docker inspect … StartedAt` before and after.
- **Two sources of truth, one winner.** The database wins after first boot; `.env` seeds
  the table only when it is empty. A test asserts convergence after simulated drift.

## Alternatives considered
- Keep `env_file` and write runtime settings to `.env.d/settings.env`: also restart-free,
  but leaves a wholesale-loaded `.env` next to a managed file, two files for operators to
  understand; recorded as the fallback if the no-`env_file` rule proves unworkable.
- Settings only in the database, nothing written to `.env`: simplest, but contradicts §3
  and loses the property that a reinstall from `.env` reproduces the installation.
- Reload containers on change: a restart, the anti-pattern §9 rejects.

## Consequences
Easier: "change a setting without a restart" is literally true at the container level and
provable in CI; `.env` still documents the installation. Harder: writing `/data/.env` from
inside the api requires the api to run as the data root's owner (`${SLAS_UID}`), and the
notice path must be as well tested as the happy path.

## Invariants touched
INV-9 (upheld at the container level, not just the process level), INV-1 (nothing new
reaches out). CLAUDE.md §3 keeps "writes `.env`" with a footnote that `.env` is a mirror;
§9 gains a note distinguishing runtime from install-time settings.
