# ADR-0003: The Phase 1 dependency set

Status: accepted
Date: 2026-09-11

## Context
CLAUDE.md §0.3 says every third-party dependency is asked for before it is added, and §15
requires a record for each one. Phase 1 (quickstart core) cannot be built from the standard
library: it needs a web framework, a database layer, a password hash, container images and a
handful of test libraries. CLAUDE.md §4.3 and §11 already name FastAPI, Pydantic v2,
SQLAlchemy 2, Alembic, Postgres 16, Redis 7, MinIO, structlog, React 18, Vite, Tailwind,
shadcn/ui, TanStack Query and react-hook-form + zod; `docs/PROMPTS.md` names Caddy and argon2.
The rest was listed for the owner at the Phase 1 kick-off and approved as one batch when the
task was reaffirmed. Every entry is pinned to an exact version in `uv.lock`, `pnpm-lock.yaml`
or `compose/images.lock.json` (INV-8) and none needs the network at runtime (INV-1).

## Decision
The Phase 1 set is exactly the following. Anything else still needs a question first.

| Dependency | Where | Named in CLAUDE.md or PROMPTS.md | Why |
|---|---|---|---|
| fastapi 0.141.1, uvicorn 0.52.4 | `apps/api` | FastAPI yes; uvicorn no (the server FastAPI needs) | the api |
| sqlalchemy[asyncio] 2.0.52, alembic 1.19.2, asyncpg 0.31.0 | `apps/api` | SQLAlchemy and Alembic yes; asyncpg no (the async Postgres driver) | users, sessions, settings |
| argon2-cffi 25.1.0 | `apps/api` | argon2 named in the plan | password hashing (argon2id) |
| structlog 26.1.0 | `apps/api` | yes (§11) | JSON logs with `trace_id` |
| pyyaml 6.0.3, types-pyyaml (dev) | `packages/slas-authz` | no | reads `config/rbac-roles.yaml` |
| httpx 0.28.1, pytest-asyncio 1.4.0 (dev) | workspace | no | FastAPI test client; async tests |
| react-router-dom 7.18.3 | `apps/webui` | no (no router is named) | page routing |
| @tanstack/react-query 5.102.8 | `apps/webui` | yes | server state |
| react-hook-form 7.87.0, zod 4.6.2, @hookform/resolvers 5.9.1 | `apps/webui` | first two yes; resolvers no | forms and boundary validation |
| @radix-ui/react-{slot,label,select,dialog}, lucide-react 1.44.0 | `apps/webui` | shadcn/ui yes; its primitives implied | shadcn/ui components |
| jsdom 30.0.1, @testing-library/{react,dom,user-event,jest-dom} (dev) | `apps/webui` | no | component tests against a scripted API |
| caddy 2.10.2 | image | yes (PROMPTS.md) | edge with self-signed TLS; webui static server |
| postgres 16.10, redis 7.4.6, minio RELEASE.2025-09-07T16-13-09Z | images | yes | the stores |
| python 3.12.11-slim-bookworm, node 22.19.0-bookworm-slim, ghcr.io/astral-sh/uv 0.8.17 | build images | no | building the api and webui images |

Redis is provisioned in compose because §12 lists it and later phases lease through it; the
Phase 1 api does not connect to it, so no Redis client library is added yet. Prometheus
client, TanStack Table and msw were on the candidate list and were not added: `/metrics`
arrives with the observability phase, the People table is small, and the webui tests use a
hand-written scripted client.

## Consequences
Easier: one approved list to check new work against; the api, authz and webui can be built
and tested offline from the lockfiles. Harder: images are built in a networked prepare step
(uv, pnpm and the pinned base images are fetched from the lockfiles); the offline *install*
is what Phase 1 proves, an offline *build* needs a mirror and is recorded as an open decision
in ADR-0004. Every version bump is a lockfile change reviewed like code.

## Invariants touched
INV-1 — nothing here needs the network at runtime; Caddy issues its certificates from a local
CA. INV-2 — no cloud AI library is in the set (CI's `no-cloud-ai` grep still runs). INV-8 —
every entry is pinned exactly; images are additionally pinned by image id in
`compose/images.lock.json`. No invariant is relaxed.
