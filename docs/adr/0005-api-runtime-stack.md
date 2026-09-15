# ADR-0005: The `apps/api` runtime stack and its dependencies

Status: proposed
Date: 2026-09-14

## Context
Phase 1 creates `apps/api`: authentication, sessions, people, settings, health. CLAUDE.md
§4.3 names FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, Postgres 16, Redis 7 and MinIO;
§11 names structlog and async I/O with explicit timeouts. Each is a third-party dependency
the project rule says to ask about before adding, and the exact set and versions belong in
one place.

## Decision
`apps/api/pyproject.toml` (package `slas-api`, joins the uv workspace) depends on, each
pinned with `==` at the version current when the implementing session starts:

| Dependency | Why |
|---|---|
| fastapi | The web framework named in §4.3; created with `docs_url=None`, `redoc_url=None`, `openapi_url=None` so no page loads a CDN (INV-1). |
| uvicorn | ASGI server for the container; no extras. |
| pydantic, pydantic-settings | Pydantic v2 at every boundary (§11); typed settings read from environment and `/run/secrets`. |
| sqlalchemy[asyncio], alembic | ORM and migrations named in §4.3; migrations run at start under a Postgres advisory lock. |
| psycopg[binary] | Async Postgres driver with binary wheels, so the offline build needs no compiler. |
| redis | Async client for login throttling and the health probe. |
| structlog | JSON logs with `trace_id` (§11) and a redaction processor for password, token, secret, Cookie, Set-Cookie and Authorization. |
| httpx | The MinIO health probe and FastAPI's test client. |
| argon2-cffi | argon2id password hashing (P1 scope). |
| pyyaml (+ types-PyYAML for mypy) | Load `config/rbac-roles.yaml`; also used by unit tests to parse the compose file. |

Rules that go with them:
- Every non-2xx body is exactly `{what_happened, likely_cause, what_to_do, trace_id}`
  (plus `reason` on 401). No stack traces, no `detail`.
- `slas_schemas.ThreePartMessage` stays a stdlib dataclass because the host CLI must run
  without a virtualenv; `slas_api.errors.ThreePartProblem` is its Pydantic twin.
- The api image is built with `docker build --network none` from a vendored uv cache and
  runs as `${SLAS_UID}:${SLAS_GID}`; `secrets/` and `tls/` are mounted read-only; the Redis
  URL is assembled in memory from `/run/secrets/redis_password` and never placed in an
  environment variable.
- `apps/api` holds authz, people, settings and (from P2) tickets and approvals; no
  hardware, no LLM (§11 separation).

## Alternatives considered
- Starlette without FastAPI: fewer dependencies, but §4.3 names FastAPI and constrained
  request models are wanted at every boundary.
- asyncpg instead of psycopg 3: comparable; psycopg 3 is the driver SQLAlchemy documents
  for both sync (Alembic) and async use, so one driver serves both.
- Sessions in Redis: sessions are durable data and belong in Postgres; Redis keeps only
  throttling counters and fails closed.

## Consequences
Easier: one documented, pinned dependency set; offline builds; a single error contract the
WebUI renders. Harder: wheel availability for the offline mirror must be confirmed for
psycopg, greenlet and pydantic-core before locking.

## Invariants touched
INV-1 (no docs CDN, no network at build or start), INV-2 (no cloud AI library), INV-5
(secrets read from files, redacted at the logging boundary), INV-8 (every dependency
pinned).
