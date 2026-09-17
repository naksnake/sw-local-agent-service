# apps/api

FastAPI application: sign-in and sessions, people, runtime settings, the model registry
view, health and metrics — round 1 of `docs/api-contract.md`. Later rounds add quota,
leases, approvals and the Ticket Service. No hardware, no LLM (CLAUDE.md §11). Runtime
stack and its dependency pins: ADR-0005; identity and the CLI transport: ADR-0007; settings
in Postgres mirrored to `.env`: ADR-0008; roles: ADR-0006.

Package `slas_api`, distribution `slas-api`, console script `slas-api`.

## Modules

| Module | Holds |
|---|---|
| `settings.py` | `Settings` (pydantic-settings): environment for facts, files under `SLAS_SECRETS_DIR` (default `/run/secrets`) for `postgres_password`, `redis_password`, `secret_key`, `admin-initial-password`. The database URL is a SQLAlchemy `URL` object assembled in memory; `SLAS_DATABASE_URL` overrides it (tests use SQLite). |
| `models.py` | The tables: `people`, `sessions`, `settings`, `audit_log`; portable SQL types only; `as_utc()` for SQLite's naive timestamps. |
| `db.py` | Synchronous engine on psycopg 3 (FastAPI runs the sync endpoints in its threadpool); `migrate()` runs Alembic to head under `pg_advisory_lock` on Postgres and without a lock on SQLite. |
| `migrations/` | Alembic environment and versions, driven only by `slas-api migrate` (no `alembic.ini`, no URL on disk). |
| `security.py` | argon2id (time cost 3, 64 MiB, parallelism 4; a `test` profile only through `SLAS_ARGON2_PROFILE=test`), the password policy sentences, 256-bit session tokens stored as SHA-256, readable one-time passwords. |
| `throttle.py` | 10 failures per 15 minutes per email and per address in Redis; fails closed (`ThrottleUnavailableError` → 503). `MemoryThrottle` for tests. |
| `authz.py` | `RolesLoader` re-reads `config/rbac-roles.yaml` on mtime change and keeps the last good set on a bad edit; `principal_for()` per request; `require()` → 403 in three parts. |
| `errors.py` | `ThreePartProblem` (the Pydantic twin of `ThreePartMessage`), `ApiError`, and the handlers that turn every error into that body. |
| `runtime_settings.py` | The three runtime keys, their validation sentences, the `.env` mirror under `# managed by Admin → Settings`, the seed-when-empty rule and the install-time facts. |
| `registry_view.py` | `GET /api/v1/models`: `Models/models.yaml` parsed and validated on every request, `present` from `Models/<path>/SHA256SUMS`. |
| `service.py` | People, sessions, audit rows and the bootstrap administrator — shared by the routes (`via=webui`) and the CLI (`SYSTEM`, `via=cli`). |
| `routes.py` | The contract's routes; `signed_in` and `ready` (must-change gate) dependencies; cookie `__Host-slas_session`. |
| `app.py` | `create_app(settings, engine=…, throttle=…, clock=…, log=…)`, the trace-id and `X-Requested-With` ASGI middlewares, `route_table()`. |
| `cli.py` | `slas-api migrate | bootstrap status | user add | user list | serve`. |

## Running it locally

```sh
uv sync
export SLAS_DATA_ROOT=/tmp/slas-data SLAS_SECRETS_DIR=/tmp/slas-secrets
export SLAS_DATABASE_URL="sqlite+pysqlite:////tmp/slas-data/api.db"   # or point at Postgres
export SLAS_ROLES_FILE=$PWD/config/rbac-roles.yaml
mkdir -p "$SLAS_DATA_ROOT" "$SLAS_SECRETS_DIR"
printf 'choose-a-one-time-password' > "$SLAS_SECRETS_DIR/admin-initial-password"
uv run slas-api migrate            # migrations, then admin@slas.local with must_change_password
uv run slas-api bootstrap status   # pending | done
uv run slas-api user list
uv run slas-api serve              # migrate again (idempotent), then uvicorn on 0.0.0.0:8000
```

`serve` needs a Redis for sign-in (`SLAS_REDIS_HOST`, `redis_password`); without one, sign-in
answers 503 and `/health` names redis. In compose, Postgres and Redis are the service names
`postgres` and `redis` and the secrets are the mounted files.

## Tests

`tests/unit/test_api_*.py` run the real Alembic migrations on SQLite, an in-memory throttle
and a fixed clock through FastAPI's `TestClient`; `tests/unit/api_harness.py` builds that.
`test_route_table_matches_the_contract` compares `slas_api.app.route_table()` with the
routes named in `docs/api-contract.md`.
