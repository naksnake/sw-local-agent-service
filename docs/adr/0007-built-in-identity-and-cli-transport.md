# ADR-0007: Built-in identity, the bootstrap administrator, and how the host CLI reaches the API

Status: proposed
Date: 2026-09-14

## Context
Quickstart authenticates with built-in users (§3); the P1 scope says argon2. `install.sh`
must print a one-time administrator password and reach a login page with no manual step
(INV-10). `slas user add` runs on the platform host, outside every container, and must
never place a password in argv or a log (§11). Credentials never enter a model context
(INV-5) — and in Phase 1, never a log or a URL either.

## Decision
- **Passwords:** argon2id, time cost 3, memory 64 MiB, parallelism 4; rehash on login when
  parameters change; a low-cost profile exists for tests only and a test asserts it is
  never the production default. Policy: at least 12 characters, not equal to the email.
- **Sessions:** server-side rows in Postgres holding the SHA-256 of a 256-bit token; cookie
  `__Host-slas_session`, HttpOnly, Secure, SameSite=Strict, Path=/; sliding `last_seen`;
  lifetime from the runtime setting (default 8 hours). State-changing requests carry
  `X-Requested-With: slas-webui`. A 401 body carries `reason: expired | none` so the UI
  chooses the sentence; a failed login never says which part was wrong. Every request
  re-checks `is_active`; switching a person off and resetting a password both revoke that
  person's sessions.
- **Throttling:** 10 failures per 15 minutes per email and per IP, counted in Redis, and
  **fails closed** with a sentence naming `slas logs redis` when Redis is unreachable.
- **Bootstrap administrator:** at first start with an empty users table, the api creates
  `admin@slas.local` from `/run/secrets/admin-initial-password` with
  `must_change_password`, in one transaction guarded by a unique index. The secret file is
  never deleted or rewritten: a compose file secret is a read-only mount whose source every
  later `compose up` needs. Consumption is recorded in the database
  (`bootstrap_consumed_at` on the first successful password change, plus an audit row).
  `install.sh` asks `slas-api bootstrap status` and prints the sign-in sentence with the
  one-time password only while the status is "pending". One onboarding mechanism for
  everyone: a one-time password and a forced change at first sign-in.
- **Audit:** `audit_log(at, actor, action, subject, detail, via ∈ {webui, cli, installer},
  trace_id)` for every people and settings change; `detail` never contains a password.
- **Host CLI transport:** `${SLAS_DATA_ROOT}/bin/slas user add|list` and `slas logs` build
  the argv `docker compose --project-name slas --project-directory … -f … exec -T api
  slas-api …` and run it through the existing `Host.run` (argv only). Passwords travel only
  on stdin (`--password-stdin`) or are generated inside the container and printed once.
  Docker access on the platform host is treated as equivalent to administrator. The
  in-container `slas-api` commands reuse the same service layer as the REST routes, acting
  as the `SYSTEM` principal with `via=cli`.

## Alternatives considered
- Delete or blank the bootstrap secret after use: breaks the next `compose up` (missing
  source) or is impossible (the mount is read-only inside the api).
- REST with a stored admin token for the CLI: needs a token on disk on the host, which is a
  credential to protect; recorded as the fallback for rootless Docker or Podman hosts where
  `docker compose exec` is not available to the invoking user.
- One-time sign-in links instead of one-time passwords: a URL is easy to paste into a chat
  or a log; a password typed once into a page is not. Open question for the product owner.

## Consequences
Easier: one identity model, one onboarding path, no credential on the host's disk, an
audit trail from day one. Harder: argon2 at 64 MiB per verification is a denial-of-service
lever, which is why throttling ships in the same session; fail-closed throttling locks
everyone out during a Redis outage and the sentence must say what to do.

## Invariants touched
INV-5 (hashed at rest; never in argv, URL, log or model context; redaction proven by tests),
INV-9 (users and sessions are data), INV-10 (the printed one-time password is the only
manual input and it is typed into the login page). No invariant is relaxed.
