# API contract — round 1 (sign-in, people, settings, models, the Home lists)

The WebUI and `apps/api` are built against this file. Routes live under `/api/v1`; the edge
sends `/api/*` to the api container and everything else to the webui container. Every
response is JSON. Every non-2xx body is exactly

```json
{"what_happened": "…", "likely_cause": "…", "what_to_do": "…", "trace_id": "<32 hex>"}
```

plus `"reason": "expired" | "none"` on 401 (ADR-0005 §errors, ADR-0007). No `detail`, no
stack traces. Copy for every sentence the UI shows is in `docs/ui/sign-in.md`,
`docs/ui/admin-people.md` and `docs/ui/admin-settings.md`; the api sends those sentences,
the UI does not invent its own.

Requests carry `traceparent` and `X-Slas-Trace-Id` (apps/webui/src/trace.ts,
slas_observability.tracing); the api echoes `X-Slas-Trace-Id` on every response. Every
state-changing request (POST, PATCH, DELETE) must carry `X-Requested-With: slas-webui`, or
the api answers 403 in three parts.

## Health and metrics (no auth, not under /api)

| Route | Answer |
|---|---|
| `GET /health` | `200 {"service": "api", "ok": true, "checks": {"postgres": "ok", "redis": "ok"}}`; `503` with the failing check named when one is down. This is what the compose healthcheck probes. |
| `GET /metrics` | Prometheus text from `slas_observability.metrics.REGISTRY`. |

## Public

| Route | Answer |
|---|---|
| `GET /api/v1/public/installation` | `{"installation_name": str, "auth_modes": ["builtin"], "version": str, "session_lifetime_hours": int}` — the sign-in page shows the name. |

## Session (ADR-0007)

Cookie `__Host-slas_session`: HttpOnly, Secure, SameSite=Strict, Path=/. The token is 256
random bits; the database holds its SHA-256. Sliding `last_seen`; lifetime is the runtime
setting `session_lifetime_hours` (default 8) at sign-in time.

| Route | Body | Answer |
|---|---|---|
| `POST /api/v1/session` | `{"email": str, "password": str}` | `200 Person` + Set-Cookie. `401 reason=none` "The email or password is not right." (never says which). `429` after 10 failures in 15 minutes per email or per address (Redis), sentence from docs/ui/sign-in.md. `503` when Redis is unreachable (fails closed, names `slas logs redis`). A switched-off person gets the same 401 as a wrong password. |
| `DELETE /api/v1/session` | — | `204`; the cookie is cleared. |
| `GET /api/v1/me` | — | `200 Person`; `401 reason=expired` when the session existed but ran out, `401 reason=none` otherwise. |
| `POST /api/v1/me/password` | `{"current_password": str, "new_password": str}` | `200 Person` with `must_change_password: false`; other sessions of the person are revoked, this one stays. `400` when the new password is shorter than 12 characters or equals the email; `401` when the current password is wrong. |

`Person`:

```json
{"id": "uuid", "email": "admin@slas.local", "display_name": "Administrator",
 "role": "administrator", "role_label": "Administrator",
 "capabilities": ["admin:people", "admin:settings", "..."], "must_change_password": true,
 "is_active": true, "last_sign_in_at": "2026-09-17T08:00:00Z" | null}
```

While `must_change_password` is true, every route except `GET /api/v1/me`,
`POST /api/v1/me/password`, `DELETE /api/v1/session`, the public route and health answers
`403` with the sentence "Choose a new password first." (docs/ui/sign-in.md).

## People (capability `admin:people`)

| Route | Body | Answer |
|---|---|---|
| `GET /api/v1/admin/people` | — | `200 [Person]`, ordered by display name. |
| `POST /api/v1/admin/people` | `{"email": str, "display_name": str, "role": str}` | `201 {"person": Person, "one_time_password": str}`; the person must change it at first sign-in. `409` when the email exists. `400` for an unknown role (names the roles). |
| `PATCH /api/v1/admin/people/{id}` | `{"role"?: str, "is_active"?: bool}` | `200 Person`. Switching a person off revokes their sessions. An administrator cannot switch off or demote the last active administrator (`400`). |
| `POST /api/v1/admin/people/{id}/password-reset` | — | `200 {"one_time_password": str}`; sessions revoked; `must_change_password` set. |

Every change writes an `audit_log` row (`at, actor, action, subject, detail, via, trace_id`);
`detail` never holds a password.

## Settings (ADR-0008; `admin:settings` to change, any signed-in person to read)

| Route | Body | Answer |
|---|---|---|
| `GET /api/v1/admin/settings` | — | `{"runtime": {"installation_name": str, "chinese_variant": "zh-Hant"|"zh-Hans", "session_lifetime_hours": int}, "install": {"profile", "data_root", "https_port", "tls_mode", "tls_names", "version"}}` |
| `PATCH /api/v1/admin/settings` | any subset of the three runtime keys | `200 {"runtime": {...}, "notice": null | "Saved. The copy in .env could not be updated: <reason>."}`. `400` in three parts for a value out of range (name ≤ 60 chars, hours 1–168). |

After every change and at api start, the three runtime keys are mirrored into
`${SLAS_DATA_ROOT}/.env` under the marker `# managed by Admin → Settings` with
`slas_schemas.envfile` (atomic, mode preserved, other lines untouched). The database wins;
`.env` seeds the table only when it is empty.

## Models (any signed-in person)

| Route | Answer |
|---|---|
| `GET /api/v1/models` | `{"sentence": str, "models": [{"id","display_name","family","path","quant","vram_gib","context","roles": [..], "present": bool}], "roles": {role: id}, "voters": [id], "problem": ThreePart | null}` read from `${SLAS_DATA_ROOT}/Models/models.yaml` on every request (INV-9); `present` is whether `Models/<path>/SHA256SUMS` exists. When the file is missing or invalid, `models` is empty and `problem` says so in three parts. |

## The Home lists (any signed-in person)

Round 1 answers empty lists; the shapes are the WebUI's own types so later rounds only fill
them in.

| Route | Answer |
|---|---|
| `GET /api/v1/coding/tasks` | `[]` of `CodingTask` (apps/webui/src/coding/api.ts) |
| `GET /api/v1/validation/runs` | `[]` of `RunView` (apps/webui/src/validation/api.ts) |
| `GET /api/v1/factory/jobs` | `[]` of `FactoryJob` (apps/webui/src/factory/api.ts) |

## In-container CLI (`slas-api`, ADR-0007)

| Command | Does |
|---|---|
| `slas-api migrate` | Runs the Alembic migrations under a Postgres advisory lock, then the bootstrap (below). The container entrypoint runs it before serving. |
| `slas-api bootstrap status` | Prints `pending` while `admin@slas.local` still has its one-time password, `done` after the first successful change; exit 0 either way. `install.sh` prints the one-time password only while pending. |
| `slas-api user add --email E --display-name N --role R [--password-stdin]` | Adds a person; without `--password-stdin` a one-time password is generated and printed once. |
| `slas-api user list` | One line per person: email, role, active, last sign-in. |

CLI actions run as `SYSTEM` with `via=cli` and write audit rows like the routes do.

## Bootstrap

At first start with an empty `people` table the api creates `admin@slas.local`
("Administrator", role `administrator`) with the password from
`/run/secrets/admin-initial-password` and `must_change_password = true`, in one transaction
guarded by the unique email index. The secret file is never rewritten; the first successful
change records `bootstrap_consumed_at`.
