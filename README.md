# SW Local Agent Service

Self-hosted, air-gap-capable AI workers for server hardware engineering: a **Coding Agent**,
a **Validation Agent** and a **Factory Agent** on one shared kernel. Every agent can operate
the OS of the machine it works on, has critical outputs cross-checked by several local
models, tracks its work as a ticket from ingestion to root-cause analysis, writes its report
and SOP in English and Chinese, and can be extended with importable skills.

No cloud. No external APIs. Nothing leaves the perimeter.

## Start here

| If you are… | Read |
|---|---|
| An AI coding session | [`CLAUDE.md`](CLAUDE.md) — the single source of truth. Read it first, every session. |
| Planning the build | [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — 13 phases, done criteria, milestone demos |
| Driving the build with prompts | [`docs/PROMPTS.md`](docs/PROMPTS.md) — copy-paste prompts per phase |
| Reviewing the intended UI | [`docs/ui-demo/slas-ui-demo.html`](docs/ui-demo/slas-ui-demo.html) — open in a browser, no server needed |

## Quick start (target state, Phase 1)

```bash
tar xzf slas-bundle-<version>.tar.gz
cd slas-bundle-<version>
./install.sh            # preflight → config → load images → models → up → login URL
```

One command, one `.env`, one page to manage models. Details in `CLAUDE.md` §3.

## Status

Phase 0 (skeleton) is merged. Phase 1 (quickstart core) is built: the compose stack
(postgres, redis, minio, api, webui, edge with self-signed TLS), built-in sign-in with
argon2id and roles from `config/rbac-roles.yaml`, Admin → People and Admin → Settings, the
`slas user` and `slas logs` commands, the bundle builder and the full `install.sh`. Follow
`docs/DEVELOPMENT_PLAN.md`; the Phase 1 interfaces are in `docs/plans/P1-contract.md` and
ADR-0003 to ADR-0006. The design review of 2026-09-10 lives in `docs/reviews/`.

## Developing

Prerequisites: Python 3.12, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm through
`corepack enable`. Every dependency is pinned in `uv.lock` and `pnpm-lock.yaml` (INV-8).

```sh
uv sync --locked --all-packages     # every workspace member plus the dev tools
uv run ruff check . && uv run ruff format --check .
uv run mypy                          # --strict, configured in pyproject.toml
uv run pytest                        # tests/unit and tests/deploy, all against fakes
pnpm install --frozen-lockfile
pnpm -r typecheck && pnpm -r test    # apps/webui and tests/e2e
./install.sh --preflight-only        # the host check alone; changes nothing
./install.sh                         # full install from an unpacked bundle (needs Docker or Podman)
scripts/ci/egress-drop.sh --netns    # the whole suite with outbound traffic blocked
scripts/ci/no-cloud-ai.sh            # INV-2 grep
```

## Licence

To be decided (see `CLAUDE.md` §15, open decisions).
