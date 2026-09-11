# Phase 1 build contract — Quickstart core

Status: binding for the Phase 1 implementation, 2026-09-11. Companion to CLAUDE.md (v3.1, the
SSOT), ADR-0003 (dependency set), ADR-0004 (install contract), ADR-0005 (roles) and ADR-0006
(settings and identity). Where this document and CLAUDE.md disagree, CLAUDE.md wins and the
disagreement is a bug to report.

This is the interface every Phase 1 component is built against so that the api, the web
interface, the installer, the CLI and the tests agree without reading each other's code.

## 1. Repository conventions (apply to every file)

- Python 3.12, `uv`, `ruff` (E W F I B UP S N, line length 100), `mypy --strict` over
  `apps/api`, `packages`, `services`, `tests/unit`, `tests/deploy`, `tests/integration`.
  Flat-layout packages with `py.typed`; every module has a docstring naming the CLAUDE.md
  section it serves; every `def` is annotated. Other `slas_*` members import as third-party
  blocks (ruff isort). `subprocess.run` only with an argv list, an absolute executable and
  `# noqa: S603 - reason`; never `shell=True`; secrets travel by environment, stdin or file,
  never argv. `secrets`, never `random`, for tokens. No literal `0.0.0.0` in Python outside
  a `# noqa: S104` with reason; no `/tmp` paths in Python (`tempfile`).
- Pydantic v2 at every boundary (`frozen=True`, `extra="forbid"` for request models). Every
  error a component returns is a `slas_schemas.errors.ThreePartError`
  (`what_happened / likely_cause / what_to_do`, three sentences).
- Logs: `structlog` JSON with `trace_id`, `event`, `level`; never a password, token, cookie or
  one-time password. Allow-list the fields you log.
- Tests: `pytest`, against fakes (in-memory repositories, `FakeEngineRunner`, PATH shims, a
  scripted web client). Nothing needs real hardware, a display or a model. `tests/unit`
  needs no services; `tests/integration` needs the ephemeral Postgres described in §12 and
  skips when it is absent (fails in CI where it must be present); `tests/deploy` drives
  `install.sh` with shims and, only with `SLAS_DEPLOY_REAL=1`, a real Docker stack.
- TypeScript 5 strict (`apps/webui/tsconfig.json`), `tsc` only, vitest with jsdom, no `any`
  without a comment. UI copy: sentences, three-part errors rendered as they are, no enum or
  raw JSON as primary content, say what will happen before it happens, no dead ends (§9).
- Shell: POSIX `sh` for `install.sh` and `bin/slas`; `bash` only under `scripts/ci/`.
- No new dependency of any kind beyond ADR-0003. If you need one, stop and report it.
- Do not edit these shared files unless your component owns them (below): root
  `pyproject.toml`, `uv.lock`, `pnpm-lock.yaml`, `CLAUDE.md`, `.github/workflows/ci.yml`.

## 2. Ownership map

| Component | Owns (create and change freely) |
|---|---|
| authz (done) | `packages/slas-authz/**`, `config/rbac-roles.yaml`, `tests/unit/authz/**`, ADR-0005 |
| foundation (done) | `packages/slas-cli/slas_cli/{runner,envfile}.py` and their tests, ADR-0003, root `pyproject.toml` |
| api-core | `apps/api/slas_api/**` except `routers/{users,roles,settings}.py` and `users_service.py`, `settings_*.py`; `apps/api/README.md`; `packages/slas-schemas/slas_schemas/{envelope,auth,users}.py`; `tests/unit/api/**` (core files); `tests/integration/**` (core files); ADR-0006 |
| api-admin (after api-core) | `apps/api/slas_api/routers/{users,roles,settings}.py`, `apps/api/slas_api/{users_service,settings_catalogue,settings_store}.py`, `python -m slas_api user|settings` subcommands in `cli.py`, `packages/slas-schemas/slas_schemas/settings.py`, their tests under `tests/unit/api/` and `tests/integration/`, and edits to `app.py` to include the routers |
| infra | `compose/**`, `services/edge/**`, `apps/api/Dockerfile`, `apps/webui/Dockerfile`, `.dockerignore`, `install.sh`, `bin/slas`, `VERSION`, `packages/slas-cli/slas_cli/{install,images_lock,bundle}.py` and tests, `scripts/bundle/**`, `scripts/ci/{container-egress-drop,netns-run}.sh`, `tests/deploy/**`, `tests/unit/compose/**`, `tests/unit/test_install_*.py`, `.github/workflows/ci.yml`, `config/.env.example` and `tests/unit/test_env_example.py`, `tests/unit/test_layout.py` additions, ADR-0004, READMEs of compose/edge/images/deploy |
| cli | `packages/slas-cli/slas_cli/{cli,user,logs}.py`, `tests/unit/test_cli_user.py`, `tests/unit/test_cli_logs.py`, `tests/unit/test_doctor.py` (LATER_COMMANDS assertions), `packages/slas-cli/README.md` |
| webui | `apps/webui/**` except `Dockerfile`, `tests/e2e/**`, `docs/ui/README.md` |
| docs (last) | `CLAUDE.md`, `README.md`, `docs/DEVELOPMENT_PLAN.md`, `docs/adr/README.md` |

## 3. Installed layout and `.env`

```
${SLAS_DATA_ROOT}/                 default /AI/Agent; created by install.sh, owned by the installing user
├── .env                           0600, NON-SECRET settings only (keys below)
├── secrets/                       0700; one file per secret, 0644 so a non-root container process can read its mount
│   ├── postgres_password          Postgres superuser; read only by the postgres container
│   ├── postgres_api_password      role slas_api, owner of database slas; read by postgres (init) and api
│   ├── redis_password             read by redis (requirepass) and nothing else in Phase 1
│   ├── minio_root_user
│   └── minio_root_password
├── compose/docker-compose.yml     installed copy of the bundle's compose file (0644)
├── compose/images.lock.json       installed copy
├── config/rbac-roles.yaml         installed copy; the api mounts ${SLAS_DATA_ROOT}/config:/etc/slas:ro as a DIRECTORY
├── edge/                          Caddy data (its local CA); edge/root.crt is copied out 0644 for browsers and the CLI
├── bin/slas                       POSIX launcher; bin/slas_cli/ is a copy of packages/slas-cli/slas_cli
├── Coding/ Validation/ Factory/ Tickets/ Skills/ SOP/ Models/ Knowledge/ Backups/   (§4.4, empty, 0755)
```

Service state (Postgres, Redis, MinIO data) lives in named volumes `slas_postgres`,
`slas_redis`, `slas_minio` of compose project `slas`. Backups of them arrive with the phase
that owns backups.

`.env` keys (all non-secret; `config/.env.example` lists them with comments):

```
SLAS_PROFILE=quickstart
SLAS_DATA_ROOT=/AI/Agent
SLAS_EDGE_PORT=443
SLAS_EDGE_HOSTS=localhost, 127.0.0.1, <hostname>, <primary IPs>   # names Caddy issues certificates for
SLAS_ENGINE=docker|podman
SLAS_VERSION=<from the bundle's VERSION file>
SLAS_LOG_LEVEL=info
SLAS_SECRET_KEY=                                                    # empty until Phase 6 (credential store)
```

A re-run of `install.sh` keeps every existing `.env` value and every existing secret file;
flags given explicitly override `.env` for that run and are written back. `--data-root` may
not change an existing installation (refused with a three-part message).

## 4. Compose contract (`compose/docker-compose.yml`)

- `name: slas`. Networks: `slas-edge` (external, edge only), `slas-frontend` (`internal:
  true`; edge, webui, api), `slas-backend` (`internal: true`; api, postgres, redis, minio).
- Services and images (tags, never `latest`; `pull_policy: never`; ids verified against
  `compose/images.lock.json` at install):

| service | image | networks | ports | secrets it reads |
|---|---|---|---|---|
| postgres | `postgres:16.10` | backend | none | postgres_password (`POSTGRES_PASSWORD_FILE`), postgres_api_password (init script) |
| redis | `redis:7.4.6` | backend | none | redis_password (`--requirepass "$(cat /run/secrets/redis_password)"` via the image's entrypoint) |
| minio | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | backend | none | minio_root_user (`MINIO_ROOT_USER_FILE`), minio_root_password (`MINIO_ROOT_PASSWORD_FILE`) |
| api | `slas/api:${SLAS_VERSION}` | frontend, backend | none | postgres_api_password (`SLAS_DB_PASSWORD_FILE=/run/secrets/postgres_api_password`) |
| webui | `slas/webui:${SLAS_VERSION}` | frontend | none | none |
| edge | `slas/edge:${SLAS_VERSION}` | edge, frontend | `${SLAS_EDGE_PORT}:443` | none |

- `x-common`: `restart: unless-stopped`, json-file logging 50m×5. `x-airgap` env on api and
  webui (`DO_NOT_TRACK=1` and friends). Every service has a `healthcheck`; `depends_on` uses
  `condition: service_healthy` in the §12 start order (postgres → redis → minio → api → edge
  → webui). `security_opt: [no-new-privileges:true]` everywhere; `cap_drop: [ALL]` on api,
  webui and edge (edge adds `NET_BIND_SERVICE`).
- api environment: `SLAS_DB_HOST=postgres`, `SLAS_DB_PORT=5432`, `SLAS_DB_NAME=slas`,
  `SLAS_DB_USER=slas_api`, `SLAS_DB_PASSWORD_FILE=/run/secrets/postgres_api_password`,
  `SLAS_RBAC_ROLES_FILE=/etc/slas/rbac-roles.yaml`, `SLAS_LOG_LEVEL`, `SLAS_VERSION`,
  `SLAS_DATA_ROOT` (display only), `SLAS_EDGE_PORT` (display only). Volume:
  `${SLAS_DATA_ROOT}/config:/etc/slas:ro` and nothing else.
- postgres init: `compose/postgres-init/01-roles.sh` mounted at
  `/docker-entrypoint-initdb.d/01-roles.sh:ro`; it creates role `slas_api` with the password
  read from `/run/secrets/postgres_api_password` (never on argv: psql `\set` from a file, or
  `PGPASSWORD`-free heredoc with `format('%L', ...)`) and `CREATE DATABASE slas OWNER slas_api`.
- edge: `services/edge/Caddyfile` — global `{ admin off  skip_install_trust  local_certs }`,
  site `{$SLAS_EDGE_HOSTS}` with `tls internal`, `/api/*` → `reverse_proxy api:8000` adding
  `X-Trace-Id {http.request.uuid}` when absent, everything else → `reverse_proxy webui:8080`,
  headers `X-Content-Type-Options nosniff`, `Referrer-Policy no-referrer`, `X-Frame-Options
  DENY`, a `Content-Security-Policy` that allows only `'self'` (scripts, styles, images, fonts,
  connect), no HSTS (self-signed). Caddy data at `/data` ← `${SLAS_DATA_ROOT}/edge`.
  Healthcheck: `wget -q --spider --no-check-certificate https://localhost:443/api/health` or
  the Caddy-native equivalent.
- webui: `services/edge/webui.Caddyfile` — `admin off`, `:8080`, `root * /srv`, `file_server`,
  SPA `try_files {path} /index.html`, `/healthz` responds 200. Image built from
  `apps/webui/Dockerfile` (node build stage → caddy stage).
- `compose/images.lock.json`:

```json
{ "version": 1,
  "images": [
    { "ref": "postgres:16.10", "source": "docker.io/library/postgres@sha256:2c72031ac25606bf94fd2fece7c35efdf263a48b08446950034f0725653f4efc",
      "image_id": "sha256:23ab7b9541a25c07fd6f64c6021b5389908e62bb9ff24364c9d3c3aacb8b9963", "built": false },
    { "ref": "redis:7.4.6", "source": "docker.io/library/redis@sha256:6a11fed904cf317684ebb75bfe987d4f777c605d6f4e98d1bf3066db6c58f0c1",
      "image_id": "sha256:b4e53bb4637c329c42395eec0b86851f5314f6ed597ec217b5e4e19f7c102c11", "built": false },
    { "ref": "minio/minio:RELEASE.2025-09-07T16-13-09Z", "source": "docker.io/minio/minio@sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2",
      "image_id": "sha256:69b2ec208575b69597784255eec6fa6a2985ee9e1a47f4411a51f7f5fdd193a9", "built": false },
    { "ref": "slas/api:${SLAS_VERSION}", "built": true, "dockerfile": "apps/api/Dockerfile", "context": ".", "image_id": null },
    { "ref": "slas/webui:${SLAS_VERSION}", "built": true, "dockerfile": "apps/webui/Dockerfile", "context": ".", "image_id": null },
    { "ref": "slas/edge:${SLAS_VERSION}", "built": true, "dockerfile": "services/edge/Dockerfile", "context": "services/edge", "image_id": null }
  ] }
```

  Build bases (Dockerfiles pin by digest): `python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49`,
  `node:22.19.0-bookworm-slim@sha256:cff78eb5aa1cf27dc2b6aeea9d31366415a43e9a9ea0ddec00d780b2b66fad0f`,
  `caddy:2.10.2@sha256:d8c17a862962def15cde69863a3a463f25a2664942eafd7bdbf050e9c3116b83`,
  `ghcr.io/astral-sh/uv:0.8.17@sha256:db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3`.
  The bundle builder writes the built image ids into the staged copy of the lock and into
  `MANIFEST.json`; install.sh verifies every loaded image id against the staged lock.

## 5. Bundle and install contract

Bundle: `slas-bundle-<version>.tgz` unpacking to `slas-bundle-<version>/` with `install.sh`,
`VERSION`, `bin/slas`, `compose/`, `config/`, `packages/slas-cli/`, `images/<name>.tar`
(one `docker save` per lock entry, file name = ref with `/` and `:` replaced by `_`),
`MANIFEST.json` (`{version, git_sha, built_at, images:[{ref, image_id, file, sha256}],
files:[{path, sha256}]}`) and `SHA256SUMS`. Built by `scripts/bundle/build-bundle.sh
--version <v> --out <dir> [--engine docker]` (networked; needs Docker).

`install.sh` (POSIX sh, idempotent) keeps its P0 flags and adds `--engine podman|docker`,
`--preflight-only` (P0 behaviour and output, byte for byte; `--json` implies it),
`--admin-email` (default `admin@slas.local`). Steps after the preflight run in stdlib Python
(`python -m slas_cli install …`, module `slas_cli/install.py`, engine calls through
`slas_cli.runner`), each printing one plain-language line:

1. configure — `.env` and secrets created only if absent; layout directories; installed
   copies of compose/ and config/; `bin/slas` launcher.
2. images — `<engine> load -i images/*.tar`; `<engine> image inspect --format {{.Id}} <ref>`
   must equal the staged lock; mismatch → exit 4 with a three-part message.
3. models — Phase 1 ships none: prints "No models are bundled yet; the Models page will say
   so." and continues.
4. up — `<engine> compose --project-name slas -f … --env-file … up -d --remove-orphans`.
5. wait — poll `compose ps --format json` until every service reports healthy (default
   timeout 300 s, `SLAS_HEALTH_TIMEOUT_S`); timeout → exit 5 with the unhealthy services named.
6. admin — `compose exec -T api python -m slas_api bootstrap-admin --email <e> --json`;
   first run prints `Sign in at https://<host>:<port> as <email> with this one-time password:
   <otp>` and `It works once; you will choose your own password.`; re-run prints `The
   administrator account already exists; nothing was changed.` Any other outcome → exit 6.
7. done — prints the URL(s), the CA certificate path `${SLAS_DATA_ROOT}/edge/root.crt` and
   `Manage this installation with ${SLAS_DATA_ROOT}/bin/slas`.

Exit codes: 0 ok · 2 preflight blocked · 3 configuration failed · 4 image verification failed ·
5 services did not become healthy · 6 administrator bootstrap failed. On a GPU-less host the
login page is still reached; the doctor's warning is the only mention.

## 6. `python -m slas_api` (inside the api container)

| Command | Behaviour | Output |
|---|---|---|
| `serve` | run migrations if `--migrate` then uvicorn on `0.0.0.0:8000` | logs |
| `migrate` | `alembic upgrade head` (idempotent, advisory-locked) | one line |
| `bootstrap-admin --email E [--display-name N] [--json]` | create the first administrator with a one-time password if no user with that email exists | JSON `{"created": true, "email": E, "one_time_password": "…"}` or `{"created": false, "email": E}` |
| `user add E --role R [--display-name N] [--json]` | create a person; refuse an unknown role or an existing email with a three-part message on stderr and exit 2 | JSON `{"user": {...}, "one_time_password": "…"}` or the sentence `Added E as <Role label>. One-time password (works once): …` |
| `user list [--json]` | list people | table or JSON |
| `user deactivate E` / `user reset-password E [--json]` | as the API endpoints | sentence or JSON |
| `settings get KEY` / `settings set KEY VALUE` | as the API endpoints; `set` prints `Saved. It applies immediately; nothing restarted.` or the applies sentence | value or sentence |
| `openapi` | print the OpenAPI document | JSON |

Every CLI action is audited in the structlog as actor `host-console`. Image entrypoint:
`python -m slas_api migrate && exec python -m slas_api serve`.

## 7. REST API (all under `/api`, JSON, cookies)

Envelope for every non-2xx response: `{"error": {"what_happened": …, "likely_cause": …,
"what_to_do": …}, "trace_id": "…"}`. Every response carries `X-Trace-Id` (taken from the
request header when present, else generated). Unsafe methods (POST, PUT, PATCH, DELETE)
require the header `X-Slas-Client` with any value (the CSRF rule; browsers cannot add custom
headers cross-site) — missing → 403. Request bodies are Pydantic models with `extra="forbid"`
→ 422 with a sentence naming the field. Timestamps are RFC 3339 UTC.

`User` (public shape): `{id (uuid), email, display_name, role, role_label, is_active,
must_change_password, created_at, last_login_at | null}`. Emails are lower-cased and unique.

| Method and path | Capability | Body → Response |
|---|---|---|
| `GET /api/health` | none | `{"status": "ok", "version", "started_at", "sentence": "Everything is healthy."}` |
| `GET /api/health/ready` | none | 200 `{"status": "ok", "checks": {"database": "ok"}}` or 503 with the envelope |
| `POST /api/auth/login` | none | `{email, password}` → 200 `{"user": User, "capabilities": [...]}` + cookie; 401 generic ("The email or password is not right."); 429 when locked ("Too many attempts; try again in N minutes.") |
| `POST /api/auth/logout` | signed in | 204; cookie cleared |
| `GET /api/auth/me` | signed in | `{"user": User, "capabilities": [...]}` |
| `POST /api/auth/change-password` | signed in | `{current_password, new_password}` → 204; rotates the session, revokes every other session of the user, clears `must_change_password` |
| `GET /api/roles` | signed in | `[{"name", "label", "description", "capabilities": [...]}]` from the live roles file |
| `GET /api/users` | `users:manage` | `[User]` ordered by email |
| `POST /api/users` | `users:manage` | `{email, display_name, role}` → 201 `{"user": User, "one_time_password": "…"}`; 409 if the email exists; 422 unknown role |
| `PATCH /api/users/{id}` | `users:manage` | `{display_name?, role?}` → 200 `{"user": User}`; refuses removing the last administrator's role |
| `POST /api/users/{id}/deactivate` | `users:manage` | 200 `{"user": User}`; refuses self and the last active administrator; revokes the user's sessions |
| `POST /api/users/{id}/reactivate` | `users:manage` | 200 `{"user": User}` |
| `POST /api/users/{id}/reset-password` | `users:manage` | 200 `{"user": User, "one_time_password": "…"}`; sets `must_change_password`; revokes sessions |
| `GET /api/settings` | `settings:read` | `[Setting]` |
| `PUT /api/settings/{key}` | `settings:manage` | `{value}` → 200 `{"setting": Setting, "sentence": "Saved. It applies immediately; nothing restarted."}` or the setting's applies sentence; 422 out of range; 403 read-only |

`Setting`: `{key, label, description, kind: "integer" | "choice" | "text", value, default,
options?: [{value, label}], min?, max?, editable, applies: "immediately" | "install-time" |
"phase-3" | "phase-5" | "phase-6", applies_sentence, updated_at | null, updated_by | null}`.

While `must_change_password` is true every route except `/auth/me`, `/auth/change-password`,
`/auth/logout` and health answers 403 with `what_to_do: "Choose a new password first."`.

Sessions: opaque 32-byte tokens (`secrets.token_urlsafe`), stored as SHA-256 in table
`sessions`, cookie `__Host-slas_session` (`HttpOnly; Secure; SameSite=Strict; Path=/`), idle
expiry `session_idle_minutes` (setting, default 480) refreshed on use, absolute lifetime 7
days. Passwords: argon2id with argon2-cffi defaults (time 3, memory 64 MiB, parallelism 4),
hashed in a worker thread. Login lock: after `login_lock_after_failures` (default 5) failed
attempts for an email, refuse for `login_lock_minutes` (default 5); the response for an
unknown email, a wrong password and a deactivated account is the same 401 sentence.
One-time passwords: 20 characters from `ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789`
in four groups of five joined by `-`; valid until the person chooses a password.

Trusted proxy: the api listens only on internal networks; it takes the client address from
the rightmost `X-Forwarded-For` hop and logs it; the edge sets the header.

## 8. Database (Postgres 16, SQLAlchemy 2 async + asyncpg, Alembic)

Tables (revision `0001_users_settings_sessions`):

- `users(id uuid pk, email text unique not null, display_name text not null, role text not
  null, password_hash text not null, must_change_password bool not null default true,
  is_active bool not null default true, failed_logins int not null default 0, locked_until
  timestamptz null, created_at timestamptz not null, updated_at timestamptz not null,
  last_login_at timestamptz null, deactivated_at timestamptz null)`
- `sessions(id uuid pk, token_hash text unique not null, user_id uuid fk users on delete
  cascade, created_at, expires_at, last_seen_at timestamptz not null, revoked_at timestamptz
  null)`
- `settings(key text pk, value jsonb not null, updated_at timestamptz not null, updated_by
  text null)`

Repositories are Protocols (`UserRepository`, `SessionRepository`, `SettingsRepository`)
with in-memory fakes used by unit tests and SQLAlchemy implementations used by integration
tests and production. Settings are read from the table on every request that needs them
(no cache longer than 2 s), so a change is live without a restart (INV-9).

## 9. Settings catalogue (code, `settings_catalogue.py`)

| key | kind | default | range/options | applies |
|---|---|---|---|---|
| `session_idle_minutes` | integer | 480 | 5..1440 | immediately ("Sign out after this many idle minutes.") |
| `login_lock_after_failures` | integer | 5 | 3..20 | immediately |
| `login_lock_minutes` | integer | 5 | 1..60 | immediately |
| `consensus_min_voters` | choice | 3 | 3 ("3, recommended"), 5 | phase-3 ("Stored now; used from Phase 3, when models arrive.") |
| `chinese_script` | choice | zh-Hant | zh-Hant ("Traditional"), zh-Hans ("Simplified") | phase-5 ("Every report is exported in English and Chinese; this chooses the Chinese script. Used from Phase 5.") |
| `sandbox_isolation` | choice | gvisor | gvisor ("gVisor, default"), kata ("Kata with Firecracker, strongest and slower") | phase-6 |
| `data_root` | text | from env | read-only | install-time ("Set when the platform was installed.") |
| `edge_port` | integer | from env | read-only | install-time |

## 10. Web interface

Routes (react-router): `/login`, `/change-password`, `/` (Home), `/admin` (People and
Settings panels plus "Servers" and "Git hosts" placeholders naming their phase), and one
placeholder route per remaining §9 page (`/coding`, `/validation`, `/factory`, `/runs`,
`/tickets`, `/models`, `/skills`, `/knowledge`) saying which phase brings it. Unauthenticated
→ `/login`; `must_change_password` → `/change-password`.

Login page (the e2e contract): heading "SW Local Agent Service", inputs labelled "Email" and
"Password", button "Sign in", inline three-part notice on 401/429/503. API access goes
through one typed client (`src/api/client.ts`) that adds `X-Slas-Client: webui`, sends
cookies, parses the envelope and validates responses with zod; a scripted fake client
(`src/api/fakeClient.ts`) drives component tests. Server state through TanStack Query.

Admin → People: table Person / Role / Can approve (the role description), buttons "Add
person" (dialog: email, display name, role; success shows the one-time password once with
the sentence "Share this in person. It works once; they will choose their own password."),
"Reset one-time password", "Deactivate" (confirm dialog saying what will happen). Admin →
Settings: one field per editable setting with its applies sentence, an explicit "Save" per
field, success toast with the api's sentence, read-only fields shown as text. The shell has
the ten §9 pages in a rail, a top bar with "Signed in as <name>, <role label>" and "Sign
out", and a footer line "No internet connection is used."

Playwright (`tests/e2e`): `smoke.spec.ts` (un-skipped: login page visible), `login.spec.ts`,
`admin-people.spec.ts`, `admin-settings.spec.ts`; env `SLAS_BASE_URL`, `SLAS_E2E_ADMIN_EMAIL`,
`SLAS_E2E_ADMIN_PASSWORD` (a password the deploy test set after the one-time login),
`SLAS_E2E_BROWSER_CHANNEL=chrome` on GitHub runners (or `PLAYWRIGHT_CHROMIUM_EXECUTABLE`).

## 11. `slas` CLI (stdlib only)

`slas user add E --role R [--display-name N]`, `slas user list|deactivate|reset-password`,
`slas logs [service] [--tail N] [-f]`: resolve the data root (`--data-root` > `$SLAS_DATA_ROOT`
> `/AI/Agent`), read `SLAS_ENGINE` from `.env`, build argv with `slas_cli.runner.Compose`
(`compose_file=${DATA_ROOT}/compose/docker-compose.yml`, `env_file=${DATA_ROOT}/.env`), run
`exec -T api python -m slas_api …` and relay stdout/stderr and the exit code. `slas --version`
reads `VERSION`. `LATER_COMMANDS` loses `user` and `logs`; `backup` → P11 and `upgrade` → P12.

## 12. Tests and CI

- Local ephemeral Postgres for integration tests: env `SLAS_TEST_DB_HOST`, `SLAS_TEST_DB_PORT`,
  `SLAS_TEST_DB_USER`, `SLAS_TEST_DB_PASSWORD`, `SLAS_TEST_DB_NAME`; when unset the tests
  skip with a sentence; in CI (`SLAS_CI=1`) they fail instead. In this build environment a
  Postgres 16 listens on `127.0.0.1:55432`, user/password/database `slas_test`.
- CI jobs added: `integration` (ubuntu-24.04, `services: postgres` from the lock digest,
  `uv run pytest tests/integration`), `deploy` (needs python, node; builds the bundle with
  Docker, removes the images, applies `iptables -I DOCKER-USER -o <uplink> -j DROP` and an
  `OUTPUT` rule for uid 0 so `dockerd` cannot pull, proves both with canaries, extracts the
  bundle, runs `install.sh --engine docker --data-root $RUNNER_TEMP/slas-data --edge-port 8443`
  inside `unshare -n` through `scripts/ci/netns-run.sh`, then `uv run pytest
  tests/deploy/test_real_stack.py` with `SLAS_DEPLOY_REAL=1` which asserts health, logs in
  with the one-time password, sets a password, adds a person through `bin/slas`, changes
  `session_idle_minutes`, checks every container's `StartedAt` is unchanged, re-runs
  `install.sh` for idempotence, greps every sink for secrets, and runs Playwright once).
- The existing `egress-drop` job calls `install.sh --preflight-only` and keeps accepting
  exit 0 or 2.
