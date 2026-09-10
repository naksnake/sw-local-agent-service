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

Pre-Phase 0. The repository currently holds the design documents and the UI demo.
Follow `docs/DEVELOPMENT_PLAN.md` from P0.

## Licence

To be decided (see `CLAUDE.md` §15, open decisions).
